"""Check the running web services (M5) and write docs/serving-check.md.

Start the services first (scripts/start.ps1), then in another shell:

    .venv\\Scripts\\python scripts\\m5_verify.py

Exit status 1 if any check fails. Isochrone timings are only meaningful on a fresh API start,
because the API caches responses; calls answered from the cache are marked and don't count.
"""
from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

import psycopg
import requests
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
load_dotenv(REPO / ".env")
TILES = os.getenv("TILES_URL", "http://127.0.0.1:7800")
API = os.getenv("API_URL", "http://127.0.0.1:8000")
ORIGIN = "http://localhost:5173"
TILE_LIMIT_S = 2.0
WALK_LIMIT_S = 3.0  # acceptance criterion 5

EXPECTED_LAYERS = {
    "core.study_boundary", "core.counties", "core.buildings", "core.roads", "core.rail_lines",
    "core.transit_stops", "core.transit_routes", "core.flood_zones", "core.hydro_areas",
    "core.hydro_lines", "analysis.hex_buildings", "analysis.hex_network",
    "analysis.hex_transit", "analysis.building_transit", "analysis.flood_buildings",
    "analysis.flood_roads", "analysis.flood_stops", "qc.net_dead_ends",
    "qc.net_unnoded_crossings", "qc.net_overlaps", "qc.net_islands", "qc.net_oneway_traps",
}
# Layers drawn only when zoomed in; test them at zoom 14 instead of 10.
DENSE = {"core.buildings", "core.roads", "analysis.building_transit", "analysis.flood_buildings"}
DOWNTOWN = (-90.1925, 38.6310)
FLOODPLAIN = (-90.3092, 38.6643)  # River des Peres, University City: a cluster of flood-exposed buildings
ISO_POINTS = [  # same points as etl/08_analyses.py
    ("Downtown St. Louis", -90.1925, 38.6310),
    ("Central West End", -90.2610, 38.6440),
    ("Clayton", -90.3240, 38.6490),
    ("Florissant", -90.3224, 38.7892),
    ("Kirkwood", -90.4068, 38.5834),
]


def tile_xy(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def timed_get(url: str, **kw):
    t = time.perf_counter()
    r = requests.get(url, timeout=60, **kw)
    return r, time.perf_counter() - t


def table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    return out + ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]


def role_is_read_only(role: str, var: str) -> tuple[bool, str]:
    """The role can't read raw data or create anything."""
    conn = psycopg.connect(host=os.getenv("PGHOST", "localhost"), port=os.getenv("PGPORT", "5432"),
                           dbname=os.getenv("PGDATABASE", "stl_gis"), user=role,
                           password=os.getenv(var), autocommit=True)
    denied = []
    for stmt in ("SELECT 1 FROM raw.roads LIMIT 1", "CREATE TABLE public.m5_probe (x int)",
                 "DELETE FROM core.study_boundary WHERE false"):
        try:
            conn.execute(stmt)
        except psycopg.errors.InsufficientPrivilege:
            denied.append(True)
        except psycopg.errors.UndefinedTable:
            denied.append(True)  # can't even see it
        else:
            denied.append(False)
    conn.close()
    return all(denied), f"{sum(denied)} of {len(denied)} write/raw probes denied"


def main() -> int:
    checks: list[tuple[str, str, bool]] = []
    L: list[str] = []

    # --- pg_tileserv ----------------------------------------------------------------------
    index, _ = timed_get(f"{TILES}/index.json")
    published = set(index.json())
    missing, extra = EXPECTED_LAYERS - published, published - EXPECTED_LAYERS
    checks.append(("pg_tileserv publishes exactly the planned layers",
                   f"{len(published & EXPECTED_LAYERS)} of {len(EXPECTED_LAYERS)}; "
                   f"missing {sorted(missing) or 'none'}, extra {sorted(extra) or 'none'}",
                   not missing and not extra))

    rows, slow, empty = [], [], []
    for layer in sorted(published & EXPECTED_LAYERS):
        z = 14 if layer in DENSE else 10
        lon, lat = FLOODPLAIN if layer.startswith("analysis.flood") and z == 14 else DOWNTOWN
        x, y = tile_xy(lon, lat, z)
        r, s = timed_get(f"{TILES}/{layer}/{z}/{x}/{y}.pbf")
        if r.status_code != 200 or not r.content:
            empty.append(layer)
        if s > TILE_LIMIT_S:
            slow.append(layer)
        rows.append((f"`{layer}`", f"{z}/{x}/{y}", r.status_code, f"{len(r.content) / 1024:,.0f}",
                     f"{s:.2f}"))
    checks.append(("Every layer returns a non-empty tile", f"{len(rows) - len(empty)} of {len(rows)}"
                   + (f" (empty: {', '.join(empty)})" if empty else ""), not empty))
    checks.append((f"Every sample tile renders in under {TILE_LIMIT_S:.0f} s",
                   f"slowest {max(float(r[4]) for r in rows):.2f} s", not slow))
    cors = requests.get(f"{TILES}/index.json", headers={"Origin": ORIGIN}, timeout=10).headers
    checks.append(("pg_tileserv allows the website origin (CORS)",
                   cors.get("Access-Control-Allow-Origin", "none"),
                   cors.get("Access-Control-Allow-Origin") in (ORIGIN, "*")))
    tile_rows = rows

    # --- API ------------------------------------------------------------------------------
    health, _ = timed_get(f"{API}/health")
    checks.append(("API health", f"{health.status_code} {health.json().get('status')}",
                   health.ok and health.json().get("status") == "ok"))

    flood = requests.get(f"{API}/stats/flood", timeout=30).json()["zones"]
    zones = {z["zone"]: z for z in flood}
    has_both = any(k.startswith("100") for k in zones) and any(k.startswith("500") for k in zones)
    checks.append(("/stats/flood has buildings and road miles for the 100-yr and 500-yr zones "
                   "(criterion 7)", ", ".join(f"{z['zone']}: {z['buildings']:,} bldgs, "
                                              f"{z['road_miles']} mi" for z in flood),
                   has_both and all(z["buildings"] > 0 and z["road_miles"] > 0 for z in flood)))

    transit = requests.get(f"{API}/stats/transit-access", timeout=60).json()
    ok = all(d["straight_line"]["pct"] > d["network"]["pct"] > 0 for d in transit["distances"])
    checks.append(("/stats/transit-access has both measures and the difference (criterion 6)",
                   "; ".join(f"{d['label']}: {d['straight_line']['pct']}% vs. "
                             f"{d['network']['pct']}% (+{d['pct_overstated']}%)"
                             for d in transit["distances"]), ok))

    iso_rows, walk_times, slow_walk = [], [], []
    for name, lon, lat in ISO_POINTS:
        for mode in ("walk", "drive"):
            r, s = timed_get(f"{API}/isochrone", params={"lon": lon, "lat": lat, "mode": mode,
                                                        "minutes": "5,10,15"})
            d = r.json()
            areas = {f["properties"]["minutes"]: f["properties"]["area_km2"] for f in d["features"]}
            valid = (r.ok and len(d["features"]) == 3 and
                     all(f["geometry"]["type"] in ("Polygon", "MultiPolygon") for f in d["features"])
                     and areas[5] < areas[10] < areas[15])
            if mode == "walk" and not d["cached"]:
                walk_times.append(s)
                if s > WALK_LIMIT_S or not valid:
                    slow_walk.append(name)
            iso_rows.append((name, mode, f"{s:.2f}", "yes" if d["cached"] else "no",
                             d["snap_distance_m"], areas[5], areas[10], areas[15],
                             "ok" if valid else "BAD"))
    checks.append(("Isochrones return 3 nested polygons at every test point",
                   f"{sum(r[-1] == 'ok' for r in iso_rows)} of {len(iso_rows)}",
                   all(r[-1] == "ok" for r in iso_rows)))
    checks.append((f"15-min walk isochrone over HTTP in under {WALK_LIMIT_S:.0f} s (criterion 5)",
                   f"slowest {max(walk_times):.2f} s over {len(walk_times)} uncached calls"
                   if walk_times else "every call was cached; restart the API and rerun",
                   bool(walk_times) and not slow_walk))
    again, s = timed_get(f"{API}/isochrone", params={"lon": ISO_POINTS[2][1],
                                                    "lat": ISO_POINTS[2][2], "mode": "drive"})
    checks.append(("A repeated drive request is served from the cache",
                   f"cached={again.json()['cached']}, {s * 1000:.0f} ms",
                   again.json()["cached"] and s < 0.5))

    bad = [("outside the study area", {"lon": -91.5, "lat": 38.6}, 400),
           ("unknown mode", {"lon": -90.3, "lat": 38.6, "mode": "bike"}, 422),
           ("drive longer than 30 min", {"lon": -90.3, "lat": 38.6, "mode": "drive",
                                         "minutes": "45"}, 422),
           ("non-numeric minutes", {"lon": -90.3, "lat": 38.6, "minutes": "five"}, 422)]
    codes = [(label, requests.get(f"{API}/isochrone", params=p, timeout=30).status_code, want)
             for label, p, want in bad]
    checks.append(("Bad isochrone requests get a clear 4xx error",
                   ", ".join(f"{l}: {c}" for l, c, _ in codes), all(c == w for _, c, w in codes)))
    cors = requests.get(f"{API}/health", headers={"Origin": ORIGIN}, timeout=10).headers
    checks.append(("API allows the website origin (CORS)",
                   cors.get("access-control-allow-origin", "none"),
                   cors.get("access-control-allow-origin") == ORIGIN))

    for role, var in (("tiles_reader", "TILES_DB_PASSWORD"), ("api_reader", "API_DB_PASSWORD")):
        ok, detail = role_is_read_only(role, var)
        checks.append((f"`{role}` is read-only and can't see raw data", detail, ok))

    # --- report ---------------------------------------------------------------------------
    passed = sum(c[2] for c in checks)
    L += ["# Serving Check", "",
          f"Generated {time.strftime('%Y-%m-%d %H:%M')} by `scripts/m5_verify.py` against the "
          f"services started by `scripts/start.ps1` (pg_tileserv at {TILES}, API at {API}).", "",
          "## Checks", ""]
    L += table(["Check", "Value", "Result"], [(n, v, "PASS" if ok else "FAIL") for n, v, ok in checks])
    L += ["", f"**{passed} of {len(checks)} checks pass.**", "",
          "## Vector tile layers (pg_tileserv)", "",
          "One sample tile per layer over downtown St. Louis (flood buildings over the River des Peres "
          "in University City). Dense layers are tested at zoom 14, where the web maps start drawing them.",
          ""]
    L += table(["Layer", "Tile z/x/y", "HTTP", "KB", "Seconds"], tile_rows)
    L += ["", "Tile URL pattern: `http://localhost:7800/{layer}/{z}/{x}/{y}.pbf`. "
          "Layer metadata (properties, bounds) is at `/{layer}.json`.", "",
          "## Isochrones (API)", "",
          "`GET /isochrone?lon=..&lat=..&mode=walk|drive&minutes=5,10,15`. Seconds are measured "
          "by this script over HTTP and include JSON encoding. The API caches the last "
          "256 responses by snapped start vertex.", ""]
    L += table(["Start", "Mode", "Seconds", "Cached", "Snap m", "5 min km²", "10 min km²",
                "15 min km²", "Shape"], iso_rows)
    L += ["", "## Endpoints", "",
          "| Endpoint | Returns |", "|---|---|",
          "| `GET /isochrone` | GeoJSON FeatureCollection, one polygon per band (largest first), "
          "plus the snapped `start` point |",
          "| `GET /stats/flood` | Buildings, road miles and transit exposed per FEMA zone |",
          "| `GET /stats/transit-access` | Straight-line vs. network access at 1/4 and 1/2 mile, "
          "and the median detour ratio |",
          "| `GET /health` | `{\"status\": \"ok\"}` when the database answers |",
          "| `GET /docs` | Interactive API docs (FastAPI) |", ""]
    out = REPO / "docs" / "serving-check.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"wrote {out.relative_to(REPO)}; {passed} of {len(checks)} checks pass")
    for n, v, ok in checks:
        if not ok:
            print(f"  FAIL: {n}: {v}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
