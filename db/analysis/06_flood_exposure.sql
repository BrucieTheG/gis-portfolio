-- 06 - Flood exposure of buildings, roads and transit (analysis 5).
--
-- FEMA's 100-yr (1% annual chance) and 500-yr (0.2%) zones don't overlap, so "500-yr exposure"
-- means 100-yr + 500-yr together (see analysis.flood_summary). `levee` is Zone X behind a
-- levee, reported separately.
--   Buildings  a footprint touching a zone counts as exposed. A building touching more than
--              one class takes the highest-risk one.
--   Roads      drivable edges cut to the zones, in miles. Bridges are left out: they span the
--              floodplain (the river crossings sit over 100-yr polygons) but are elevated.
--   Transit    stops inside a zone, and route miles inside a zone.

DROP TABLE IF EXISTS analysis.flood_pieces CASCADE;
CREATE TABLE analysis.flood_pieces AS
SELECT flood_class,
       CASE flood_class WHEN '100-yr' THEN 1 WHEN '500-yr' THEN 2 ELSE 3 END AS risk_rank,
       ST_Subdivide(geom, 255)::geometry(Polygon, 26915) AS geom
FROM core.flood_zones;
CREATE INDEX ON analysis.flood_pieces USING gist (geom);
ANALYZE analysis.flood_pieces;

DROP TABLE IF EXISTS analysis.flood_buildings CASCADE;
CREATE TABLE analysis.flood_buildings AS
SELECT DISTINCT ON (b.id) b.id AS building_id, p.flood_class, b.height_m, b.area_m2, b.geom
FROM core.buildings b
JOIN analysis.flood_pieces p ON ST_Intersects(b.geom, p.geom)
ORDER BY b.id, p.risk_rank;
ALTER TABLE analysis.flood_buildings ADD PRIMARY KEY (building_id);
CREATE INDEX ON analysis.flood_buildings USING gist (geom);

DROP TABLE IF EXISTS analysis.flood_roads CASCADE;
CREATE TABLE analysis.flood_roads AS
SELECT edge_id, road_class, name, flood_class, ST_Length(geom) AS length_m, geom
FROM (SELECT e.id AS edge_id, e.road_class, e.name, p.flood_class,
             (ST_Dump(ST_CollectionExtract(ST_Intersection(e.geom, p.geom), 2))).geom
                 ::geometry(LineString, 26915) AS geom
      FROM net.edges e
      JOIN analysis.flood_pieces p ON ST_Intersects(e.geom, p.geom)
      WHERE e.drivable AND NOT e.bridge) cut;
CREATE INDEX ON analysis.flood_roads USING gist (geom);

DROP TABLE IF EXISTS analysis.flood_stops CASCADE;
CREATE TABLE analysis.flood_stops AS
SELECT DISTINCT ON (s.stop_id) s.stop_id, s.stop_name, s.is_rail, s.routes,
       s.weekday_departures, p.flood_class, s.geom
FROM core.transit_stops s
JOIN analysis.flood_pieces p ON ST_Intersects(s.geom, p.geom)
ORDER BY s.stop_id, p.risk_rank;
CREATE INDEX ON analysis.flood_stops USING gist (geom);

DROP TABLE IF EXISTS analysis.flood_routes CASCADE;
CREATE TABLE analysis.flood_routes AS
SELECT r.route_id, r.route_short_name, r.mode, p.flood_class,
       sum(ST_Length(ST_Intersection(r.geom, p.geom))) AS length_m
FROM core.transit_routes r
JOIN analysis.flood_pieces p ON ST_Intersects(r.geom, p.geom)
GROUP BY r.route_id, r.route_short_name, r.mode, p.flood_class;

-- Summary for the site and acceptance criterion 7. Classes are cumulative where that's the
-- usual reading: "500-yr" includes the 100-yr zone.
DROP VIEW IF EXISTS analysis.flood_summary;
CREATE VIEW analysis.flood_summary AS
WITH z(label, classes, sort) AS (VALUES
    ('100-yr', ARRAY['100-yr'], 1),
    ('500-yr (includes 100-yr)', ARRAY['100-yr', '500-yr'], 2),
    ('Behind levee', ARRAY['levee'], 3))
SELECT z.label,
       (SELECT count(*) FROM analysis.flood_buildings b WHERE b.flood_class = ANY (z.classes))
           AS buildings,
       (SELECT sum(length_m) / 1609.344 FROM analysis.flood_roads r
        WHERE r.flood_class = ANY (z.classes)) AS road_miles,
       (SELECT count(*) FROM analysis.flood_stops s WHERE s.flood_class = ANY (z.classes))
           AS transit_stops,
       (SELECT count(DISTINCT route_short_name) FROM analysis.flood_routes r
        WHERE r.flood_class = ANY (z.classes)) AS transit_routes,
       (SELECT sum(length_m) / 1609.344 FROM analysis.flood_routes r
        WHERE r.flood_class = ANY (z.classes)) AS route_miles,
       z.sort
FROM z ORDER BY z.sort;
