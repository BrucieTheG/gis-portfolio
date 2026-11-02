"""05 - FEMA National Flood Hazard Layer (NFHL) flood hazard zones.

Queried from FEMA's NFHL map service (layer 28, Flood Hazard Zones): first the object IDs
inside the study bbox, then the features in chunks, cached as GeoJSON in data/raw/fema/.
The service is slow and sometimes fails, so chunks are retried and reused if already saved.

Output:
  core.flood_zones  zones clipped to the study area, classified as
                    '100-yr' (Special Flood Hazard Area: A, AE, AH, AO, ...),
                    '500-yr' (Zone X, 0.2% annual chance), or
                    'levee'  (Zone X, reduced risk due to levee)
"""
from common import (RAW_DIR, SRID, arcgis_fetch, connect, ensure_schemas, log, main_guard,
                    ogr_to_pg, sql, study_bbox_4326)

LAYER = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28"
FIELDS = "OBJECTID,DFIRM_ID,FLD_ZONE,ZONE_SUBTY,SFHA_TF,STATIC_BFE,DEPTH,SOURCE_CIT"


def main() -> None:
    conn = connect()
    ensure_schemas(conn)
    # Big river polygons make this server time out; arcgis_fetch splits failing batches.
    merged = arcgis_fetch(LAYER, FIELDS, RAW_DIR / "fema", study_bbox_4326(conn))
    ogr_to_pg(str(merged), "fema_flood_zones", "-t_srs", f"EPSG:{SRID}", "-nlt", "MULTIPOLYGON")

    sql(conn, f"""
        DROP TABLE IF EXISTS core.flood_zones CASCADE;
        CREATE TABLE core.flood_zones AS
        WITH classed AS (
            SELECT f.*,
                   CASE WHEN f.sfha_tf = 'T' THEN '100-yr'
                        WHEN f.zone_subty ILIKE '%0.2 PCT%' THEN '500-yr'
                        WHEN f.zone_subty ILIKE '%LEVEE%' THEN 'levee'
                   END AS flood_class
            FROM raw.fema_flood_zones f
        )
        SELECT c.objectid AS fema_id, c.dfirm_id, c.flood_class, c.fld_zone, c.zone_subty,
               NULLIF(c.static_bfe, -9999) AS static_bfe,
               (ST_Dump(ST_CollectionExtract(ST_Intersection(ST_MakeValid(c.geom), b.geom), 3))
               ).geom::geometry(Polygon, {SRID}) AS geom
        FROM classed c JOIN core.study_boundary b ON ST_Intersects(c.geom, b.geom)
        WHERE c.flood_class IS NOT NULL;
        DELETE FROM core.flood_zones WHERE ST_Area(geom) < 1;  -- slivers from clipping
        ALTER TABLE core.flood_zones ADD COLUMN id bigserial PRIMARY KEY;
        CREATE INDEX ON core.flood_zones USING gist (geom);
        CREATE INDEX ON core.flood_zones (flood_class);
    """)
    rows = sql(conn, "SELECT flood_class, count(*), round((sum(ST_Area(geom))/1e6)::numeric, 1) "
                     "FROM core.flood_zones GROUP BY 1 ORDER BY 1")
    log("core.flood_zones: " + ", ".join(f"{c} {n} polys / {a} km2" for c, n, a in rows))
    zones = sql(conn, "SELECT fld_zone, zone_subty, count(*) FROM raw.fema_flood_zones "
                      "GROUP BY 1, 2 ORDER BY 3 DESC")
    for z in zones:
        log(f"  raw zone: {z}")


if __name__ == "__main__":
    main_guard(main)
