"""Road network QC for net.* (built by 07_network.py). Writes docs/network-qc.md.

Two kinds of output:
  * Checks (pass/fail, exit status 1 on failure): every edge is wired to vertices, splitting
    lost no length, and the main walk and drive components hold almost all of the network.
  * Findings (for review, like a data-production QC pass): dead ends, near-miss gaps
    (undershoots), crossings with no shared node, duplicated ways, islands and one-way traps.
    Each finding is also saved as a table in the `qc` schema so it can be mapped.
"""
import sys
import time

from common import REPO, SRID, connect, log, sql

NEAR_MISS_M = 3.0      # a dead end this close to another edge is a likely missing connection
BOUNDARY_M = 5.0       # dead ends this close to the study boundary are clipping artifacts
MAIN_SHARE_MIN = 95.0  # % of walk / drive length that must be in the main component
MAJOR = ("motorway", "trunk", "primary", "secondary", "tertiary")

BUILD_SQL = f"""
CREATE SCHEMA IF NOT EXISTS qc;

DROP TABLE IF EXISTS qc.boundary_lines;
CREATE TABLE qc.boundary_lines AS
SELECT ST_Subdivide(ST_Boundary(geom), 64)::geometry(LineString, {SRID}) AS geom
FROM core.study_boundary;
CREATE INDEX ON qc.boundary_lines USING gist (geom);

-- Dead ends: vertices with one edge end. near_miss_m is the gap to the nearest edge that is
-- not attached to the vertex (NULL if none within {NEAR_MISS_M} m).
DROP TABLE IF EXISTS qc.net_dead_ends;
CREATE TABLE qc.net_dead_ends AS
SELECT v.id AS vertex_id, e.id AS edge_id, e.highway, e.road_class, e.name,
       EXISTS (SELECT 1 FROM qc.boundary_lines b WHERE ST_DWithin(v.geom, b.geom, {BOUNDARY_M}))
           AS at_boundary,
       nm.dist AS near_miss_m, nm.road_class AS near_miss_class,
       v.geom
FROM net.vertices v
JOIN net.edges e ON v.id IN (e.source, e.target)
LEFT JOIN LATERAL (
    SELECT ST_Distance(v.geom, o.geom) AS dist, o.road_class
    FROM net.edges o
    WHERE ST_DWithin(v.geom, o.geom, {NEAR_MISS_M}) AND v.id NOT IN (o.source, o.target)
    ORDER BY v.geom <-> o.geom LIMIT 1) nm ON true
WHERE v.degree = 1;
CREATE INDEX ON qc.net_dead_ends USING gist (geom);

-- Edges whose interiors cross on the same layer, neither a bridge nor a tunnel, with no shared
-- node: either a missing junction or a missing bridge/layer tag.
DROP TABLE IF EXISTS qc.net_unnoded_crossings;
CREATE TABLE qc.net_unnoded_crossings AS
SELECT a.id AS edge_a, b.id AS edge_b, a.road_class AS class_a, b.road_class AS class_b,
       CASE WHEN a.drivable AND b.drivable THEN 'road x road'
            WHEN a.drivable OR b.drivable THEN 'path x road'
            ELSE 'path x path' END AS kind,
       ST_GeometryN(ST_CollectionExtract(ST_Intersection(a.geom, b.geom), 1), 1)
           ::geometry(Point, {SRID}) AS geom
FROM net.edges a
JOIN net.edges b ON a.id < b.id AND a.geom && b.geom
WHERE ST_Crosses(a.geom, b.geom)
  AND a.layer = b.layer
  AND NOT (a.bridge OR a.tunnel OR b.bridge OR b.tunnel);
DELETE FROM qc.net_unnoded_crossings WHERE geom IS NULL;
CREATE INDEX ON qc.net_unnoded_crossings USING gist (geom);

-- Different ways drawn on top of each other for more than 1 m.
DROP TABLE IF EXISTS qc.net_overlaps;
CREATE TABLE qc.net_overlaps AS
SELECT a.id AS edge_a, b.id AS edge_b, a.road_class AS class_a, b.road_class AS class_b,
       ST_Length(ST_Intersection(a.geom, b.geom)) AS overlap_m,
       ST_Multi(ST_CollectionExtract(ST_Intersection(a.geom, b.geom), 2))
           ::geometry(MultiLineString, {SRID}) AS geom
FROM net.edges a
JOIN net.edges b ON a.id < b.id AND a.geom && b.geom AND a.road_id <> b.road_id
WHERE ST_Overlaps(a.geom, b.geom) OR ST_Equals(a.geom, b.geom);
DELETE FROM qc.net_overlaps WHERE overlap_m <= 1;
CREATE INDEX ON qc.net_overlaps USING gist (geom);

-- Islands: connected pieces of the walk or drive network outside the main component.
DROP TABLE IF EXISTS qc.net_islands;
CREATE TABLE qc.net_islands AS
SELECT 'walk' AS mode, v.walk_comp AS component, count(*) AS edges,
       sum(e.length_m) AS length_m,
       ST_Multi(ST_Union(e.geom))::geometry(MultiLineString, {SRID}) AS geom
FROM net.edges e JOIN net.vertices v ON v.id = e.source
WHERE e.walkable AND NOT v.in_walk_main
GROUP BY v.walk_comp
UNION ALL
SELECT 'drive', v.drive_comp, count(*), sum(e.length_m),
       ST_Multi(ST_Union(e.geom))::geometry(MultiLineString, {SRID})
FROM net.edges e JOIN net.vertices v ON v.id = e.source
WHERE e.drivable AND v.drive_comp <> (SELECT drive_comp FROM net.vertices
                                      WHERE in_drive_main LIMIT 1)
GROUP BY v.drive_comp;
CREATE INDEX ON qc.net_islands USING gist (geom);

-- One-way traps: drive vertices joined to the main road network that you can't drive both
-- to and from (outside the main strongly connected component).
DROP TABLE IF EXISTS qc.net_oneway_traps;
CREATE TABLE qc.net_oneway_traps AS
SELECT v.id AS vertex_id, v.drive_scc, v.geom
FROM net.vertices v
WHERE v.drive_scc IS NOT NULL AND NOT v.in_drive_main
  AND v.drive_comp = (SELECT drive_comp FROM net.vertices WHERE in_drive_main LIMIT 1);
CREATE INDEX ON qc.net_oneway_traps USING gist (geom);
"""


def one(conn, q, params=None):
    return sql(conn, q, params)[0]


def _numeric(v) -> bool:
    return isinstance(v, (int, float)) or str(v).replace(",", "").replace(".", "", 1) \
        .lstrip("-").isdigit()


def table(header: list[str], rows) -> list[str]:
    """Markdown table; a column is right-aligned when every value in it is a number."""
    rows = [tuple(r) for r in rows]
    right = [bool(rows) and all(_numeric(r[i]) for r in rows) for i in range(len(header))]
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join("--:" if r else "---" for r in right) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return out


def main() -> int:
    conn = connect()
    t = time.time()
    log("building QC tables")
    sql(conn, BUILD_SQL)
    log(f"QC tables built in {time.time() - t:.0f}s")

    # --- checks ---------------------------------------------------------------------------
    unwired, = one(conn, "SELECT count(*) FROM net.edges WHERE source IS NULL OR target IS NULL")
    road_km, net_km = one(conn, "SELECT (SELECT sum(ST_Length(geom)) FROM core.roads)/1000, "
                                "(SELECT sum(length_m) FROM net.edges)/1000")
    walk_share, drive_share = one(conn, """
        SELECT 100 * sum(e.length_m) FILTER (WHERE e.walkable AND v.in_walk_main)
                   / sum(e.length_m) FILTER (WHERE e.walkable),
               100 * sum(e.length_m) FILTER (WHERE e.drivable AND v.in_drive_main)
                   / sum(e.length_m) FILTER (WHERE e.drivable)
        FROM net.edges e JOIN net.vertices v ON v.id = e.source""")
    length_diff = abs(net_km - road_km) / road_km * 100
    checks = [
        ("Every edge has a source and target vertex", f"{unwired} unwired", unwired == 0),
        ("Splitting kept the road length (within 0.01%)",
         f"{road_km:,.1f} km in, {net_km:,.1f} km out", length_diff < 0.01),
        (f"Main walk component holds at least {MAIN_SHARE_MIN:.0f}% of walkable length",
         f"{walk_share:.1f}%", walk_share >= MAIN_SHARE_MIN),
        (f"Main drive component (strongly connected) holds at least {MAIN_SHARE_MIN:.0f}% "
         "of drivable length", f"{drive_share:.1f}%", drive_share >= MAIN_SHARE_MIN),
    ]
    failures = sum(not ok for *_, ok in checks)

    # --- summary --------------------------------------------------------------------------
    edges, verts, walk_km, drive_km, oneway_km, posted = one(conn, """
        SELECT (SELECT count(*) FROM net.edges), (SELECT count(*) FROM net.vertices),
               sum(length_m) FILTER (WHERE walkable)/1000, sum(length_m) FILTER (WHERE drivable)/1000,
               sum(length_m) FILTER (WHERE drivable AND oneway_dir <> 0)/1000,
               100 * sum(length_m) FILTER (WHERE speed_posted) / sum(length_m) FILTER (WHERE drivable)
        FROM net.edges""")
    junctions, = one(conn, "SELECT count(*) FROM net.vertices WHERE degree >= 3")

    # --- findings -------------------------------------------------------------------------
    de_total, de_boundary = one(conn, "SELECT count(*), count(*) FILTER (WHERE at_boundary) "
                                      "FROM qc.net_dead_ends")
    de_by_class = sql(conn, f"""
        SELECT CASE WHEN road_class IN {MAJOR} THEN 'major road (' || road_class || ')'
                    ELSE road_class END AS cls,
               count(*),
               count(*) FILTER (WHERE near_miss_m <= 1),
               count(*) FILTER (WHERE near_miss_m > 1)
        FROM qc.net_dead_ends WHERE NOT at_boundary
        GROUP BY 1 ORDER BY 2 DESC""")
    major_examples = sql(conn, f"""
        SELECT COALESCE(d.name, '(unnamed)'), d.highway,
               round(ST_Y(ST_Transform(d.geom, 4326))::numeric, 5),
               round(ST_X(ST_Transform(d.geom, 4326))::numeric, 5),
               COALESCE(round(d.near_miss_m::numeric, 2)::text, '-')
        FROM qc.net_dead_ends d
        WHERE NOT at_boundary AND road_class IN {MAJOR}
        ORDER BY d.near_miss_m NULLS LAST, d.name, d.vertex_id LIMIT 10""")
    cross = sql(conn, "SELECT kind, count(*) FROM qc.net_unnoded_crossings GROUP BY 1 ORDER BY 2 DESC")
    overlaps, overlap_m = one(conn, "SELECT count(*), COALESCE(sum(overlap_m), 0) FROM qc.net_overlaps")
    islands = sql(conn, """
        SELECT mode, count(*), round((sum(length_m)/1000)::numeric, 1),
               count(*) FILTER (WHERE length_m >= 1000), round((max(length_m)/1000)::numeric, 2)
        FROM qc.net_islands GROUP BY 1 ORDER BY 1""")
    traps, = one(conn, "SELECT count(*) FROM qc.net_oneway_traps")
    cross_total = sum(n for _, n in cross)

    # --- report ---------------------------------------------------------------------------
    L = ["# Road Network QC", "",
         f"Generated {time.strftime('%Y-%m-%d %H:%M')} by `etl/network_qc.py` for the network "
         "built by `etl/07_network.py` (schema `net`).", "",
         "## Checks", ""]
    L += table(["Check", "Value", "Result"],
               [(c, v, "PASS" if ok else "**FAIL**") for c, v, ok in checks])
    L += ["", f"**{len(checks) - failures} of {len(checks)} checks pass.**", "",
          "## Network summary", ""]
    L += table(["Measure", "Value"], [
        ("Edges / vertices", f"{edges:,} / {verts:,}"),
        ("Junctions (3+ edge ends)", f"{junctions:,}"),
        ("Walkable length", f"{walk_km:,.0f} km"),
        ("Drivable length", f"{drive_km:,.0f} km ({oneway_km:,.0f} km one-way)"),
        ("Drivable length with a posted speed limit", f"{posted:.1f}% (the rest use class defaults)"),
        ("Walk main component", f"{walk_share:.1f}% of walkable length"),
        ("Drive main component (strongly connected)", f"{drive_share:.1f}% of drivable length"),
    ])
    L += ["", "## Findings for review", "",
          "These are not failures. They're the kind of issues a data-production QC pass flags "
          "for an editor to confirm or fix. Each is a table in the `qc` schema so it can be mapped.",
          "", f"### Dead ends (`qc.net_dead_ends`): {de_total:,}", "",
          f"{de_boundary:,} are within {BOUNDARY_M:.0f} m of the study boundary, where clipping "
          f"cut the way, and are left out below. A *near miss* is a dead end within "
          f"{NEAR_MISS_M:.0f} m of an edge it doesn't connect to: a likely undershoot.", ""]
    L += table(["Class of the dead-end edge", "Dead ends", "Near miss ≤ 1 m",
                f"Near miss 1–{NEAR_MISS_M:.0f} m"], de_by_class)
    L += ["", "Most local and service dead ends are real cul-de-sacs and driveways. Dead ends on "
          "major roads are rarer and worth a look:", ""]
    L += table(["Name", "Highway", "Lat", "Lon", "Gap to nearest edge (m)"], major_examples) \
        if major_examples else ["None."]
    L += ["", f"### Crossings with no shared node (`qc.net_unnoded_crossings`): {cross_total:,}", "",
          "Two edges on the same layer, neither tagged bridge or tunnel, cross without a junction. "
          "Either the junction node is missing, or a bridge/tunnel/layer tag is.", ""]
    L += table(["Kind", "Crossings"], cross) if cross else ["None."]
    L += ["", f"### Duplicated ways (`qc.net_overlaps`): {overlaps:,}", "",
          f"Different ways drawn on top of each other for more than 1 m ({overlap_m / 1000:,.1f} km "
          "in total).", "",
          "### Islands (`qc.net_islands`)", "",
          "Connected pieces outside the main network. Routing can't reach them, so the "
          "isochrone tool snaps clicks to the main component.", ""]
    L += table(["Mode", "Islands", "Total km", "Islands ≥ 1 km", "Largest (km)"], islands)
    L += ["", f"### One-way traps (`qc.net_oneway_traps`): {traps:,}", "",
          "Drive vertices joined to the main network that you can drive into but not out of "
          "(or the reverse), usually from a one-way tag pointing the wrong way or a gated exit.",
          "", "## Method notes",
          "- **Noding follows OSM topology.** Ways are split only at vertices they share with "
          "another way (an OSM node), so overpasses don't get false junctions. Geometric noding "
          "(`ST_Node`, `pgr_separateCrossing`) would join every bridge to the road below it.",
          "- **Costs** are in seconds. Walking is 1.34 m/s on walkable ways in both directions. "
          "Driving uses posted `maxspeed` (bare numbers read as mph) or a default by highway "
          "type, at free-flow speed with no turn penalties or traffic.",
          "- **One-way:** `oneway=yes/-1`, plus the implied one-way of motorways and roundabouts. "
          "Walking ignores one-way tags.",
          f"- **Islands and one-way traps** come from `pgr_connectedComponents` and "
          f"`pgr_strongComponents`.", ""]

    out = REPO / "docs" / "network-qc.md"
    out.write_text("\n".join(L), encoding="utf-8")
    log(f"wrote {out.relative_to(REPO)}; {failures} failing check(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
