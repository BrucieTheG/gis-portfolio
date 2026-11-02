"""04 - Metro Transit St. Louis GTFS (MetroBus + MetroLink).

Every GTFS file is loaded as text into raw.gtfs_<name> with COPY, then SQL builds:
  core.transit_stops   stops inside the study area, with the routes and weekday departures at each
  core.transit_routes  one MultiLineString per route from shapes.txt, clipped to the study area
"""
import csv
import io
import os
import zipfile

from common import (BROWSER_UA, RAW_DIR, SRID, connect, download, ensure_schemas, log,
                    main_guard, sql)

# Override in .env if Metro moves the feed (the Mobility Database lists mirrors).
URL = os.getenv("GTFS_URL", "https://www.metrostlouis.org/Transit/google_transit.zip")
FILES = ("agency", "routes", "stops", "trips", "stop_times", "shapes", "calendar", "calendar_dates")


def load_txt(conn, z: zipfile.ZipFile, name: str) -> None:
    member = f"{name}.txt"
    if member not in z.namelist():
        log(f"  {member} not in feed, skipping")
        return
    text = z.read(member).decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    header = [h.strip() for h in next(reader)]
    table = f"raw.gtfs_{name}"
    cols = ", ".join(f'"{h}" text' for h in header)
    sql(conn, f"DROP TABLE IF EXISTS {table}; CREATE TABLE {table} ({cols});")
    with conn.cursor() as cur, cur.copy(f"COPY {table} FROM STDIN") as cp:
        n = 0
        for row in reader:
            if row:
                cp.write_row([v.strip() or None for v in row])
                n += 1
    log(f"  {table}: {n} rows")


def main() -> None:
    conn = connect()
    ensure_schemas(conn)
    z = zipfile.ZipFile(download(URL, RAW_DIR / "metro_stl_gtfs.zip", max_age_days=7,
                                 user_agent=BROWSER_UA))
    for name in FILES:
        load_txt(conn, z, name)

    sql(conn, f"""
        CREATE INDEX ON raw.gtfs_stop_times (stop_id);
        CREATE INDEX ON raw.gtfs_stop_times (trip_id);

        DROP TABLE IF EXISTS core.transit_stops CASCADE;
        CREATE TABLE core.transit_stops AS
        -- Count departures on ONE sample weekday: the first Wednesday at least 7 days into the
        -- feed. Feeds often have overlapping weekday service_ids for different weeks, so counting
        -- every service_id with monday=1 would double-count.
        WITH sample_day AS (
            SELECT d::date AS day
            FROM generate_series((SELECT min(start_date)::date + 7 FROM raw.gtfs_calendar),
                                 (SELECT min(start_date)::date + 13 FROM raw.gtfs_calendar),
                                 interval '1 day') d
            WHERE extract(isodow FROM d) = 3
        ),
        weekday AS (
            SELECT c.service_id FROM raw.gtfs_calendar c, sample_day s
            WHERE c.wednesday = '1' AND s.day BETWEEN c.start_date::date AND c.end_date::date
              AND NOT EXISTS (SELECT 1 FROM raw.gtfs_calendar_dates cd
                              WHERE cd.service_id = c.service_id AND cd.date::date = s.day
                                AND cd.exception_type = '2')
            UNION
            SELECT cd.service_id FROM raw.gtfs_calendar_dates cd, sample_day s
            WHERE cd.date::date = s.day AND cd.exception_type = '1'
        ),
        served AS (
            SELECT st.stop_id,
                   count(*) FILTER (WHERE t.service_id IN (SELECT service_id FROM weekday))
                       AS weekday_departures,
                   array_agg(DISTINCT r.route_short_name ORDER BY r.route_short_name) AS routes,
                   bool_or(r.route_type IN ('0', '1', '2')) AS is_rail
            FROM raw.gtfs_stop_times st
            JOIN raw.gtfs_trips t USING (trip_id)
            JOIN raw.gtfs_routes r USING (route_id)
            GROUP BY st.stop_id
        )
        SELECT s.stop_id, s.stop_code, s.stop_name,
               s.wheelchair_boarding = '1' AS wheelchair_boarding,
               COALESCE(sv.is_rail, false) AS is_rail,
               COALESCE(sv.weekday_departures, 0)::int AS weekday_departures,
               COALESCE(array_length(sv.routes, 1), 0) AS route_count,
               array_to_string(sv.routes, ', ') AS routes,
               ST_Transform(ST_SetSRID(ST_MakePoint(s.stop_lon::float8, s.stop_lat::float8), 4326),
                            {SRID})::geometry(Point, {SRID}) AS geom
        FROM raw.gtfs_stops s
        LEFT JOIN served sv USING (stop_id);
        DELETE FROM core.transit_stops t USING core.study_boundary b
            WHERE NOT ST_Intersects(t.geom, b.geom);
        DELETE FROM core.transit_stops WHERE route_count = 0;  -- parent stations / unused stops
        ALTER TABLE core.transit_stops ADD PRIMARY KEY (stop_id);
        CREATE INDEX ON core.transit_stops USING gist (geom);

        DROP TABLE IF EXISTS core.transit_routes CASCADE;
        CREATE TABLE core.transit_routes AS
        WITH shape_lines AS (
            SELECT shape_id,
                   ST_Transform(ST_SetSRID(ST_MakeLine(
                       ST_MakePoint(shape_pt_lon::float8, shape_pt_lat::float8)
                       ORDER BY shape_pt_sequence::int), 4326), {SRID}) AS geom
            FROM raw.gtfs_shapes GROUP BY shape_id
        ),
        -- The feed repeats a route under a new route_id per service period (e.g. MetroLink),
        -- so routes are merged by their public short name.
        route_shapes AS (
            SELECT DISTINCT r.route_short_name, t.shape_id
            FROM raw.gtfs_trips t JOIN raw.gtfs_routes r USING (route_id)
        ),
        merged AS (
            SELECT rs.route_short_name, ST_LineMerge(ST_Union(sl.geom)) AS geom
            FROM route_shapes rs JOIN shape_lines sl USING (shape_id)
            GROUP BY rs.route_short_name
        ),
        r AS (
            SELECT DISTINCT ON (route_short_name) * FROM raw.gtfs_routes
            ORDER BY route_short_name, route_id
        )
        SELECT r.route_short_name AS route_id, r.route_short_name, r.route_long_name,
               -- Metro codes MetroLink light rail as route_type 2, so group 0/1/2 as 'rail'.
               CASE WHEN r.route_type IN ('0', '1', '2') THEN 'rail'
                    WHEN r.route_type = '3' THEN 'bus' ELSE r.route_type END AS mode,
               '#' || COALESCE(r.route_color, '666666') AS color,
               ST_Multi(ST_CollectionExtract(ST_Intersection(m.geom, b.geom), 2))
                   ::geometry(MultiLineString, {SRID}) AS geom
        FROM merged m
        JOIN r USING (route_short_name)
        JOIN core.study_boundary b ON ST_Intersects(m.geom, b.geom);
        ALTER TABLE core.transit_routes ADD PRIMARY KEY (route_id);
        CREATE INDEX ON core.transit_routes USING gist (geom);
    """)
    s = sql(conn, "SELECT count(*), count(*) FILTER (WHERE is_rail), sum(weekday_departures) "
                  "FROM core.transit_stops")[0]
    r = sql(conn, "SELECT mode, count(*), round((sum(ST_Length(geom))/1000)::numeric) "
                  "FROM core.transit_routes GROUP BY 1")
    feed = sql(conn, "SELECT min(start_date), max(end_date) FROM raw.gtfs_calendar")[0]
    log(f"feed valid {feed[0]}-{feed[1]}")
    log(f"core.transit_stops: {s[0]} stops ({s[1]} rail), {s[2]} weekday departures")
    log("core.transit_routes: " + ", ".join(f"{m} {n} routes / {km} km" for m, n, km in r))


if __name__ == "__main__":
    main_guard(main)
