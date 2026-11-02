# Local setup (Windows)

This is how the development environment was built for Milestone 1. It uses native Windows installers, not Docker.
Versions were confirmed on 2026-11-02 with `scripts/m1-verify.ps1`.

## Confirmed versions
| Component | Version | Location |
|---|---|---|
| PostgreSQL | 18.6 (x86_64-windows, msvc) | `C:\Program Files\PostgreSQL\18`, data dir `A:\PostGRE_Data` |
| PostGIS | 3.6.2 (GEOS 3.14.1dev, PROJ 8.2.1) | Stack Builder bundle |
| pgRouting | 4.0.1 | Stack Builder bundle |
| GDAL / ogr2ogr | 3.9.2 (no PostgreSQL driver; ETL writes PGDUMP into `psql`) | `C:\Program Files\PostgreSQL\18\bin` (not on `PATH`) |
| osm2pgrouting | bundled with PostGIS | `C:\Program Files\PostgreSQL\18\bin` |
| osm2pgsql | 2.3.1 (Libosmium 2.23.1, PROJ 9.8.1, Lua 5.5.0) | `A:\gis-tools\osm2pgsql\osm2pgsql-bin\` |
| pg_tileserv | "latest" snapshot build, dated 2025-12-11 (it reports no version number) | `A:\gis-tools\pg_tileserv\` |
| Python packages | fastapi 0.162.0, psycopg 3.3.6, uvicorn 0.54.0, python-dotenv 1.2.4, requests 2.34.2, overturemaps 1.0.2, pyarrow 26.0.0 | `.venv\` |

## Steps
1. **PostgreSQL 18:** install it with the EDB Windows installer. Put the data directory at `A:\PostGRE_Data` and use port 5432.
   The service name is `postgresql-x64-18`.
2. **PostGIS bundle:** in Stack Builder, choose Spatial Extensions → PostGIS 3.6 bundle. It includes pgRouting and GDAL/ogr2ogr.
3. **osm2pgsql and pg_tileserv:** download the Windows zips and unzip them into `A:\gis-tools\`.
4. **Python environment:**
   ```powershell
   cd A:\gis-portfolio
   py -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   ```
5. **Connection settings:** copy `.env.example` to `.env` and set `PGPASSWORD`. Git ignores `.env`, so never commit it.
6. **Database and extensions:** run the verify script. It checks the tool versions, creates `stl_gis` if it's missing, and enables
   `postgis` and `pgrouting`. You can run it again safely.
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\m1-verify.ps1
   ```
   It saves a report to `data\m1-verify.txt` (git-ignored). The password is never printed.
7. **Load the data:** this downloads every source into `data\raw\` (about 600 MB, cached) and builds the
   `raw` and `core` schemas, then writes `docs\data-qc.md`. A first run takes about 15 minutes, mostly the FEMA
   service. Rerunning is safe.
   ```powershell
   .venv\Scripts\python etl\run_all.py
   ```

## Quirks of the bundled tools
- The PostGIS bundle's GDAL has no PostgreSQL driver and can't read snappy-compressed Parquet. `etl/common.py`
  pipes `ogr2ogr -f PGDUMP` into `psql`, and `etl/03_buildings.py` rewrites the Overture file uncompressed.
- That GDAL uses PROJ 8 (`share\contrib\postgis-3.6\proj`). osm2pgsql ships PROJ 9 with its own `proj.db`.
  `etl/common.py` sets `PROJ_DATA` separately for each tool.
- metrostlouis.org returns 403 to non-browser user agents, so the GTFS download sends a browser one.

## Conventions
- Study area: St. Louis City + St. Louis County (FIPS 29510, 29189).
- Analysis CRS: EPSG:26915 (NAD83 / UTM 15N).
- Keep all project data on `A:`. `B:` is a slow HDD.
