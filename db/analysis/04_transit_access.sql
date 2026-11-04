-- 04 - Transit walk access: network walking distance vs. straight-line distance (analysis 2).
--
-- Every building gets two distances to its nearest stop with weekday service:
--   eucl_m  straight line (what a simple buffer measures)
--   net_m   walking distance along the walk network
-- The share of buildings within 1/4 mile (402 m) and 1/2 mile (805 m) is compared for both.
--
-- Network distance comes from one shortest-path run: a virtual source (vertex 0) is joined to
-- each stop's nearest walk vertex by an edge as long as the stop's offset from it. So
-- pgr_dijkstraCost from vertex 0 gives every vertex its distance to the nearest stop. A building
-- then snaps to its nearest walkable edge and takes the shorter way along it to either end,
-- plus its offset from the edge.

DROP TABLE IF EXISTS analysis.stop_snap CASCADE;
CREATE TABLE analysis.stop_snap AS
SELECT s.stop_id, v.id AS vertex_id, ST_Distance(s.geom, v.geom) AS offset_m
FROM core.transit_stops s
CROSS JOIN LATERAL (SELECT id, geom FROM net.vertices WHERE in_walk_main
                    ORDER BY geom <-> s.geom LIMIT 1) v
WHERE s.weekday_departures > 0;

DROP TABLE IF EXISTS analysis.vertex_stop_dist CASCADE;
CREATE TABLE analysis.vertex_stop_dist AS
SELECT end_vid AS vertex_id, agg_cost AS dist_m
FROM pgr_dijkstraCost(
    $$SELECT id, source, target, length_m AS cost, length_m AS reverse_cost
      FROM net.edges WHERE walkable
      UNION ALL
      SELECT 1000000000000 + row_number() OVER (ORDER BY stop_id), 0, vertex_id, offset_m, -1
      FROM analysis.stop_snap$$,
    0::bigint,
    ARRAY(SELECT id FROM net.vertices WHERE in_walk_main));
ALTER TABLE analysis.vertex_stop_dist ADD PRIMARY KEY (vertex_id);
ANALYZE analysis.vertex_stop_dist;

-- Walkable edges of the main walk component, for snapping buildings.
DROP TABLE IF EXISTS analysis.walk_edges CASCADE;
CREATE TABLE analysis.walk_edges AS
SELECT e.id, e.source, e.target, e.length_m, e.geom
FROM net.edges e JOIN net.vertices v ON v.id = e.source
WHERE e.walkable AND v.in_walk_main;
CREATE INDEX ON analysis.walk_edges USING gist (geom);
ANALYZE analysis.walk_edges;

DROP TABLE IF EXISTS analysis.building_transit CASCADE;
CREATE TABLE analysis.building_transit AS
SELECT b.building_id, b.hex_id,
       st.eucl_m,
       LEAST(ds.dist_m + e.frac * e.length_m, dt.dist_m + (1 - e.frac) * e.length_m)
           + e.offset_m AS net_m,
       e.offset_m AS snap_m,
       b.geom
FROM analysis.building_pts b
CROSS JOIN LATERAL (
    SELECT ST_Distance(b.geom, s.geom) AS eucl_m
    FROM core.transit_stops s WHERE s.weekday_departures > 0
    ORDER BY s.geom <-> b.geom LIMIT 1) st
CROSS JOIN LATERAL (
    SELECT w.source, w.target, w.length_m, ST_LineLocatePoint(w.geom, b.geom) AS frac,
           ST_Distance(w.geom, b.geom) AS offset_m
    FROM analysis.walk_edges w ORDER BY w.geom <-> b.geom LIMIT 1) e
LEFT JOIN analysis.vertex_stop_dist ds ON ds.vertex_id = e.source
LEFT JOIN analysis.vertex_stop_dist dt ON dt.vertex_id = e.target;
ALTER TABLE analysis.building_transit ADD PRIMARY KEY (building_id);
CREATE INDEX ON analysis.building_transit USING gist (geom);
ANALYZE analysis.building_transit;

-- Per hex, for the map: how many buildings each method puts within 1/4 and 1/2 mile.
DROP TABLE IF EXISTS analysis.hex_transit CASCADE;
CREATE TABLE analysis.hex_transit AS
SELECT h.hex_id,
       count(t.building_id)::int AS buildings,
       count(*) FILTER (WHERE t.eucl_m <= 402.336)::int AS q_mile_eucl,
       count(*) FILTER (WHERE t.net_m  <= 402.336)::int AS q_mile_net,
       count(*) FILTER (WHERE t.eucl_m <= 804.672)::int AS h_mile_eucl,
       count(*) FILTER (WHERE t.net_m  <= 804.672)::int AS h_mile_net,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY t.net_m) AS median_net_m,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY t.net_m / NULLIF(t.eucl_m, 0))
           AS median_detour_ratio,
       h.geom_clip AS geom
FROM analysis.hex h
LEFT JOIN analysis.building_transit t ON t.hex_id = h.hex_id
GROUP BY h.hex_id;
ALTER TABLE analysis.hex_transit ADD PRIMARY KEY (hex_id);
CREATE INDEX ON analysis.hex_transit USING gist (geom);

-- Study-wide summary (acceptance criterion 6: both methods, with the difference as a number).
DROP VIEW IF EXISTS analysis.transit_access_summary;
CREATE VIEW analysis.transit_access_summary AS
SELECT d.label, d.meters,
       count(*) FILTER (WHERE t.eucl_m <= d.meters) AS buildings_straight_line,
       count(*) FILTER (WHERE t.net_m <= d.meters) AS buildings_network,
       count(*) AS buildings_total,
       100.0 * count(*) FILTER (WHERE t.eucl_m <= d.meters) / count(*) AS pct_straight_line,
       100.0 * count(*) FILTER (WHERE t.net_m <= d.meters) / count(*) AS pct_network,
       100.0 * (count(*) FILTER (WHERE t.eucl_m <= d.meters)
                - count(*) FILTER (WHERE t.net_m <= d.meters))
             / NULLIF(count(*) FILTER (WHERE t.eucl_m <= d.meters), 0) AS pct_overstated
FROM analysis.building_transit t
CROSS JOIN (VALUES ('1/4 mile', 402.336), ('1/2 mile', 804.672)) d(label, meters)
GROUP BY d.label, d.meters;
