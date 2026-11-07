# Road Network QC

Generated 2026-11-07 00:30 by `etl/network_qc.py` for the network built by `etl/07_network.py` (schema `net`).

## Checks

| Check | Value | Result |
|---|---|---|
| Every edge has a source and target vertex | 0 unwired | PASS |
| Splitting kept the road length (within 0.01%) | 25,866.9 km in, 25,866.9 km out | PASS |
| Main walk component holds at least 95% of walkable length | 98.5% | PASS |
| Main drive component (strongly connected) holds at least 95% of drivable length | 98.6% | PASS |

**4 of 4 checks pass.**

## Network summary

| Measure | Value |
|---|---|
| Edges / vertices | 457,247 / 328,803 |
| Junctions (3+ edge ends) | 230,750 |
| Walkable length | 24,830 km |
| Drivable length | 17,217 km (2,823 km one-way) |
| Drivable length with a posted speed limit | 16.5% (the rest use class defaults) |
| Walk main component | 98.5% of walkable length |
| Drive main component (strongly connected) | 98.6% of drivable length |

## Findings for review

These are not failures. They're the kind of issues a data-production QC pass flags for an editor to confirm or fix. Each is a table in the `qc` schema so it can be mapped.

### Dead ends (`qc.net_dead_ends`): 40,309

141 are within 5 m of the study boundary, where clipping cut the way, and are left out below. A *near miss* is a dead end within 3 m of an edge it doesn't connect to: a likely undershoot.

| Class of the dead-end edge | Dead ends | Near miss ≤ 1 m | Near miss 1–3 m |
|---|--:|--:|--:|
| service | 22043 | 33 | 249 |
| path | 13070 | 277 | 1373 |
| local | 4876 | 5 | 25 |
| track | 174 | 0 | 0 |
| major road (tertiary) | 5 | 0 | 0 |

Most local and service dead ends are real cul-de-sacs and driveways. Dead ends on major roads are rarer and worth a look:

| Name | Highway | Lat | Lon | Gap to nearest edge (m) |
|---|---|--:|--:|---|
| College Avenue | tertiary | 38.72295 | -90.26442 | - |
| East Watson Road | tertiary | 38.55679 | -90.38465 | - |
| North Outer 40 | tertiary | 38.66791 | -90.57623 | - |
| Prichard Farm Road | tertiary | 38.74105 | -90.46921 | - |
| Prichard Farm Road | tertiary | 38.74035 | -90.46884 | - |

### Crossings with no shared node (`qc.net_unnoded_crossings`): 56

Two edges on the same layer, neither tagged bridge or tunnel, cross without a junction. Either the junction node is missing, or a bridge/tunnel/layer tag is.

| Kind | Crossings |
|---|--:|
| path x path | 26 |
| path x road | 19 |
| road x road | 11 |

### Duplicated ways (`qc.net_overlaps`): 62

Different ways drawn on top of each other for more than 1 m (0.7 km in total).

### Islands (`qc.net_islands`)

Connected pieces outside the main network. Routing can't reach them, so the isochrone tool snaps clicks to the main component.

| Mode | Islands | Total km | Islands ≥ 1 km | Largest (km) |
|---|--:|--:|--:|--:|
| drive | 968 | 174.5 | 25 | 4.43 |
| walk | 1162 | 383.5 | 68 | 16.12 |

### One-way traps (`qc.net_oneway_traps`): 784

Drive vertices joined to the main network that you can drive into but not out of (or the reverse), usually from a one-way tag pointing the wrong way or a gated exit.

## Method notes
- **Noding follows OSM topology.** Ways are split only at vertices they share with another way (an OSM node), so overpasses don't get false junctions. Geometric noding (`ST_Node`, `pgr_separateCrossing`) would join every bridge to the road below it.
- **Costs** are in seconds. Walking is 1.34 m/s on walkable ways in both directions. Driving uses posted `maxspeed` (bare numbers read as mph) or a default by highway type, at free-flow speed with no turn penalties or traffic.
- **One-way:** `oneway=yes/-1`, plus the implied one-way of motorways and roundabouts. Walking ignores one-way tags.
- **Islands and one-way traps** come from `pgr_connectedComponents` and `pgr_strongComponents`.
