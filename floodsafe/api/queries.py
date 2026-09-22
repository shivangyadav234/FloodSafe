"""
The spatial queries.

This module is the actual point of the PostGIS migration. In the Flask
app, classifying a point means looping 191 shapely polygons in Python,
scanning a list of watershed polygons, linear-searching soil samples by
haversine distance, and reading a separate uint8 grid for FFPI -- all
from structures built at import time. Here each of those is one indexed
query, and zone_rows() answers the whole zone list in a single pass.
"""

from . import config, db


ZONE_SELECT = """
WITH z AS (
    SELECT l.id, l.name, l.parent_town, l.kind,
           ST_Y(l.geom) AS lat, ST_X(l.geom) AS lon, l.geom,
           s.ffpi, s.ffpi_band, s.model_prob, s.components, s.terrain
    FROM localities l
    LEFT JOIN ffpi_scores s ON s.locality_id = l.id
)
SELECT z.id, z.name, z.parent_town, z.kind, z.lat, z.lon,
       z.ffpi, z.ffpi_band, z.model_prob, z.components, z.terrain,
       h.hazard_class, h.exact_match, h.distance_km,
       w.hybas_id, w.up_area_km2, w.sub_area_km2,
       so.hydrologic_soil_group, so.sand_pct, so.clay_pct, so.silt_pct
FROM z
LEFT JOIN LATERAL (
    SELECT * FROM classify_hazard_point(z.lat, z.lon, %(hazard_max_km)s)
) h ON true
LEFT JOIN LATERAL (
    SELECT ws.hybas_id, ws.up_area_km2, ws.sub_area_km2
    FROM watersheds ws
    WHERE ST_Contains(ws.geom, z.geom)
    LIMIT 1
) w ON true
LEFT JOIN LATERAL (
    SELECT ss.hydrologic_soil_group, ss.sand_pct, ss.clay_pct, ss.silt_pct
    FROM soil_samples ss
    WHERE ST_DWithin(ss.geom::geography, z.geom::geography, %(soil_max_km)s * 1000)
    ORDER BY ss.geom <-> z.geom
    LIMIT 1
) so ON true
ORDER BY z.kind DESC, z.name
"""


POINT_SELECT = """
WITH p AS (
    SELECT ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326) AS geom
)
SELECT h.hazard_class, h.exact_match, h.distance_km,
       w.hybas_id, w.up_area_km2, w.sub_area_km2,
       so.hydrologic_soil_group, so.sand_pct, so.clay_pct, so.silt_pct,
       f.ffpi, ffpi_band(f.ffpi) AS ffpi_band,
       s.ffpi AS exact_ffpi, s.ffpi_band AS exact_ffpi_band,
       s.model_prob, s.components, s.terrain
FROM p
-- Sampled once and reused; calling ffpi_at_point twice would hit the
-- raster twice for the same cell.
LEFT JOIN LATERAL (
    SELECT ffpi_at_point(%(lat)s, %(lon)s) AS ffpi
) f ON true
LEFT JOIN LATERAL (
    SELECT * FROM classify_hazard_point(%(lat)s, %(lon)s, %(hazard_max_km)s)
) h ON true
LEFT JOIN LATERAL (
    SELECT ws.hybas_id, ws.up_area_km2, ws.sub_area_km2
    FROM watersheds ws WHERE ST_Contains(ws.geom, p.geom) LIMIT 1
) w ON true
LEFT JOIN LATERAL (
    SELECT ss.hydrologic_soil_group, ss.sand_pct, ss.clay_pct, ss.silt_pct
    FROM soil_samples ss
    WHERE ST_DWithin(ss.geom::geography, p.geom::geography, %(soil_max_km)s * 1000)
    ORDER BY ss.geom <-> p.geom LIMIT 1
) so ON true
-- A precomputed score exists only if this point is (essentially) one of
-- the known locations; it is sampled from the 90 m raster directly and
-- so beats the regridded raster value.
LEFT JOIN LATERAL (
    SELECT sc.ffpi, sc.ffpi_band, sc.model_prob, sc.components, sc.terrain
    FROM ffpi_scores sc
    JOIN localities l ON l.id = sc.locality_id
    WHERE ST_DWithin(l.geom::geography, p.geom::geography, 100)
    ORDER BY l.geom <-> p.geom LIMIT 1
) s ON true
"""


def zone_rows() -> list[dict]:
    return db.fetch_all(ZONE_SELECT, {
        "hazard_max_km": config.HAZARD_MAX_KM,
        "soil_max_km": config.SOIL_MAX_KM,
    })


def point_row(lat: float, lon: float) -> dict | None:
    return db.fetch_one(POINT_SELECT, {
        "lat": lat, "lon": lon,
        "hazard_max_km": config.HAZARD_MAX_KM,
        "soil_max_km": config.SOIL_MAX_KM,
    })


def shelters(evacuation_only: bool = False) -> list[dict]:
    sql = """
        SELECT id, name, category, is_evacuation_target,
               ST_Y(geom) AS lat, ST_X(geom) AS lon
        FROM shelters
    """
    if evacuation_only:
        sql += " WHERE is_evacuation_target"
    return db.fetch_all(sql + " ORDER BY name")


def nearest_shelters(lat: float, lon: float, limit: int = 5,
                     evacuation_only: bool = True) -> list[dict]:
    """K nearest by true distance.

    The `<->` ordering uses the GiST index for the candidate scan, and
    the geography cast gives metres rather than degrees -- a plain
    degree distance over-weights longitude at this latitude.
    """
    sql = """
        SELECT id, name, category, is_evacuation_target,
               ST_Y(geom) AS lat, ST_X(geom) AS lon,
               ST_Distance(geom::geography,
                           ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography
               ) / 1000.0 AS distance_km
        FROM shelters
        {where}
        ORDER BY geom <-> ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)
        LIMIT %(limit)s
    """.format(where="WHERE is_evacuation_target" if evacuation_only else "")
    return db.fetch_all(sql, {"lat": lat, "lon": lon, "limit": limit})


def layer_geojson(table: str, properties: list[str], simplify_deg: float = 0.0) -> dict:
    """One FeatureCollection built in the database.

    ST_AsGeoJSON per row then assembled here, rather than selecting
    geometry and converting in Python.
    """
    allowed = {"hazard_zones", "watersheds"}
    if table not in allowed:
        raise ValueError(f"refusing to serve unknown table {table!r}")

    geom = "geom"
    if simplify_deg > 0:
        geom = f"ST_SimplifyPreserveTopology(geom, {float(simplify_deg)})"

    cols = ", ".join(properties)
    rows = db.fetch_all(
        f"SELECT {cols}, ST_AsGeoJSON({geom}) AS geometry FROM {table}"
    )

    import json as _json
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": _json.loads(r.pop("geometry")),
                "properties": r,
            }
            for r in rows
        ],
    }
