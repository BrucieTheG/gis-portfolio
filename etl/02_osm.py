"""02 - OpenStreetMap roads, paths, rail and transit features (Geofabrik Missouri extract).

osm2pgsql loads the extract (limited to the study-area bbox) into raw.* with etl/osm_flex.lua,
then SQL clips to the study boundary.

Outputs:
  core.roads         every highway way, with a road class and walk/drive flags
  core.rail_lines    MetroLink light rail and other rail lines
  core.osm_transit   OSM stations, platforms and stops (GTFS in 04 is the main stop source)
"""
from common import (OSM2PGSQL, RAW_DIR, REPO, SRID, connect, download, ensure_schemas, log,
                    main_guard, osm2pgsql_env, run, sql, study_bbox_4326)

URL = "https://download.geofabrik.de/north-america/us/missouri-latest.osm.pbf"

# highway value -> (road_class, drivable, walkable)
HIGHWAY_CLASSES = {
    "motorway": ("motorway", True, False), "motorway_link": ("motorway", True, False),
    "trunk": ("trunk", True, False), "trunk_link": ("trunk", True, False),
    "primary": ("primary", True, True), "primary_link": ("primary", True, True),
    "secondary": ("secondary", True, True), "secondary_link": ("secondary", True, True),
    "tertiary": ("tertiary", True, True), "tertiary_link": ("tertiary", True, True),
    "unclassified": ("local", True, True), "residential": ("local", True, True),
    "living_street": ("local", True, True), "road": ("local", True, True),
    "service": ("service", True, True),
    "footway": ("path", False, True), "pedestrian": ("path", False, True),
    "path": ("path", False, True), "steps": ("path", False, True),
    "cycleway": ("path", False, True), "bridleway": ("path", False, True),
    "corridor": ("path", False, True), "track": ("track", False, True),
}


def main() -> None:
    conn = connect()
    ensure_schemas(conn)
    pbf = download(URL, RAW_DIR / "missouri-latest.osm.pbf", max_age_days=30)

    xmin, ymin, xmax, ymax = study_bbox_4326(conn, buffer_m=1000)
    run([OSM2PGSQL, "--create", "--output=flex", f"--style={REPO / 'etl' / 'osm_flex.lua'}",
         f"--bbox={xmin:.5f},{ymin:.5f},{xmax:.5f},{ymax:.5f}", str(pbf)], env=osm2pgsql_env())

    values = ",".join(f"('{k}','{c}',{str(d).lower()},{str(w).lower()})"
                      for k, (c, d, w) in HIGHWAY_CLASSES.items())
    sql(conn, f"""
        DROP TABLE IF EXISTS core.roads CASCADE;
        CREATE TABLE core.roads AS
        WITH cls(highway, road_class, drivable, walkable) AS (VALUES {values}),
        b AS (SELECT geom FROM core.study_boundary)
        SELECT w.way_id AS osm_id, w.highway, cls.road_class, w.name, w.ref, w.oneway, w.maxspeed,
               w.lanes, w.surface, w.bridge = 'yes' AS bridge, w.tunnel = 'yes' AS tunnel,
               COALESCE(w.layer, '0') AS layer,
               (cls.drivable AND COALESCE(w.access, 'yes') NOT IN ('no', 'private')) AS drivable,
               ((cls.walkable AND COALESCE(w.foot, 'yes') <> 'no')
                   OR w.foot IN ('yes', 'designated', 'permissive')) AS walkable,
               w.sidewalk,
               (ST_Dump(CASE WHEN ST_CoveredBy(w.geom, b.geom) THEN w.geom
                             ELSE ST_CollectionExtract(ST_Intersection(w.geom, b.geom), 2) END)
               ).geom::geometry(LineString, {SRID}) AS geom
        FROM raw.osm_ways w
        JOIN cls USING (highway)
        JOIN b ON ST_Intersects(w.geom, b.geom);
        ALTER TABLE core.roads ADD COLUMN id bigserial PRIMARY KEY;
        CREATE INDEX ON core.roads USING gist (geom);
        CREATE INDEX ON core.roads (road_class);

        DROP TABLE IF EXISTS core.rail_lines CASCADE;
        CREATE TABLE core.rail_lines AS
        SELECT r.way_id AS osm_id, r.railway, r.name, r.service,
               (ST_Dump(ST_CollectionExtract(ST_Intersection(r.geom, b.geom), 2))).geom
                   ::geometry(LineString, {SRID}) AS geom
        FROM raw.osm_rail r JOIN core.study_boundary b ON ST_Intersects(r.geom, b.geom);
        ALTER TABLE core.rail_lines ADD COLUMN id bigserial PRIMARY KEY;
        CREATE INDEX ON core.rail_lines USING gist (geom);

        DROP TABLE IF EXISTS core.osm_transit CASCADE;
        CREATE TABLE core.osm_transit AS
        SELECT t.osm_type, t.osm_id, t.kind, t.name, t.geom::geometry(Point, {SRID}) AS geom
        FROM raw.osm_transit t JOIN core.study_boundary b ON ST_Intersects(t.geom, b.geom);
        CREATE INDEX ON core.osm_transit USING gist (geom);
    """)
    for t in ("roads", "rail_lines", "osm_transit"):
        n = sql(conn, f"SELECT count(*) FROM core.{t}")[0][0]
        log(f"core.{t}: {n} rows")
    km = sql(conn, "SELECT road_class, round(sum(ST_Length(geom))/1000) FROM core.roads "
                   "GROUP BY 1 ORDER BY 2 DESC")
    log("road km by class: " + ", ".join(f"{c} {k}" for c, k in km))


if __name__ == "__main__":
    main_guard(main)
