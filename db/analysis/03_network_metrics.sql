-- 03 - Street network metrics per hexagon (analysis 4).
--
-- Uses the street network: drivable edges except highway=service (driveways, parking aisles,
-- alleys), which would otherwise count every driveway as a dead end.
--   intersections_per_km2  street vertices where 3+ edge ends meet
--   dead_end_share         dead ends / (dead ends + intersections), the usual cul-de-sac measure;
--                          dead ends within 5 m of the study boundary are clipping artifacts and
--                          are left out
--   street_km_per_km2      street length per km2 (edges assigned to a hex by their midpoint)

DROP TABLE IF EXISTS analysis.street_nodes CASCADE;
CREATE TABLE analysis.street_nodes AS
WITH ends AS (
    SELECT source AS vertex_id FROM net.edges WHERE drivable AND highway <> 'service'
    UNION ALL
    SELECT target FROM net.edges WHERE drivable AND highway <> 'service'
)
SELECT v.id AS vertex_id, count(*)::int AS street_degree, v.geom,
       EXISTS (SELECT 1 FROM analysis.boundary_lines b WHERE ST_DWithin(v.geom, b.geom, 5))
           AS at_boundary
FROM ends JOIN net.vertices v ON v.id = ends.vertex_id
GROUP BY v.id;
CREATE INDEX ON analysis.street_nodes USING gist (geom);
ANALYZE analysis.street_nodes;

DROP TABLE IF EXISTS analysis.hex_network CASCADE;
CREATE TABLE analysis.hex_network AS
WITH nodes AS (
    SELECT h.hex_id,
           count(*) FILTER (WHERE n.street_degree >= 3) AS intersections,
           count(*) FILTER (WHERE n.street_degree = 1 AND NOT n.at_boundary) AS dead_ends
    FROM analysis.street_nodes n
    JOIN LATERAL (SELECT hex_id FROM analysis.hex h WHERE ST_Intersects(h.geom, n.geom)
                  ORDER BY hex_id LIMIT 1) h ON true
    GROUP BY h.hex_id
), streets AS (
    SELECT h.hex_id, sum(e.length_m) / 1000 AS street_km
    FROM net.edges e
    JOIN LATERAL (SELECT hex_id FROM analysis.hex h
                  WHERE ST_Intersects(h.geom, ST_LineInterpolatePoint(e.geom, 0.5))
                  ORDER BY hex_id LIMIT 1) h ON true
    WHERE e.drivable AND e.highway <> 'service'
    GROUP BY h.hex_id
)
SELECT h.hex_id, h.area_km2,
       COALESCE(n.intersections, 0)::int AS intersections,
       COALESCE(n.dead_ends, 0)::int AS dead_ends,
       COALESCE(n.intersections, 0) / h.area_km2 AS intersections_per_km2,
       CASE WHEN COALESCE(n.intersections, 0) + COALESCE(n.dead_ends, 0) > 0
            THEN n.dead_ends::float8 / (n.intersections + n.dead_ends) END AS dead_end_share,
       COALESCE(s.street_km, 0) AS street_km,
       COALESCE(s.street_km, 0) / h.area_km2 AS street_km_per_km2,
       h.geom_clip AS geom
FROM analysis.hex h
LEFT JOIN nodes n USING (hex_id)
LEFT JOIN streets s USING (hex_id);
ALTER TABLE analysis.hex_network ADD PRIMARY KEY (hex_id);
CREATE INDEX ON analysis.hex_network USING gist (geom);
