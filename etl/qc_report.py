"""Run the data QC checks in db/qc/ and write docs/data-qc.md.

Exits with status 1 if any core table fails (no rows, invalid/empty/null geometry, wrong
SRID, or features outside the study boundary).
"""
import sys
import time

from common import REPO, SRID, connect, log, sql

# Short notes for the report: (label, query returning one value).
FACTS = [
    ("Study area", "SELECT round((ST_Area(geom)/1e6)::numeric) || ' km2' FROM core.study_boundary"),
    ("Buildings with a height value",
     "SELECT round(100.0 * count(height_m) / count(*), 1) || '%' FROM core.buildings"),
    ("Buildings by source", "SELECT string_agg(source || ' ' || n, ', ' ORDER BY n DESC) "
                            "FROM (SELECT source, count(*) n FROM core.buildings GROUP BY 1) s"),
    ("Road network length (km, all classes)",
     "SELECT round((sum(ST_Length(geom))/1000)::numeric) FROM core.roads"),
    ("Walkable / drivable ways",
     "SELECT count(*) FILTER (WHERE walkable) || ' / ' || count(*) FILTER (WHERE drivable) "
     "FROM core.roads"),
    ("GTFS feed service dates",
     "SELECT min(start_date) || ' to ' || max(end_date) FROM raw.gtfs_calendar"),
    ("Transit stops with weekday service",
     "SELECT count(*) FILTER (WHERE weekday_departures > 0) || ' of ' || count(*) "
     "FROM core.transit_stops"),
    ("Flood zone area by class (km2)",
     "SELECT string_agg(flood_class || ' ' || a, ', ' ORDER BY flood_class) FROM "
     "(SELECT flood_class, round((sum(ST_Area(geom))/1e6)::numeric, 1) a "
     "FROM core.flood_zones GROUP BY 1) s"),
]

NOTES = """\
## Notes
- **Clipping.** Lines and polygons are cut at the study boundary. Buildings are the exception:
  they're kept whole and assigned by an interior point (`ST_PointOnSurface`), so a footprint on
  the county line is counted once and never split.
- **Datum shift.** osm2pgsql (PROJ 9) had no NAD83 grid file, so OSM data was transformed with
  PROJ's fallback WGS84 to NAD83 operation, accurate to about 1 m. Other sources were
  transformed by GDAL/PostGIS. Differences are about 1 m, which is below the analyses' resolution.
- **FEMA geometry.** NFHL polygons were requested with ~1 m server-side generalization
  (`maxAllowableOffset` 0.00001 degrees) so the service would not time out on river polygons.
- **Flood classes.** FEMA's 100-yr and 500-yr zones don't overlap. "500-yr exposure" in the
  analyses means 100-yr + 500-yr. `levee` is Zone X behind a levee (accredited or not).
- **GTFS departures** count trips on one sample Wednesday, so overlapping service periods are
  not double-counted. Metro codes MetroLink as route_type 2; it's labeled `rail`.
- **NHD** came from the USGS staged HUC8 GeoPackages (the NHD map service was returning 504s).
  The load checks that the HUC8 outlines cover the whole study area.
"""


def main() -> int:
    conn = connect()
    sql(conn, (REPO / "db" / "qc" / "qc_functions.sql").read_text())
    tables = sql(conn, "SELECT tbl::text, geom_col, declared_srid, declared_type FROM qc.core_tables")

    lines = ["# Data QC", "",
             f"Generated {time.strftime('%Y-%m-%d %H:%M')} by `etl/qc_report.py` "
             "(checks in `db/qc/qc_functions.sql`).", "",
             f"Every `core` table must have rows, valid non-empty geometry in EPSG:{SRID}, and no "
             "features outside the study boundary (by interior point, 1 m tolerance).", "",
             "| Table | Rows | Geometry | SRID | Null | Empty | Invalid | Outside | Result |",
             "|---|--:|---|---|--:|--:|--:|--:|---|"]
    failures = 0
    for tbl, col, srid, gtype in tables:
        _, rows, null, empty, invalid, srids, _types, outside = sql(
            conn, "SELECT * FROM qc.check_table(%s::regclass, %s)", (tbl, col))[0]
        ok = rows > 0 and null == empty == invalid == outside == 0 and srid == SRID \
            and srids == str(SRID)
        failures += not ok
        lines.append(f"| `{tbl}` | {rows:,} | {gtype} | {srids} | {null} | {empty} | {invalid} "
                     f"| {outside} | {'PASS' if ok else '**FAIL**'} |")
        log(f"{tbl}: {rows} rows, invalid {invalid}, outside {outside} -> {'PASS' if ok else 'FAIL'}")

    lines += ["", f"**{len(tables) - failures} of {len(tables)} tables pass.**", "",
              "## Key facts", "", "| Check | Value |", "|---|---|"]
    for label, q in FACTS:
        lines.append(f"| {label} | {sql(conn, q)[0][0]} |")
    lines += ["", NOTES]

    out = REPO / "docs" / "data-qc.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    log(f"wrote {out.relative_to(REPO)}; {failures} failing table(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
