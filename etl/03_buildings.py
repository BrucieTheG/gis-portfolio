"""03 - Building footprints from Overture Maps (merged OSM + Microsoft + Google + Esri).

The overturemaps CLI downloads only the study-area bbox as GeoParquet; ogr2ogr loads it.

Output:
  core.buildings  footprint polygon, area_m2, height_m and levels where known, source dataset
"""
import subprocess
from pathlib import Path

from common import (RAW_DIR, SRID, connect, ensure_schemas, log, main_guard, ogr_to_pg, sql,
                    study_bbox_4326)

OVERTURE = str(Path(__file__).resolve().parent.parent / ".venv" / "Scripts" / "overturemaps.exe")


def flatten(src: Path) -> Path:
    """Rewrite the Overture GeoParquet with flat columns and no compression.

    The PostGIS-bundled GDAL can't decode snappy, and can't use the nested
    `sources` list-of-structs, so keep the first source's dataset name as text.
    """
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    dest = src.with_name(src.stem + "_flat.parquet")
    if dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime:
        return dest
    t = pq.read_table(src, columns=["id", "geometry", "height", "num_floors", "class",
                                    "subtype", "sources"])
    dataset = pc.struct_field(pc.list_element(t["sources"], 0), "dataset")
    t = t.drop_columns(["sources"]).append_column("source_dataset", dataset)
    pq.write_table(t.replace_schema_metadata(t.schema.metadata), dest, compression="none")
    log(f"flattened -> {dest.name} ({dest.stat().st_size / 1e6:.1f} MB)")
    return dest


def main() -> None:
    conn = connect()
    ensure_schemas(conn)
    out = RAW_DIR / "overture_buildings.parquet"
    if out.exists():
        log(f"cached: {out.name} ({out.stat().st_size / 1e6:.1f} MB) - delete it to re-download")
    else:
        xmin, ymin, xmax, ymax = study_bbox_4326(conn, buffer_m=200)
        bbox = f"{xmin:.5f},{ymin:.5f},{xmax:.5f},{ymax:.5f}"
        log(f"downloading Overture buildings for bbox {bbox} (takes a few minutes)")
        tmp = out.with_suffix(".part")
        subprocess.run([OVERTURE, "download", f"--bbox={bbox}", "-f", "geoparquet",
                        "-t", "building", "-o", str(tmp)], check=True)
        tmp.replace(out)
        log(f"saved {out.name} ({out.stat().st_size / 1e6:.1f} MB)")

    flat = flatten(out)
    ogr_to_pg(str(flat), "overture_buildings", "-t_srs", f"EPSG:{SRID}", "-nlt", "MULTIPOLYGON")

    sql(conn, f"""
        DROP TABLE IF EXISTS core.buildings CASCADE;
        CREATE TABLE core.buildings AS
        SELECT o.id AS overture_id,
               o.class AS bldg_class, o.subtype,
               NULLIF(o.height, 0)::real AS height_m,
               NULLIF(o.num_floors, 0)::smallint AS levels,
               COALESCE(o.source_dataset, 'unknown') AS source,
               ST_Multi(ST_MakeValid(o.geom))::geometry(MultiPolygon, {SRID}) AS geom
        FROM raw.overture_buildings o
        JOIN core.study_boundary b ON ST_Intersects(b.geom, ST_PointOnSurface(o.geom));
        -- Assign each building to the study area by its interior point, so footprints that
        -- straddle the county line are kept whole and counted once.
        ALTER TABLE core.buildings ADD COLUMN id bigserial PRIMARY KEY,
                                   ADD COLUMN area_m2 real;
        UPDATE core.buildings SET area_m2 = ST_Area(geom);
        CREATE INDEX ON core.buildings USING gist (geom);
    """)
    rows = sql(conn, """SELECT count(*), round((sum(area_m2)/1e6)::numeric, 1), count(height_m), count(levels)
                        FROM core.buildings""")[0]
    log(f"core.buildings: {rows[0]} rows, {rows[1]} km2 footprint, "
        f"{rows[2]} with height, {rows[3]} with levels")
    src = sql(conn, "SELECT source, count(*) FROM core.buildings GROUP BY 1 ORDER BY 2 DESC")
    log("by source: " + ", ".join(f"{s} {n}" for s, n in src))


if __name__ == "__main__":
    main_guard(main)
