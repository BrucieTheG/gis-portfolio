-- 01 - Hexagon grid and building points shared by the other analyses.
--
-- Hexagons are 500 m across (flat side to flat side; edge length 500/sqrt(3) = 288.675 m),
-- about 0.217 km2 each. Hexes on the study boundary are clipped (geom_clip), and densities use
-- the clipped area so edge hexes aren't understated.

CREATE SCHEMA IF NOT EXISTS analysis;

DROP TABLE IF EXISTS analysis.boundary_pieces CASCADE;
CREATE TABLE analysis.boundary_pieces AS
SELECT ST_Subdivide(geom, 128)::geometry(Polygon, 26915) AS geom FROM core.study_boundary;
CREATE INDEX ON analysis.boundary_pieces USING gist (geom);

DROP TABLE IF EXISTS analysis.boundary_lines CASCADE;
CREATE TABLE analysis.boundary_lines AS
SELECT ST_Subdivide(ST_Boundary(geom), 64)::geometry(LineString, 26915) AS geom
FROM core.study_boundary;
CREATE INDEX ON analysis.boundary_lines USING gist (geom);

DROP TABLE IF EXISTS analysis.hex CASCADE;
CREATE TABLE analysis.hex AS
WITH grid AS (
    SELECT h.i, h.j, h.geom
    FROM core.study_boundary b, LATERAL ST_HexagonGrid(288.675, b.geom) h
    WHERE EXISTS (SELECT 1 FROM analysis.boundary_pieces p WHERE ST_Intersects(h.geom, p.geom))
)
, clipped AS (
    SELECT g.i, g.j, g.geom,
           ST_Multi(CASE WHEN ST_CoveredBy(g.geom, b.geom) THEN g.geom
                         ELSE ST_CollectionExtract(ST_Intersection(g.geom, b.geom), 3) END) AS clip
    FROM grid g, core.study_boundary b
)
SELECT row_number() OVER (ORDER BY i, j)::int AS hex_id, i, j,
       geom::geometry(Polygon, 26915) AS geom,
       clip::geometry(MultiPolygon, 26915) AS geom_clip,
       ST_Area(clip) / 1e6 AS area_km2
FROM clipped WHERE NOT ST_IsEmpty(clip);
-- Each file runs as one transaction (08_analyses.py sends it in one call). Don't UPDATE a new
-- table before indexing it there: PostgreSQL then marks the index unusable until the
-- transaction ends (pg_index.indcheckxmin), and the building join below falls back to testing
-- every building against every hex (it ran for 20+ minutes instead of ~15 s). Compute columns
-- in CREATE TABLE ... AS instead, and ANALYZE so the planner has row counts.
ALTER TABLE analysis.hex ADD PRIMARY KEY (hex_id);
CREATE INDEX ON analysis.hex USING gist (geom);
ANALYZE analysis.hex;

-- One interior point per building (the same rule the data QC uses), tagged with its hex.
-- A point exactly on a shared hex edge goes to the lower hex_id, so it's counted once.
DROP TABLE IF EXISTS analysis.building_pts CASCADE;
CREATE TABLE analysis.building_pts AS
SELECT b.id AS building_id, h.hex_id, b.area_m2, b.height_m, p.geom
FROM core.buildings b
CROSS JOIN LATERAL (SELECT ST_PointOnSurface(b.geom)::geometry(Point, 26915) AS geom) p
LEFT JOIN LATERAL (SELECT hex_id FROM analysis.hex h WHERE ST_Intersects(h.geom, p.geom)
                   ORDER BY hex_id LIMIT 1) h ON true;
ALTER TABLE analysis.building_pts ADD PRIMARY KEY (building_id);
CREATE INDEX ON analysis.building_pts USING gist (geom);
CREATE INDEX ON analysis.building_pts (hex_id);
ANALYZE analysis.building_pts;
