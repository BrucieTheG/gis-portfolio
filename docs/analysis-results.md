# Analysis Results

Generated 2026-11-03 18:08 by `etl/08_analyses.py` from the SQL in `db/analysis/`. Results are tables in the `analysis` schema.

## Checks

| Check | Value | Result |
|---|---|---|
| Every building is in a hexagon (analysis 1) | 0 of 500,066 unassigned | PASS |
| Network metrics found intersections (analysis 4) | 5,183 hexes with intersections | PASS |
| Every building has a network distance to transit (analysis 2) | 0 missing | PASS |
| 15-min walk isochrone under 3 s at every test point (analysis 3, criterion 5) | slowest 0.13 s | PASS |
| Flood exposure found buildings (analysis 5) | 15,947 buildings | PASS |

**5 of 5 checks pass.**

## 1. Building density and character (`analysis.hex_buildings`)

7,316 hexagons, 500 m across. Buildings are placed in hexes by an interior point.

| Area | Hexes | Buildings | Buildings / km² | Footprint coverage % | Median of hex median heights (m) |
|---|--:|--:|--:|--:|--:|
| St. Louis city | 829 | 120949 | 707 | 14.6 | 4.9 |
| St. Louis County | 6487 | 379117 | 279 | 7.3 | 4.6 |

Tallest hexes (90th-percentile building height, at least 10 buildings):

| 90th pct height (m) | Buildings | Lat | Lon |
|---|--:|--:|--:|
| 90 | 37 | 38.6271 | -90.1981 |
| 87 | 51 | 38.6314 | -90.1879 |
| 85 | 47 | 38.6269 | -90.1881 |

## 2. Transit walk access (`analysis.transit_access_summary`)

Buildings within walking distance of a stop with weekday service, measured two ways. Straight-line buffers count buildings the street network doesn't actually reach in that distance.

| Distance | Straight line | Network walk | All buildings | Straight line % | Network % | Straight line overstates by % |
|---|--:|--:|--:|--:|--:|--:|
| 1/4 mile | 212599 | 144302 | 500066 | 42.5 | 28.9 | 32.1 |
| 1/2 mile | 325775 | 260384 | 500066 | 65.1 | 52.1 | 20.1 |

The median building's walk to its nearest stop is **1.43×** the straight-line distance (buildings more than 50 m from a stop).

## 3. Isochrones (`analysis.isochrones()`)

Computed live. Each call returns the 5, 10 and 15-minute bands; times include all three.

| Start | Mode | Seconds | 5 min km² | 10 min km² | 15 min km² |
|---|---|--:|--:|--:|--:|
| Downtown St. Louis | walk | 0.13 | 0.29 | 1.29 | 3.00 |
| Downtown St. Louis | drive | 2.22 | 29.31 | 129.20 | 334.19 |
| Central West End | walk | 0.06 | 0.25 | 1.23 | 2.77 |
| Central West End | drive | 2.20 | 39.07 | 201.69 | 445.93 |
| Clayton | walk | 0.03 | 0.09 | 0.46 | 1.88 |
| Clayton | drive | 4.10 | 37.22 | 204.23 | 557.26 |
| Florissant | walk | 0.02 | 0.25 | 1.07 | 2.58 |
| Florissant | drive | 1.83 | 44.18 | 217.35 | 437.09 |
| Kirkwood | walk | 0.02 | 0.25 | 1.13 | 2.56 |
| Kirkwood | drive | 2.11 | 37.45 | 222.29 | 577.49 |

Slowest walk call 0.13 s; slowest drive call 4.10 s.

## 4. Street network metrics (`analysis.hex_network`)

Streets are drivable ways except `highway=service` (driveways, parking aisles, alleys).

| Area | Intersections | Dead ends | Intersections / km² | Dead-end share % | Street km / km² |
|---|--:|--:|--:|--:|--:|
| St. Louis city | 7998 | 876 | 46.8 | 9.9 | 11.3 |
| St. Louis County | 40419 | 5871 | 29.8 | 12.7 | 6.8 |

## 5. Flood exposure (`analysis.flood_summary`)

A building is exposed if its footprint touches the zone. Road miles are drivable roads, not counting bridges (elevated over the floodplain). Transit route miles do include river crossings such as the MetroLink bridges, because GTFS route shapes carry no bridge information. The 500-yr row includes the 100-yr zone.

| Zone | Buildings | Road miles | Transit stops | Transit routes | Route miles |
|---|--:|--:|--:|--:|--:|
| 100-yr | 6680 | 305.8 | 85 | 31 | 14.4 |
| 500-yr (includes 100-yr) | 13267 | 491.1 | 155 | 37 | 28.7 |
| Behind levee | 2680 | 410.4 | 59 | 15 | 21.2 |

## Method notes
- **Network distance** to transit comes from one `pgr_dijkstraCost` run from a virtual source joined to every stop. Buildings snap to the nearest walkable edge and take the shorter way to either end, plus the snap offset.
- **Isochrones** use `pgr_dijkstraCost` rather than `pgr_drivingDistance`, which was about 15× slower for a 15-minute drive on pgRouting 4.0.1. Bands are concave hulls (`ST_ConcaveHull`, 0.2) of the reached vertices, smoothed by 25 m (walk) or 75 m (drive).
- **Drive times** are free-flow, with no traffic or turn penalties.
- **Hex areas** include water (the Mississippi and Missouri rivers), so river hexes show low densities.
