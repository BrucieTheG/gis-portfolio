# Data QC

Generated 2026-11-02 14:00 by `etl/qc_report.py` (checks in `db/qc/qc_functions.sql`).

Every `core` table must have rows, valid non-empty geometry in EPSG:26915, and no features outside the study boundary (by interior point, 1 m tolerance).

| Table | Rows | Geometry | SRID | Null | Empty | Invalid | Outside | Result |
|---|--:|---|---|--:|--:|--:|--:|---|
| `core.buildings` | 500,066 | MULTIPOLYGON | 26915 | 0 | 0 | 0 | 0 | PASS |
| `core.counties` | 2 | MULTIPOLYGON | 26915 | 0 | 0 | 0 | 0 | PASS |
| `core.flood_zones` | 6,386 | POLYGON | 26915 | 0 | 0 | 0 | 0 | PASS |
| `core.hydro_areas` | 972 | POLYGON | 26915 | 0 | 0 | 0 | 0 | PASS |
| `core.hydro_lines` | 2,102 | LINESTRING | 26915 | 0 | 0 | 0 | 0 | PASS |
| `core.osm_transit` | 3,044 | POINT | 26915 | 0 | 0 | 0 | 0 | PASS |
| `core.rail_lines` | 1,719 | LINESTRING | 26915 | 0 | 0 | 0 | 0 | PASS |
| `core.roads` | 198,208 | LINESTRING | 26915 | 0 | 0 | 0 | 0 | PASS |
| `core.study_boundary` | 1 | MULTIPOLYGON | 26915 | 0 | 0 | 0 | 0 | PASS |
| `core.transit_routes` | 45 | MULTILINESTRING | 26915 | 0 | 0 | 0 | 0 | PASS |
| `core.transit_stops` | 3,716 | POINT | 26915 | 0 | 0 | 0 | 0 | PASS |

**11 of 11 tables pass.**

## Key facts

| Check | Value |
|---|---|
| Study area | 1528 km2 |
| Buildings with a height value | 94.3% |
| Buildings by source | Microsoft ML Buildings 395383, OpenStreetMap 104683 |
| Road network length (km, all classes) | 25867 |
| Walkable / drivable ways | 193429 / 113992 |
| GTFS feed service dates | 20261102 to 20261129 |
| Transit stops with weekday service | 3716 of 3716 |
| Flood zone area by class (km2) | 100-yr 232.7, 500-yr 27.9, levee 53.0 |

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
