-- 02 - Building density and character per hexagon (analysis 1).
--
--   bldg_per_km2      buildings per km2 of (clipped) hex area
--   coverage_pct      building footprint area as a share of hex area
--   median_height_m   median of buildings that have a height (94% do); NULL if none
--   p90_height_m      90th percentile height, which picks out the tall cores
--   median_footprint_m2

DROP TABLE IF EXISTS analysis.hex_buildings CASCADE;
CREATE TABLE analysis.hex_buildings AS
SELECT h.hex_id,
       h.area_km2,
       count(b.building_id)::int AS bldg_count,
       count(b.building_id) / h.area_km2 AS bldg_per_km2,
       COALESCE(sum(b.area_m2), 0) AS footprint_m2,
       100 * COALESCE(sum(b.area_m2), 0) / (h.area_km2 * 1e6) AS coverage_pct,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY b.height_m) AS median_height_m,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY b.height_m) AS p90_height_m,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY b.area_m2) AS median_footprint_m2,
       h.geom_clip AS geom
FROM analysis.hex h
LEFT JOIN analysis.building_pts b ON b.hex_id = h.hex_id
GROUP BY h.hex_id;
ALTER TABLE analysis.hex_buildings ADD PRIMARY KEY (hex_id);
CREATE INDEX ON analysis.hex_buildings USING gist (geom);
