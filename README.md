# GIS Portfolio

Interactive web maps and spatial analyses that show GIS analyst work.
The study area is the St. Louis metro. Data lives in PostgreSQL/PostGIS.

## Focus areas
- **Buildings:** footprint density and character by neighborhood
- **Transportation:** road network QC, transit walk access, pgRouting isochrones
- **Hydrology:** buildings and road miles inside FEMA flood zones

## Stack (planned)
- PostgreSQL + PostGIS + pgRouting
- Python ETL (GDAL/`ogr2ogr`, `osm2pgsql`)
- Tile/feature serving from PostGIS
- MapLibre GL JS front end

## Layout
| Folder | Contents |
|---|---|
| `db/` | Schema, SQL analyses, migrations |
| `etl/` | Python scripts to download, clean and load data |
| `web/` | Front-end site |
| `docs/` | Write-ups and map gallery |
| `data/` | Local raw data (git-ignored) |

## Data sources
OpenStreetMap (Geofabrik), Overture Maps / Microsoft building footprints,
Metro Transit St. Louis GTFS, FEMA NFHL, USGS NHD and 3DEP.

## Status
Early planning.
