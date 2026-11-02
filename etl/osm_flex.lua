-- osm2pgsql flex style for the gis-portfolio project.
-- Loads only what the analyses need into the `raw` schema, already in EPSG:26915.
--   raw.osm_ways      every highway=* way (roads, sidewalks, paths) with routing tags
--   raw.osm_rail      railway lines (MetroLink light rail, freight), for context
--   raw.osm_transit   transit points/areas: stations, platforms, bus stops (as points)

local srid = 26915

local tables = {}

tables.ways = osm2pgsql.define_way_table('osm_ways', {
    { column = 'highway',  type = 'text', not_null = true },
    { column = 'name',     type = 'text' },
    { column = 'ref',      type = 'text' },
    { column = 'oneway',   type = 'text' },
    { column = 'maxspeed', type = 'text' },
    { column = 'lanes',    type = 'text' },
    { column = 'surface',  type = 'text' },
    { column = 'access',   type = 'text' },
    { column = 'foot',     type = 'text' },
    { column = 'sidewalk', type = 'text' },
    { column = 'bridge',   type = 'text' },
    { column = 'tunnel',   type = 'text' },
    { column = 'layer',    type = 'text' },
    { column = 'tags',     type = 'jsonb' },
    { column = 'geom',     type = 'linestring', projection = srid, not_null = true },
}, { schema = 'raw' })

tables.rail = osm2pgsql.define_way_table('osm_rail', {
    { column = 'railway', type = 'text', not_null = true },
    { column = 'name',    type = 'text' },
    { column = 'service', type = 'text' },
    { column = 'tags',    type = 'jsonb' },
    { column = 'geom',    type = 'linestring', projection = srid, not_null = true },
}, { schema = 'raw' })

tables.transit = osm2pgsql.define_table({
    name = 'osm_transit',
    schema = 'raw',
    ids = { type = 'any', id_column = 'osm_id', type_column = 'osm_type' },
    columns = {
        { column = 'kind', type = 'text', not_null = true },
        { column = 'name', type = 'text' },
        { column = 'tags', type = 'jsonb' },
        { column = 'geom', type = 'point', projection = srid, not_null = true },
    }
})

local rail_keep = { light_rail = true, rail = true, subway = true, tram = true }

local function transit_kind(tags)
    if tags.railway == 'station' or tags.public_transport == 'station' then return 'station' end
    if tags.public_transport == 'platform' or tags.railway == 'platform' then return 'platform' end
    if tags.highway == 'bus_stop' or tags.public_transport == 'stop_position' then return 'stop' end
    if tags.amenity == 'bus_station' then return 'station' end
    return nil
end

function osm2pgsql.process_node(object)
    local kind = transit_kind(object.tags)
    if kind then
        tables.transit:insert({ kind = kind, name = object.tags.name, tags = object.tags,
                                geom = object:as_point() })
    end
end

function osm2pgsql.process_way(object)
    local t = object.tags
    if t.highway and t.area ~= 'yes' and t.highway ~= 'platform' and t.highway ~= 'proposed'
       and t.highway ~= 'construction' and t.highway ~= 'abandoned' then
        tables.ways:insert({
            highway = t.highway, name = t.name, ref = t.ref, oneway = t.oneway,
            maxspeed = t.maxspeed, lanes = t.lanes, surface = t.surface, access = t.access,
            foot = t.foot, sidewalk = t.sidewalk, bridge = t.bridge, tunnel = t.tunnel,
            layer = t.layer, tags = t, geom = object:as_linestring()
        })
    end
    if t.railway and rail_keep[t.railway] then
        tables.rail:insert({ railway = t.railway, name = t.name, service = t.service, tags = t,
                             geom = object:as_linestring() })
    end
    local kind = transit_kind(t)
    if kind then
        local g = object.is_closed and object:as_polygon() or object:as_linestring()
        tables.transit:insert({ kind = kind, name = t.name, tags = t, geom = g:centroid() })
    end
end
