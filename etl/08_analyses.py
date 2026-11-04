"""08 - Run the analyses in db/analysis/ and write docs/analysis-results.md.

The SQL files run in name order and each replaces its own tables in the `analysis` schema:
  01_hex_grid           500 m hexagons and one point per building
  02_building_density   analysis.hex_buildings     (analysis 1)
  03_network_metrics    analysis.hex_network       (analysis 4)
  04_transit_access     analysis.building_transit, hex_transit, transit_access_summary (2)
  05_isochrones         analysis.isochrones(lon, lat, mode, minutes[]) for the API (3)
  06_flood_exposure     analysis.flood_* and flood_summary (5)

Exits with status 1 if a check fails, such as an empty result or a 15-minute walk isochrone
slower than 3 seconds (acceptance criterion 5).
"""
import sys
import time

from common import REPO, connect, log, sql

ANALYSIS_DIR = REPO / "db" / "analysis"
ISO_TOP_MPH = 75  # must match the drive search radius in 05_isochrones.sql
ISO_LIMIT_S = 3.0

# Isochrone test points spread across the study area.
ISO_POINTS = [
    ("Downtown St. Louis", -90.1925, 38.6310),
    ("Central West End", -90.2610, 38.6440),
    ("Clayton", -90.3240, 38.6490),
    ("Florissant", -90.3224, 38.7892),
    ("Kirkwood", -90.4068, 38.5834),
]


def one(conn, q, params=None):
    return sql(conn, q, params)[0]


def table(header, rows, right=None) -> list[str]:
    right = right or [i > 0 for i in range(len(header))]
    return (["| " + " | ".join(header) + " |",
             "|" + "|".join("--:" if r else "---" for r in right) + "|"]
            + ["| " + " | ".join(str(c) for c in r) + " |" for r in rows])


def run_sql_files(conn) -> None:
    for f in sorted(ANALYSIS_DIR.glob("*.sql")):
        t = time.time()
        sql(conn, f.read_text(encoding="utf-8"))
        log(f"{f.name}: {time.time() - t:.0f}s")


def benchmark(conn) -> list[tuple]:
    rows = []
    for name, lon, lat in ISO_POINTS:
        for mode in ("walk", "drive"):
            t = time.time()
            bands = sql(conn, "SELECT band_minutes, area_km2 FROM analysis.isochrones(%s, %s, %s)",
                        (lon, lat, mode))
            secs = time.time() - t
            areas = {m: a for m, a in bands}
            rows.append((name, mode, secs, areas.get(5), areas.get(10), areas.get(15)))
            log(f"isochrone {name} {mode}: {secs:.2f}s, 15 min = {areas.get(15):.1f} km2")
    return rows


def main() -> int:
    conn = connect()
    top_mph, = one(conn, "SELECT max(speed_mph) FROM net.edges")
    if top_mph > ISO_TOP_MPH:
        log(f"FAILED: net.edges has {top_mph} mph, above the isochrone search cap {ISO_TOP_MPH}")
        return 1
    run_sql_files(conn)
    bench = benchmark(conn)

    # --- checks ---------------------------------------------------------------------------
    n_hex, = one(conn, "SELECT count(*) FROM analysis.hex")
    unassigned, = one(conn, "SELECT count(*) FROM analysis.building_pts WHERE hex_id IS NULL")
    n_bldg, = one(conn, "SELECT count(*) FROM core.buildings")
    no_net, = one(conn, "SELECT count(*) FROM analysis.building_transit WHERE net_m IS NULL")
    n_flood, = one(conn, "SELECT count(*) FROM analysis.flood_buildings")
    walk_max = max(s for _, m, s, *_ in bench if m == "walk")
    drive_max = max(s for _, m, s, *_ in bench if m == "drive")
    hex_net, = one(conn, "SELECT count(*) FROM analysis.hex_network WHERE intersections > 0")
    checks = [
        ("Every building is in a hexagon (analysis 1)", f"{unassigned} of {n_bldg:,} unassigned",
         unassigned == 0 and n_hex > 0),
        ("Network metrics found intersections (analysis 4)", f"{hex_net:,} hexes with intersections",
         hex_net > 0),
        ("Every building has a network distance to transit (analysis 2)",
         f"{no_net} missing", no_net == 0),
        (f"15-min walk isochrone under {ISO_LIMIT_S:.0f} s at every test point (analysis 3, "
         "criterion 5)", f"slowest {walk_max:.2f} s", walk_max < ISO_LIMIT_S),
        ("Flood exposure found buildings (analysis 5)", f"{n_flood:,} buildings", n_flood > 0),
    ]
    failures = sum(not ok for *_, ok in checks)

    # --- results --------------------------------------------------------------------------
    county_join = """
        JOIN LATERAL (SELECT c.namelsad FROM core.counties c
                      WHERE ST_Intersects(c.geom, ST_PointOnSurface(x.geom))
                      ORDER BY c.namelsad LIMIT 1) c ON true"""
    density = sql(conn, f"""
        SELECT c.namelsad, count(*), sum(bldg_count),
               round((sum(bldg_count) / sum(area_km2))::numeric),
               round((100 * sum(footprint_m2) / (sum(area_km2) * 1e6))::numeric, 1),
               round(percentile_cont(0.5) WITHIN GROUP (ORDER BY median_height_m)::numeric, 1)
        FROM analysis.hex_buildings x {county_join}
        GROUP BY 1 ORDER BY 1""")
    tallest = sql(conn, """
        SELECT round(p90_height_m::numeric), bldg_count,
               round(ST_Y(ST_Transform(ST_PointOnSurface(geom), 4326))::numeric, 4),
               round(ST_X(ST_Transform(ST_PointOnSurface(geom), 4326))::numeric, 4)
        FROM analysis.hex_buildings WHERE bldg_count >= 10 AND p90_height_m IS NOT NULL
        ORDER BY p90_height_m DESC LIMIT 3""")
    network = sql(conn, f"""
        SELECT c.namelsad, sum(intersections), sum(dead_ends),
               round((sum(intersections) / sum(area_km2))::numeric, 1),
               round((100.0 * sum(dead_ends) / NULLIF(sum(dead_ends) + sum(intersections), 0))
                     ::numeric, 1),
               round((sum(street_km) / sum(area_km2))::numeric, 1)
        FROM analysis.hex_network x {county_join}
        GROUP BY 1 ORDER BY 1""")
    transit = sql(conn, """
        SELECT label, buildings_straight_line, buildings_network, buildings_total,
               round(pct_straight_line, 1), round(pct_network, 1), round(pct_overstated, 1)
        FROM analysis.transit_access_summary ORDER BY meters""")
    detour, = one(conn, """
        SELECT round(percentile_cont(0.5) WITHIN GROUP (ORDER BY net_m / eucl_m)::numeric, 2)
        FROM analysis.building_transit WHERE eucl_m > 50""")
    flood = sql(conn, """
        SELECT label, buildings, round(road_miles::numeric, 1), transit_stops, transit_routes,
               round(route_miles::numeric, 1) FROM analysis.flood_summary""")

    L = ["# Analysis Results", "",
         f"Generated {time.strftime('%Y-%m-%d %H:%M')} by `etl/08_analyses.py` from the SQL in "
         "`db/analysis/`. Results are tables in the `analysis` schema.", "",
         "## Checks", ""]
    L += table(["Check", "Value", "Result"],
               [(c, v, "PASS" if ok else "**FAIL**") for c, v, ok in checks], [False] * 3)
    L += ["", f"**{len(checks) - failures} of {len(checks)} checks pass.**", "",
          "## 1. Building density and character (`analysis.hex_buildings`)", "",
          f"{n_hex:,} hexagons, 500 m across. Buildings are placed in hexes by an interior point.", ""]
    L += table(["Area", "Hexes", "Buildings", "Buildings / km²", "Footprint coverage %",
                "Median of hex median heights (m)"], density, [False] + [True] * 5)
    L += ["", "Tallest hexes (90th-percentile building height, at least 10 buildings):", ""]
    L += table(["90th pct height (m)", "Buildings", "Lat", "Lon"], tallest)
    L += ["", "## 2. Transit walk access (`analysis.transit_access_summary`)", "",
          "Buildings within walking distance of a stop with weekday service, measured two ways. "
          "Straight-line buffers count buildings the street network doesn't actually reach in that "
          "distance.", ""]
    L += table(["Distance", "Straight line", "Network walk", "All buildings", "Straight line %",
                "Network %", "Straight line overstates by %"], transit, [False] + [True] * 6)
    L += ["", f"The median building's walk to its nearest stop is **{detour}×** the straight-line "
          "distance (buildings more than 50 m from a stop).", "",
          "## 3. Isochrones (`analysis.isochrones()`)", "",
          "Computed live. Each call returns the 5, 10 and 15-minute bands; times include all "
          "three.", ""]
    L += table(["Start", "Mode", "Seconds", "5 min km²", "10 min km²", "15 min km²"],
               [(n, m, f"{s:.2f}", f"{a5:.2f}", f"{a10:.2f}", f"{a15:.2f}")
                for n, m, s, a5, a10, a15 in bench], [False, False] + [True] * 4)
    L += ["", f"Slowest walk call {walk_max:.2f} s; slowest drive call {drive_max:.2f} s.", "",
          "## 4. Street network metrics (`analysis.hex_network`)", "",
          "Streets are drivable ways except `highway=service` (driveways, parking aisles, alleys).",
          ""]
    L += table(["Area", "Intersections", "Dead ends", "Intersections / km²", "Dead-end share %",
                "Street km / km²"], network, [False] + [True] * 5)
    L += ["", "## 5. Flood exposure (`analysis.flood_summary`)", "",
          "A building is exposed if its footprint touches the zone. Road miles are drivable "
          "roads, not counting bridges (elevated over the floodplain). Transit route miles do "
          "include river crossings such as the MetroLink bridges, because GTFS route shapes "
          "carry no bridge information. The 500-yr row includes the 100-yr zone.", ""]
    L += table(["Zone", "Buildings", "Road miles", "Transit stops", "Transit routes",
                "Route miles"], flood, [False] + [True] * 5)
    L += ["", "## Method notes",
          "- **Network distance** to transit comes from one `pgr_dijkstraCost` run from a virtual "
          "source joined to every stop. Buildings snap to the nearest walkable edge and take the "
          "shorter way to either end, plus the snap offset.",
          "- **Isochrones** use `pgr_dijkstraCost` rather than `pgr_drivingDistance`, which was "
          "about 15× slower for a 15-minute drive on pgRouting 4.0.1. Bands are concave hulls "
          "(`ST_ConcaveHull`, 0.2) of the reached vertices, smoothed by 25 m (walk) or 75 m (drive).",
          "- **Drive times** are free-flow, with no traffic or turn penalties.",
          "- **Hex areas** include water (the Mississippi and Missouri rivers), so river hexes show "
          "low densities.", ""]

    out = REPO / "docs" / "analysis-results.md"
    out.write_text("\n".join(L), encoding="utf-8")
    log(f"wrote {out.relative_to(REPO)}; {failures} failing check(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    t0 = time.time()
    rc = main()
    log(f"done in {time.time() - t0:.0f}s")
    sys.exit(rc)
