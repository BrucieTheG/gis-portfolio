"""07 - Routable walk/drive network for pgRouting, built from core.roads.

Noding follows OSM topology: two ways connect only where they share a vertex (an OSM node).
Each way is split at every vertex it shares with another way (or with itself), so bridges and
tunnels that cross other roads without a shared node stay unconnected. Geometric noding
(ST_Node / pgr_separateCrossing) would wrongly join every overpass to the road below it.

Outputs (schema `net`):
  net.edges     one row per way piece between junctions, with pgRouting columns
                source/target and costs in seconds:
                  walk_cost / walk_rcost    1.34 m/s on walkable ways, both directions
                  drive_cost / drive_rcost  free-flow speed from maxspeed or highway defaults,
                                            -1 against a one-way (pgRouting: no edge)
  net.vertices  pgr_extractVertices output plus degree and connected-component ids, and
                in_walk_main / in_drive_main flags for the largest component (the API snaps
                clicked points to these)
"""
from common import SRID, connect, ensure_schemas, log, main_guard, sql

WALK_MPS = 1.34  # typical adult walking speed

# Free-flow speed (mph) by OSM highway value when the way has no usable maxspeed tag.
DEFAULT_MPH = {
    "motorway": 60, "motorway_link": 35, "trunk": 50, "trunk_link": 30,
    "primary": 40, "primary_link": 25, "secondary": 35, "secondary_link": 25,
    "tertiary": 30, "tertiary_link": 25, "unclassified": 25, "residential": 25,
    "road": 25, "living_street": 15, "service": 15,
}
MPH_TO_MPS = 0.44704


def build_edges(conn) -> None:
    speeds = ",".join(f"('{k}',{v})" for k, v in DEFAULT_MPH.items())
    log("splitting ways at shared vertices")
    sql(conn, rf"""
        DROP SCHEMA IF EXISTS net CASCADE;
        CREATE SCHEMA net;

        -- Every vertex of every way, keyed to a 1 cm grid so coordinates compare exactly.
        CREATE UNLOGGED TABLE net._pts AS
        SELECT r.id AS road_id, (d).path[1] AS idx,
               round(ST_X((d).geom)::numeric, 2)::float8 AS x,
               round(ST_Y((d).geom)::numeric, 2)::float8 AS y
        FROM core.roads r, LATERAL ST_DumpPoints(r.geom) d;

        -- A vertex used more than once is a shared OSM node: a junction.
        CREATE UNLOGGED TABLE net._shared AS
        SELECT x, y FROM net._pts GROUP BY x, y HAVING count(*) > 1;

        -- Split points: both ends of each way plus its junction vertices.
        CREATE UNLOGGED TABLE net._splits AS
        SELECT p.road_id, p.idx,
               lead(p.idx) OVER (PARTITION BY p.road_id ORDER BY p.idx) AS next_idx
        FROM net._pts p
        JOIN (SELECT road_id, max(idx) AS last_idx FROM net._pts GROUP BY road_id) m
             USING (road_id)
        LEFT JOIN net._shared s ON s.x = p.x AND s.y = p.y
        WHERE p.idx = 1 OR p.idx = m.last_idx OR s.x IS NOT NULL;
        DELETE FROM net._splits WHERE next_idx IS NULL;
        CREATE INDEX ON net._pts (road_id, idx);

        CREATE UNLOGGED TABLE net._pieces AS
        SELECT k.road_id, k.idx AS from_idx,
               ST_RemoveRepeatedPoints(ST_SetSRID(
                   ST_MakeLine(ST_MakePoint(p.x, p.y) ORDER BY p.idx), {SRID})) AS geom
        FROM net._splits k
        JOIN net._pts p ON p.road_id = k.road_id AND p.idx BETWEEN k.idx AND k.next_idx
        GROUP BY k.road_id, k.idx;
    """)

    log("adding attributes and costs")
    sql(conn, rf"""
        CREATE TABLE net.edges AS
        WITH spd(highway, mph) AS (VALUES {speeds}),
        e AS (
            SELECT k.road_id, r.osm_id, r.highway, r.road_class, r.name, r.ref,
                   r.layer, r.drivable, r.walkable,
                   -- Any bridge/tunnel tag but "no" counts (bridge=viaduct, tunnel=building_passage).
                   COALESCE(w.bridge NOT IN ('no'), false) AS bridge,
                   COALESCE(w.tunnel NOT IN ('no'), false) AS tunnel,
                   -- 1 = one-way with the line, -1 = against it, 0 = both directions.
                   CASE WHEN r.oneway IN ('yes', 'true', '1') THEN 1
                        WHEN r.oneway IN ('-1', 'reverse') THEN -1
                        WHEN r.oneway IS NOT NULL THEN 0
                        WHEN r.highway = 'motorway'
                          OR w.tags->>'junction' IN ('roundabout', 'circular') THEN 1
                        ELSE 0 END AS oneway_dir,
                   -- Bare numbers are treated as mph (US data); anything else falls back.
                   CASE WHEN r.maxspeed ~ '^\d+(\.\d+)?\s*mph$'
                             THEN substring(r.maxspeed FROM '^[\d.]+')::float8
                        WHEN r.maxspeed ~ '^\d+$' THEN r.maxspeed::float8 END AS posted_mph,
                   spd.mph AS default_mph,
                   ST_Length(k.geom) AS length_m,
                   k.geom::geometry(LineString, {SRID}) AS geom
            FROM net._pieces k
            JOIN core.roads r ON r.id = k.road_id
            LEFT JOIN raw.osm_ways w ON w.way_id = r.osm_id
            LEFT JOIN spd ON spd.highway = r.highway
            WHERE ST_NPoints(k.geom) >= 2 AND ST_Length(k.geom) > 0
        )
        SELECT row_number() OVER (ORDER BY road_id, ST_Length(geom))::bigint AS id,
               NULL::bigint AS source, NULL::bigint AS target,
               e.road_id, e.osm_id, e.highway, e.road_class, e.name, e.ref,
               e.layer, e.bridge, e.tunnel, e.drivable, e.walkable, e.oneway_dir,
               CASE WHEN e.drivable THEN COALESCE(e.posted_mph, e.default_mph, 25) END
                   AS speed_mph,
               e.posted_mph IS NOT NULL AS speed_posted,
               e.length_m,
               CASE WHEN e.walkable THEN e.length_m / {WALK_MPS} ELSE -1 END AS walk_cost,
               CASE WHEN e.walkable THEN e.length_m / {WALK_MPS} ELSE -1 END AS walk_rcost,
               CASE WHEN e.drivable AND e.oneway_dir >= 0
                    THEN e.length_m / (COALESCE(e.posted_mph, e.default_mph, 25) * {MPH_TO_MPS})
                    ELSE -1 END AS drive_cost,
               CASE WHEN e.drivable AND e.oneway_dir <= 0
                    THEN e.length_m / (COALESCE(e.posted_mph, e.default_mph, 25) * {MPH_TO_MPS})
                    ELSE -1 END AS drive_rcost,
               e.geom
        FROM e;
        ALTER TABLE net.edges ADD PRIMARY KEY (id);

        DROP TABLE net._pts, net._shared, net._splits, net._pieces;
    """)


def build_vertices(conn) -> None:
    log("extracting vertices (pgr_extractVertices)")
    sql(conn, f"""
        CREATE TABLE net.vertices AS
        SELECT id, in_edges, out_edges, ST_SetSRID(geom, {SRID})::geometry(Point, {SRID}) AS geom
        FROM pgr_extractVertices('SELECT id, geom FROM net.edges ORDER BY id');
        ALTER TABLE net.vertices ADD PRIMARY KEY (id);

        UPDATE net.edges e SET source = v.id
        FROM (SELECT id, unnest(out_edges) AS eid FROM net.vertices) v WHERE e.id = v.eid;
        UPDATE net.edges e SET target = v.id
        FROM (SELECT id, unnest(in_edges) AS eid FROM net.vertices) v WHERE e.id = v.eid;
        ALTER TABLE net.edges ALTER COLUMN source SET NOT NULL,
                              ALTER COLUMN target SET NOT NULL;

        -- Undirected degree: how many edge ends meet here (a self-loop counts twice).
        ALTER TABLE net.vertices ADD COLUMN degree int;
        UPDATE net.vertices
        SET degree = COALESCE(cardinality(in_edges), 0) + COALESCE(cardinality(out_edges), 0);

        CREATE INDEX ON net.edges (source);
        CREATE INDEX ON net.edges (target);
        CREATE INDEX ON net.edges USING gist (geom);
        CREATE INDEX ON net.vertices USING gist (geom);
    """)


def build_components(conn) -> None:
    log("connected components (walk, drive, drive strongly connected)")
    sql(conn, """
        ALTER TABLE net.vertices ADD COLUMN walk_comp bigint, ADD COLUMN drive_comp bigint,
                                 ADD COLUMN drive_scc bigint;

        UPDATE net.vertices v SET walk_comp = c.component
        FROM pgr_connectedComponents(
            'SELECT id, source, target, 1 AS cost, 1 AS reverse_cost
             FROM net.edges WHERE walkable') c
        WHERE v.id = c.node;

        UPDATE net.vertices v SET drive_comp = c.component
        FROM pgr_connectedComponents(
            'SELECT id, source, target, 1 AS cost, 1 AS reverse_cost
             FROM net.edges WHERE drivable') c
        WHERE v.id = c.node;

        -- Directed: from any vertex in a strong component you can drive to any other.
        UPDATE net.vertices v SET drive_scc = c.component
        FROM pgr_strongComponents(
            'SELECT id, source, target, drive_cost AS cost, drive_rcost AS reverse_cost
             FROM net.edges WHERE drivable') c
        WHERE v.id = c.node;

        ALTER TABLE net.vertices ADD COLUMN in_walk_main boolean NOT NULL DEFAULT false,
                                 ADD COLUMN in_drive_main boolean NOT NULL DEFAULT false;
        UPDATE net.vertices SET in_walk_main = true WHERE walk_comp =
            (SELECT walk_comp FROM net.vertices WHERE walk_comp IS NOT NULL
             GROUP BY 1 ORDER BY count(*) DESC LIMIT 1);
        UPDATE net.vertices SET in_drive_main = true WHERE drive_scc =
            (SELECT drive_scc FROM net.vertices WHERE drive_scc IS NOT NULL
             GROUP BY 1 ORDER BY count(*) DESC LIMIT 1);

        CREATE INDEX ON net.vertices USING gist (geom) WHERE in_walk_main;
        CREATE INDEX ON net.vertices USING gist (geom) WHERE in_drive_main;
        ANALYZE net.edges;
        ANALYZE net.vertices;
    """)


def main() -> None:
    conn = connect()
    ensure_schemas(conn)
    build_edges(conn)
    build_vertices(conn)
    build_components(conn)
    (edges, km, walk_km, drive_km), = sql(conn, """
        SELECT count(*), round(sum(length_m)/1000),
               round(sum(length_m) FILTER (WHERE walkable)/1000),
               round(sum(length_m) FILTER (WHERE drivable)/1000) FROM net.edges""")
    (verts, walk_main, drive_main), = sql(conn, """
        SELECT count(*), count(*) FILTER (WHERE in_walk_main),
               count(*) FILTER (WHERE in_drive_main) FROM net.vertices""")
    log(f"net.edges: {edges} ({km} km; walk {walk_km} km, drive {drive_km} km)")
    log(f"net.vertices: {verts} (main walk component {walk_main}, main drive component {drive_main})")


if __name__ == "__main__":
    main_guard(main)
