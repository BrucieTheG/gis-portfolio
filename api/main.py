"""FastAPI app for the analyses the map pages can't get from vector tiles (M5).

    GET /health                 database reachable?
    GET /isochrone              walk or drive time bands from a point, as GeoJSON
    GET /stats/flood            flood exposure summary (analysis 5)
    GET /stats/transit-access   straight-line vs. network transit access (analysis 2)

Map layers come from pg_tileserv (port 7800), not from here. Run with scripts/start.ps1, or:

    .venv\\Scripts\\python -m uvicorn api.main:app --port 8000

The app connects as the read-only api_reader role (API_DB_PASSWORD in .env, created by
etl/09_web_roles.py). Interactive docs are at http://localhost:8000/docs.
"""
from __future__ import annotations

import json
import os
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from psycopg_pool import ConnectionPool

REPO = Path(__file__).resolve().parent.parent
load_dotenv(REPO / ".env")

# Longest band allowed per mode. A 30-minute drive already reaches most of the study area.
MAX_MINUTES = {"walk": 60, "drive": 30}
MAX_BANDS = 6
CACHE_SIZE = 256
ORIGINS = os.getenv("WEB_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")


def _conninfo() -> str:
    password = os.getenv("API_DB_PASSWORD")
    if not password:
        raise RuntimeError("API_DB_PASSWORD is not set; run etl/09_web_roles.py first")
    parts = {"host": os.getenv("PGHOST", "localhost"), "port": os.getenv("PGPORT", "5432"),
             "dbname": os.getenv("PGDATABASE", "stl_gis"), "user": "api_reader",
             "password": password, "application_name": "gis-portfolio-api"}
    return " ".join(f"{k}='{v}'" for k, v in parts.items())


pool: ConnectionPool | None = None
_static: dict = {}  # summary numbers; they only change when the ETL reruns


@asynccontextmanager
async def lifespan(_: FastAPI):
    global pool
    pool = ConnectionPool(_conninfo(), min_size=1, max_size=6, open=True,
                          kwargs={"autocommit": True})
    try:
        yield
    finally:
        pool.close()


app = FastAPI(title="St. Louis GIS portfolio API", version="0.5.0", lifespan=lifespan,
              description="Live isochrones and summary statistics for the St. Louis City + "
                          "County GIS portfolio. Map layers are served by pg_tileserv.")
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=["GET"],
                   allow_headers=["*"])


def query(statement: str, params=None) -> list[tuple]:
    assert pool is not None
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(statement, params)
        return cur.fetchall()


def study_bbox() -> tuple[float, float, float, float]:
    if "bbox" not in _static:
        row = query("""
            SELECT ST_XMin(b), ST_YMin(b), ST_XMax(b), ST_YMax(b)
            FROM (SELECT ST_Transform(geom, 4326)::box2d AS b FROM core.study_boundary) s""")[0]
        _static["bbox"] = tuple(float(v) for v in row)
    return _static["bbox"]


# --- isochrones ---------------------------------------------------------------------------
class _LRU(OrderedDict):
    """A small thread-safe cache of isochrone responses, keyed by snapped start vertex."""

    def __init__(self, size: int):
        super().__init__()
        self.size, self.lock = size, Lock()

    def get_item(self, key):
        with self.lock:
            if key in self:
                self.move_to_end(key)
                return self[key]
        return None

    def put(self, key, value):
        with self.lock:
            self[key] = value
            self.move_to_end(key)
            while len(self) > self.size:
                self.popitem(last=False)


_iso_cache = _LRU(CACHE_SIZE)


def _parse_minutes(text: str, mode: str) -> list[int]:
    try:
        minutes = sorted({int(m) for m in text.split(",") if m.strip()})
    except ValueError:
        raise HTTPException(422, "minutes must be a comma-separated list of whole numbers")
    if not minutes or len(minutes) > MAX_BANDS:
        raise HTTPException(422, f"give between 1 and {MAX_BANDS} bands")
    if minutes[0] < 1 or minutes[-1] > MAX_MINUTES[mode]:
        raise HTTPException(422, f"{mode} bands must be 1 to {MAX_MINUTES[mode]} minutes")
    return minutes


@app.get("/isochrone", summary="Walk or drive time bands from a point")
def isochrone(lon: float = Query(..., description="Start longitude (WGS 84)"),
              lat: float = Query(..., description="Start latitude (WGS 84)"),
              mode: Literal["walk", "drive"] = "walk",
              minutes: str = Query("5,10,15", description="Comma-separated band limits")):
    """Returns a GeoJSON FeatureCollection with one polygon per band, largest first so the
    smaller bands draw on top. The start snaps to the nearest vertex of the main walk or drive
    network; `start` gives that snapped point. Drive times are free-flow (no traffic)."""
    bands = _parse_minutes(minutes, mode)
    xmin, ymin, xmax, ymax = study_bbox()
    if not (xmin <= lon <= xmax and ymin <= lat <= ymax):
        raise HTTPException(400, "start point is outside St. Louis City + County")

    t = time.perf_counter()
    snap = query("""
        SELECT n.id, ST_X(g), ST_Y(g), ST_Distance(n.geom, p.geom)
        FROM (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 26915) AS geom) p
        CROSS JOIN LATERAL (
            SELECT id, geom FROM net.vertices v
            WHERE CASE WHEN %s = 'walk' THEN v.in_walk_main ELSE v.in_drive_main END
            ORDER BY v.geom <-> p.geom LIMIT 1) n
        CROSS JOIN LATERAL ST_Transform(n.geom, 4326) g""", (lon, lat, mode))[0]
    vertex, slon, slat, snap_m = snap
    key = (mode, vertex, tuple(bands))
    cached = _iso_cache.get_item(key)
    if cached is None:
        rows = query("""
            SELECT band_minutes, reached_vertices, area_km2, ST_AsGeoJSON(geom, 6)
            FROM analysis.isochrones(%s, %s, %s, %s) ORDER BY band_minutes DESC""",
                     (slon, slat, mode, bands))
        features = [{"type": "Feature", "geometry": json.loads(g),
                     "properties": {"mode": mode, "minutes": m, "area_km2": round(a, 3),
                                    "reached_vertices": n}}
                    for m, n, a, g in rows]
        cached = features
        _iso_cache.put(key, features)
        from_cache = False
    else:
        from_cache = True
    return {"type": "FeatureCollection", "features": cached,
            "start": {"type": "Point", "coordinates": [round(slon, 6), round(slat, 6)]},
            "snap_distance_m": round(snap_m, 1), "mode": mode, "minutes": bands,
            "elapsed_ms": round((time.perf_counter() - t) * 1000), "cached": from_cache}


# --- summary statistics -------------------------------------------------------------------
@app.get("/stats/flood", summary="Flood exposure summary (analysis 5)")
def stats_flood():
    """Buildings, road miles and transit exposed in each FEMA zone. Road miles leave out
    bridges; the 500-yr row includes the 100-yr zone."""
    if "flood" not in _static:
        rows = query("""
            SELECT label, buildings, road_miles, transit_stops, transit_routes, route_miles
            FROM analysis.flood_summary ORDER BY sort""")
        _static["flood"] = {"zones": [
            {"zone": z, "buildings": b, "road_miles": round(r, 1), "transit_stops": s,
             "transit_routes": n, "transit_route_miles": round(m, 1)}
            for z, b, r, s, n, m in rows]}
    return _static["flood"]


@app.get("/stats/transit-access", summary="Transit walk access, two ways (analysis 2)")
def stats_transit_access():
    """Share of buildings within 1/4 and 1/2 mile of a stop with weekday service, measured by
    straight-line buffer and by walking the street network, and how much the buffer overstates."""
    if "transit" not in _static:
        rows = query("""
            SELECT label, meters, buildings_straight_line, buildings_network, buildings_total,
                   pct_straight_line, pct_network, pct_overstated
            FROM analysis.transit_access_summary ORDER BY meters""")
        detour = query("""
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY net_m / eucl_m)
            FROM analysis.building_transit WHERE eucl_m > 50""")[0][0]
        _static["transit"] = {
            "distances": [
                {"label": l, "meters": round(float(m), 1), "buildings_total": t,
                 "straight_line": {"buildings": s, "pct": round(float(ps), 1)},
                 "network": {"buildings": n, "pct": round(float(pn), 1)},
                 "pct_overstated": round(float(po), 1)}
                for l, m, s, n, t, ps, pn, po in rows],
            "median_detour_ratio": round(float(detour), 2)}
    return _static["transit"]


@app.get("/health", summary="Is the database reachable?")
def health():
    try:
        ok = query("SELECT 1")[0][0] == 1
    except Exception as e:  # report, don't crash the health check
        raise HTTPException(503, f"database unavailable: {type(e).__name__}")
    return {"status": "ok" if ok else "error"}
