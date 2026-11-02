"""Shared helpers for the ETL scripts: settings, DB access, downloads, ogr2ogr.

Every script follows the same pattern: download into data/raw/ (cached), load into
the `raw` schema, then clean and clip into the `core` schema in EPSG:26915.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import psycopg
import requests
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
RAW_DIR = REPO / "data" / "raw"
RAW_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(REPO / ".env")

SRID = 26915  # NAD83 / UTM zone 15N, meters
STATE_FIPS = "29"
COUNTY_FIPS = ("510", "189")  # St. Louis City, St. Louis County

PG_BIN = Path(os.getenv("PG_BIN", r"C:\Program Files\PostgreSQL\18\bin"))
OGR2OGR = str(PG_BIN / "ogr2ogr.exe")
PSQL = str(PG_BIN / "psql.exe")
OSM2PGSQL = os.getenv("OSM2PGSQL", r"A:\gis-tools\osm2pgsql\osm2pgsql-bin\osm2pgsql.exe")

# GDAL bundled with PostGIS needs to be told where its support files are.
_pg_root = PG_BIN.parent
os.environ.setdefault("GDAL_DATA", str(_pg_root / "gdal-data"))
_proj = _pg_root / "share" / "contrib" / "postgis-3.6" / "proj"
os.environ.setdefault("PROJ_DATA", str(_proj))
os.environ.setdefault("PROJ_LIB", str(_proj))

USER_AGENT = "gis-portfolio-etl/0.1 (+https://github.com/BrucieTheG/gis-portfolio)"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def connect() -> psycopg.Connection:
    """Connect with the PG* settings from .env (libpq reads them from the environment)."""
    return psycopg.connect(autocommit=True)


def sql(conn: psycopg.Connection, statement: str, params=None):
    with conn.cursor() as cur:
        cur.execute(statement, params)
        return cur.fetchall() if cur.description else None


def ensure_schemas(conn: psycopg.Connection) -> None:
    sql(conn, "CREATE SCHEMA IF NOT EXISTS raw; CREATE SCHEMA IF NOT EXISTS core;")


BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"  # some sites 403 non-browser agents


def download(url: str, dest: Path, max_age_days: float | None = None,
             user_agent: str = USER_AGENT) -> Path:
    """Download url to dest unless a cached copy exists (and is fresh enough)."""
    if dest.exists() and dest.stat().st_size > 0:
        age = (time.time() - dest.stat().st_mtime) / 86400
        if max_age_days is None or age < max_age_days:
            log(f"cached: {dest.name} ({dest.stat().st_size / 1e6:.1f} MB)")
            return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"downloading {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=120, headers={"User-Agent": user_agent}) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    tmp.replace(dest)
    log(f"saved {dest.name} ({dest.stat().st_size / 1e6:.1f} MB)")
    return dest


def run(cmd: list[str], env: dict | None = None) -> None:
    log("run: " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    subprocess.run(cmd, check=True, env=env)


def osm2pgsql_env() -> dict:
    """osm2pgsql ships PROJ 9 with its own proj.db; the PostGIS GDAL one (PROJ 8) breaks it."""
    env = dict(os.environ)
    proj = str(Path(OSM2PGSQL).parent)
    env["PROJ_DATA"] = env["PROJ_LIB"] = proj
    return env


def ogr_to_pg(src: str, table: str, *extra: str, layer: str | None = None,
              append: bool = False) -> None:
    """Load a vector source into raw.<table>. Geometry column is `geom`.

    Replaces the table, or with append=True adds rows to the table an earlier call created
    (same source schema). The GDAL bundled with PostGIS on Windows has no PostgreSQL driver,
    so this writes a PGDUMP (SQL + COPY) stream to stdout and pipes it into psql.
    """
    table_opts = (["-lco", "CREATE_TABLE=OFF", "-lco", "DROP_TABLE=OFF",
                   "-lco", "SPATIAL_INDEX=NONE", "-unsetFid"] if append else
                  ["-lco", "DROP_TABLE=IF_EXISTS", "-lco", "SPATIAL_INDEX=GIST"])
    cmd = [
        OGR2OGR, "-f", "PGDUMP", "/vsistdout/", src,
        "-nln", table,
        "-lco", "SCHEMA=raw", "-lco", "CREATE_SCHEMA=OFF", *table_opts,
        "-lco", "GEOMETRY_NAME=geom", "-lco", "FID=ogc_fid",
        "-nlt", "PROMOTE_TO_MULTI",
        *extra,
    ]
    if layer:
        cmd.append(layer)
    log("run: " + " ".join(f'"{c}"' if " " in c else c for c in cmd) + " | psql")
    ogr = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    psql = subprocess.run([PSQL, "-q", "-X", "-v", "ON_ERROR_STOP=1"],
                          stdin=ogr.stdout, stdout=subprocess.DEVNULL)
    ogr.stdout.close()
    if ogr.wait() != 0 or psql.returncode != 0:
        raise subprocess.CalledProcessError(ogr.returncode or psql.returncode, cmd)


def study_bbox_4326(conn: psycopg.Connection, buffer_m: float = 0) -> tuple[float, float, float, float]:
    """(xmin, ymin, xmax, ymax) of the study boundary in lon/lat. Needs 01_boundaries first."""
    row = sql(conn, f"""
        SELECT ST_XMin(b), ST_YMin(b), ST_XMax(b), ST_YMax(b)
        FROM (SELECT ST_Transform(ST_Envelope(ST_Buffer(geom, %s)), 4326)::box2d AS b
              FROM core.study_boundary) s""", (buffer_m,))[0]
    return tuple(float(v) for v in row)


def _arcgis_post(url: str, params: dict, tries: int) -> dict:
    last = None
    for i in range(tries):
        try:
            r = requests.post(url, data=params, timeout=300, headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            d = r.json()
            if "error" in d:
                raise RuntimeError(d["error"])
            return d
        except Exception as e:  # network errors and ArcGIS JSON errors alike
            last = e
            if i + 1 < tries:
                time.sleep(5 * (i + 1))
    raise RuntimeError(f"{url} kept failing: {str(last)[:200]}")


def arcgis_fetch(layer_url: str, fields: str, cache: Path, bbox_4326: tuple,
                 where: str = "1=1", chunk: int = 200, max_offset: float | None = 0.00001) -> Path:
    """Download every feature of an ArcGIS MapServer layer that intersects bbox_4326.

    Gets the object IDs first, then features in chunks saved as GeoJSON in `cache` (reused on
    reruns). A chunk that keeps failing - usually huge polygons timing out the server - is split
    in half down to single features. max_offset generalizes geometry server-side (degrees;
    0.00001 is about 1 m). Returns one merged GeoJSON file.
    """
    import json

    query = layer_url.rstrip("/") + "/query"
    cache.mkdir(parents=True, exist_ok=True)
    merged = cache / "merged.geojson"
    xmin, ymin, xmax, ymax = bbox_4326
    ids = _arcgis_post(query, {"geometry": f"{xmin},{ymin},{xmax},{ymax}",
                               "geometryType": "esriGeometryEnvelope", "inSR": 4326,
                               "spatialRel": "esriSpatialRelIntersects", "where": where,
                               "returnIdsOnly": "true", "f": "json"}, 5).get("objectIds") or []
    ids.sort()
    n_chunks = -(-len(ids) // chunk)
    log(f"{len(ids)} features in bbox from {layer_url} ({n_chunks} chunks)")

    def fetch(batch: list[int]) -> list:
        params = {"objectIds": ",".join(map(str, batch)), "outFields": fields, "outSR": 4326,
                  "returnGeometry": "true", "geometryPrecision": 6, "f": "geojson"}
        if max_offset:
            params["maxAllowableOffset"] = max_offset
        try:
            return _arcgis_post(query, params, 2 if len(batch) > 1 else 5)["features"]
        except RuntimeError:
            if len(batch) == 1:
                raise
            log(f"  splitting a batch of {len(batch)}")
            mid = len(batch) // 2
            return fetch(batch[:mid]) + fetch(batch[mid:])

    features = []
    for n, i in enumerate(range(0, len(ids), chunk)):
        f = cache / f"chunk_{n:03d}.geojson"
        if not f.exists():
            f.write_text(json.dumps({"type": "FeatureCollection",
                                     "features": fetch(ids[i:i + chunk])}))
            if n % 10 == 0 or n + 1 == n_chunks:
                log(f"  chunk {n + 1}/{n_chunks}")
        features += json.loads(f.read_text())["features"]
    merged.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    log(f"merged {len(features)} features -> {merged.relative_to(REPO)}")
    return merged


def main_guard(fn) -> None:
    try:
        t = time.time()
        fn()
        log(f"done in {time.time() - t:.0f}s")
    except subprocess.CalledProcessError as e:
        log(f"FAILED: {e}")
        sys.exit(1)
