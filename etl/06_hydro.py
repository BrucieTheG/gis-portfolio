"""06 - USGS National Hydrography Dataset (NHD, high resolution) for map context. Display only.

Uses the staged HUC8 GeoPackages on the USGS S3 bucket. (The NHD ArcGIS map service was
returning 504s for every request when this was written; the static files are more reliable.)

Outputs:
  core.hydro_lines  streams, rivers and canals (flowlines), with GNIS names
  core.hydro_areas  river areas (Mississippi, Missouri, Meramec) plus lakes, ponds and reservoirs
"""
from common import (RAW_DIR, SRID, connect, download, ensure_schemas, log, main_guard,
                    ogr_to_pg, sql, study_bbox_4326)

S3 = "https://prd-tnm.s3.amazonaws.com/StagedProducts/Hydrography/NHD/HU8/GPKG"
# Watersheds touching St. Louis City + County. The script checks that their WBD outlines
# cover the whole study area and stops if any part is missed.
HUC8S = ("07140101",   # Cahokia-Joachim (Mississippi River, City, south County)
         "07140102",   # Meramec
         "10300200",   # Lower Missouri
         "07110009")   # Peruque-Piasa (Mississippi above the confluence)

# NHD FType codes: 460 StreamRiver, 336 CanalDitch, 390 LakePond, 436 Reservoir, 466 SwampMarsh.
# 558 ArtificialPath (centerlines through rivers and lakes) is dropped; the areas draw those.
FLOWLINE_FTYPES = {460: "stream/river", 336: "canal/ditch"}
AREA_FTYPES = {460: "river", 390: "lake/pond", 436: "reservoir", 466: "swamp/marsh"}

LAYERS = (  # (gpkg layer, raw table, attribute filter)
    ("NHDFlowline", "nhd_flowline", f"ftype IN ({','.join(map(str, FLOWLINE_FTYPES))})"),
    ("NHDArea", "nhd_area", "ftype = 460"),
    ("NHDWaterbody", "nhd_waterbody", "ftype IN (390, 436, 466)"),
    ("WBDHU8", "nhd_wbdhu8", None),
)


def main() -> None:
    conn = connect()
    ensure_schemas(conn)
    xmin, ymin, xmax, ymax = study_bbox_4326(conn, buffer_m=500)

    for n, huc in enumerate(HUC8S):
        name = f"NHD_H_{huc}_HU8_GPKG"
        z = download(f"{S3}/{name}.zip", RAW_DIR / "nhd" / f"{name}.zip")
        gpkg = f"/vsizip/{z.as_posix()}/{name}.gpkg"
        for layer, table, where in LAYERS:
            extra = ["-t_srs", f"EPSG:{SRID}", "-dim", "XY", "-select",
                     "huc8,name" if layer == "WBDHU8" else "gnis_name,ftype,fcode",
                     "-spat", str(xmin), str(ymin), str(xmax), str(ymax), "-spat_srs", "EPSG:4326"]
            if where:
                extra += ["-where", where]
            ogr_to_pg(gpkg, table, *extra, layer=layer, append=n > 0)

    # Coverage check: every part of the study area must fall inside a downloaded HUC8.
    missing_km2 = sql(conn, """
        SELECT round((ST_Area(ST_Difference(b.geom, ST_Union(w.geom)))/1e6)::numeric, 3)
        FROM core.study_boundary b, raw.nhd_wbdhu8 w GROUP BY b.geom""")[0][0]
    if missing_km2 > 0.01:
        raise SystemExit(f"HUC8 list misses {missing_km2} km2 of the study area; add a watershed")
    log(f"HUC8 coverage check passed (uncovered: {missing_km2} km2)")

    flow_cls = ",".join(f"({k},'{v}')" for k, v in FLOWLINE_FTYPES.items())
    area_cls = ",".join(f"({k},'{v}')" for k, v in AREA_FTYPES.items())
    sql(conn, f"""
        DROP TABLE IF EXISTS core.hydro_lines CASCADE;
        CREATE TABLE core.hydro_lines AS
        WITH cls(ftype, kind) AS (VALUES {flow_cls})
        SELECT f.gnis_name AS name, cls.kind,
               (ST_Dump(ST_CollectionExtract(ST_Intersection(f.geom, b.geom), 2))).geom
                   ::geometry(LineString, {SRID}) AS geom
        FROM raw.nhd_flowline f
        JOIN cls ON cls.ftype = f.ftype
        JOIN core.study_boundary b ON ST_Intersects(f.geom, b.geom);
        ALTER TABLE core.hydro_lines ADD COLUMN id bigserial PRIMARY KEY;
        CREATE INDEX ON core.hydro_lines USING gist (geom);

        DROP TABLE IF EXISTS core.hydro_areas CASCADE;
        CREATE TABLE core.hydro_areas AS
        WITH cls(ftype, kind) AS (VALUES {area_cls}),
        src AS (
            SELECT gnis_name, ftype, geom FROM raw.nhd_area
            UNION ALL
            SELECT gnis_name, ftype, geom FROM raw.nhd_waterbody
        )
        SELECT s.gnis_name AS name, cls.kind,
               (ST_Dump(ST_CollectionExtract(ST_Intersection(ST_MakeValid(s.geom), b.geom), 3)))
                   .geom::geometry(Polygon, {SRID}) AS geom
        FROM src s
        JOIN cls USING (ftype)
        JOIN core.study_boundary b ON ST_Intersects(s.geom, b.geom);
        DELETE FROM core.hydro_areas WHERE ST_Area(geom) < 1;
        ALTER TABLE core.hydro_areas ADD COLUMN id bigserial PRIMARY KEY;
        CREATE INDEX ON core.hydro_areas USING gist (geom);
    """)
    lines = sql(conn, "SELECT kind, count(*), round((sum(ST_Length(geom))/1000)::numeric) "
                      "FROM core.hydro_lines GROUP BY 1")
    areas = sql(conn, "SELECT kind, count(*), round((sum(ST_Area(geom))/1e6)::numeric, 1) "
                      "FROM core.hydro_areas GROUP BY 1")
    log("core.hydro_lines: " + ", ".join(f"{k} {n} / {km} km" for k, n, km in lines))
    log("core.hydro_areas: " + ", ".join(f"{k} {n} / {a} km2" for k, n, a in areas))
    named = sql(conn, "SELECT name, round((sum(ST_Area(geom))/1e6)::numeric, 1) FROM core.hydro_areas "
                      "WHERE kind = 'river' GROUP BY 1 ORDER BY 2 DESC LIMIT 4")
    log("largest river areas: " + ", ".join(f"{n} {a} km2" for n, a in named))


if __name__ == "__main__":
    main_guard(main)
