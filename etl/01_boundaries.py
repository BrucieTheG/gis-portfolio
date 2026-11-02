"""01 - Study area boundary from Census TIGER/Line 2024 counties.

Outputs:
  core.counties        St. Louis City (29510) and St. Louis County (29189)
  core.study_boundary  one dissolved polygon, used to clip every other layer
"""
from common import (COUNTY_FIPS, RAW_DIR, SRID, STATE_FIPS, connect, download, ensure_schemas,
                    log, main_guard, ogr_to_pg, sql)

URL = "https://www2.census.gov/geo/tiger/TIGER2024/COUNTY/tl_2024_us_county.zip"


def main() -> None:
    conn = connect()
    ensure_schemas(conn)
    z = download(URL, RAW_DIR / "tl_2024_us_county.zip")

    fips = ",".join(f"'{c}'" for c in COUNTY_FIPS)
    ogr_to_pg(f"/vsizip/{z.as_posix()}/tl_2024_us_county.shp", "tiger_county",
              "-where", f"STATEFP = '{STATE_FIPS}' AND COUNTYFP IN ({fips})",
              "-t_srs", f"EPSG:{SRID}", "-lco", "LAUNDER=YES")

    sql(conn, f"""
        DROP TABLE IF EXISTS core.counties CASCADE;
        CREATE TABLE core.counties AS
        SELECT geoid, name, namelsad,
               ST_Multi(ST_MakeValid(geom))::geometry(MultiPolygon, {SRID}) AS geom
        FROM raw.tiger_county;
        ALTER TABLE core.counties ADD PRIMARY KEY (geoid);
        CREATE INDEX ON core.counties USING gist (geom);

        DROP TABLE IF EXISTS core.study_boundary CASCADE;
        CREATE TABLE core.study_boundary AS
        SELECT 1 AS id,
               ST_Multi(ST_Union(geom))::geometry(MultiPolygon, {SRID}) AS geom
        FROM core.counties;
        CREATE INDEX ON core.study_boundary USING gist (geom);
    """)
    n, km2 = sql(conn, "SELECT count(*), round((SELECT ST_Area(geom) FROM core.study_boundary)/1e6)"
                       " FROM core.counties")[0]
    log(f"core.counties: {n} rows; study boundary area {km2} km2")


if __name__ == "__main__":
    main_guard(main)
