-- Data QC checks for the core schema. etl/qc_report.py installs this and writes docs/data-qc.md.
-- Acceptance criteria 2 (valid geometry, expected SRID, nonzero rows) and
-- 3 (no data outside the study boundary).

CREATE SCHEMA IF NOT EXISTS qc;

-- The study boundary buffered 1 m and cut into small indexed pieces, so the point-in-polygon
-- test below uses the spatial index instead of one huge polygon.
DROP TABLE IF EXISTS qc.boundary_pieces CASCADE;
CREATE TABLE qc.boundary_pieces AS
SELECT ST_Subdivide(ST_Buffer(geom, 1), 128) AS geom FROM core.study_boundary;
CREATE INDEX ON qc.boundary_pieces USING gist (geom);
ANALYZE qc.boundary_pieces;

-- One row of checks for a table's geometry column.
-- outside_boundary counts features whose interior point is more than 1 m outside the study
-- boundary (1 m absorbs rounding along the clip line). Install after core.study_boundary exists.
CREATE OR REPLACE FUNCTION qc.check_table(tbl regclass, geom_col text DEFAULT 'geom')
RETURNS TABLE (table_name text, row_count bigint, null_geom bigint, empty_geom bigint,
               invalid_geom bigint, srids text, geom_types text, outside_boundary bigint)
LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY EXECUTE format($q$
        SELECT %1$L::text,
               count(*),
               count(*) FILTER (WHERE t.%2$I IS NULL),
               count(*) FILTER (WHERE ST_IsEmpty(t.%2$I)),
               count(*) FILTER (WHERE NOT ST_IsValid(t.%2$I)),
               string_agg(DISTINCT ST_SRID(t.%2$I)::text, ', '),
               string_agg(DISTINCT GeometryType(t.%2$I), ', '),
               count(*) FILTER (WHERE NOT EXISTS (
                   SELECT 1 FROM qc.boundary_pieces b
                   WHERE ST_Intersects(b.geom, ST_PointOnSurface(t.%2$I))))
        FROM %3$s t
    $q$, tbl::text, geom_col, tbl);
END $$;

-- Every core table with a geometry column, checked in one call.
CREATE OR REPLACE VIEW qc.core_tables AS
SELECT format('%I.%I', f_table_schema, f_table_name)::regclass AS tbl,
       f_geometry_column AS geom_col, srid AS declared_srid, type AS declared_type
FROM geometry_columns
WHERE f_table_schema = 'core'
ORDER BY f_table_name;
