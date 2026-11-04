-- 05 - Walk and drive isochrones from a point (analysis 3), computed live by the API.
--
--   SELECT * FROM analysis.isochrones(-90.1925, 38.6310, 'walk', '{5,10,15}');
--
-- The point snaps to the nearest vertex of the main walk or drive component. One
-- pgr_dijkstraCost run gives travel time to every vertex within reach, and each band is the
-- concave hull of the vertices reached in time. (pgr_dijkstraCost is used instead of
-- pgr_drivingDistance: on pgRouting 4.0.1 the latter took 24 s for a 15-minute drive here,
-- against 1.5 s.)
--
-- The edges are pre-cut to a box around the start. Nothing reachable in t seconds can be
-- farther than t * (top speed) in a straight line, so the box doesn't change the result:
-- 1.34 m/s for walking, and 75 mph for driving (above every speed in net.edges; 08_analyses.py
-- checks this).

CREATE OR REPLACE FUNCTION analysis.isochrones(
    lon float8, lat float8, mode text DEFAULT 'walk', minutes int[] DEFAULT '{5,10,15}')
RETURNS TABLE (band_minutes int, start_vertex bigint, reached_vertices int, area_km2 float8,
               geom geometry)
LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    pt geometry := ST_Transform(ST_SetSRID(ST_MakePoint(lon, lat), 4326), 26915);
    max_s float8;
    radius float8;
    v bigint;
    vgeom geometry;
    cost_cols text;
    edge_filter text;
    smooth_m float8;
BEGIN
    IF mode NOT IN ('walk', 'drive') THEN
        RAISE EXCEPTION 'mode must be walk or drive, not %', mode;
    END IF;
    SELECT max(m) * 60 INTO max_s FROM unnest(minutes) m;
    IF max_s IS NULL OR max_s <= 0 OR max_s > 3600 THEN
        RAISE EXCEPTION 'minutes must be between 1 and 60';
    END IF;

    IF mode = 'walk' THEN
        SELECT n.id, n.geom INTO v, vgeom FROM net.vertices n WHERE n.in_walk_main
        ORDER BY n.geom <-> pt LIMIT 1;
        radius := max_s * 1.34 + 10;
        cost_cols := 'walk_cost AS cost, walk_rcost AS reverse_cost';
        edge_filter := 'walkable';
        smooth_m := 25;
    ELSE
        SELECT n.id, n.geom INTO v, vgeom FROM net.vertices n WHERE n.in_drive_main
        ORDER BY n.geom <-> pt LIMIT 1;
        radius := max_s * 75 * 0.44704 + 10;
        cost_cols := 'drive_cost AS cost, drive_rcost AS reverse_cost';
        edge_filter := 'drivable';
        smooth_m := 75;
    END IF;

    RETURN QUERY
    WITH reach AS (
        SELECT c.end_vid AS node, c.agg_cost
        FROM pgr_dijkstraCost(
            format('SELECT id, source, target, %s FROM net.edges WHERE %s AND geom && %L::geometry',
                   cost_cols, edge_filter, ST_Expand(vgeom, radius)),
            v,
            ARRAY(SELECT n.id FROM net.vertices n
                  WHERE n.geom && ST_Expand(vgeom, radius)
                    AND CASE WHEN mode = 'walk' THEN n.in_walk_main ELSE n.in_drive_main END)) c
        WHERE c.agg_cost <= max_s
        UNION ALL SELECT v, 0
    )
    SELECT b.m, v, b.n::int, ST_Area(b.hull) / 1e6, ST_Transform(b.hull, 4326)
    FROM unnest(minutes) AS u(m)
    CROSS JOIN LATERAL (
        SELECT u.m, count(*) AS n,
               ST_Buffer(ST_ConcaveHull(ST_Collect(n.geom), 0.2), smooth_m) AS hull
        FROM reach r JOIN net.vertices n ON n.id = r.node
        WHERE r.agg_cost <= u.m * 60) b
    ORDER BY b.m;
END
$fn$;
