-- FloodSafe PostGIS schema.
--
-- Everything is stored in EPSG:4326 because that is what the API
-- serves and what every source dataset already uses. Distance work is
-- done with geography casts rather than a projected column, which
-- keeps the storage honest and avoids a second geometry per table.
--
-- The point of moving this into PostGIS is not storage. server.py
-- currently loads the hazard atlas, the watersheds and the localities
-- into Python lists at import time and answers "which zone is this
-- point in?" by looping shapely polygons on every call. Here that is
-- one indexed query, and the FFPI raster can be sampled in the same
-- round trip instead of through a separate lookup grid.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_raster;

-- ---------------------------------------------------------------
-- Reference layers (rebuilt wholesale by ingest.py; no user data)
-- ---------------------------------------------------------------

DROP TABLE IF EXISTS ffpi_scores CASCADE;
DROP TABLE IF EXISTS soil_samples CASCADE;
DROP TABLE IF EXISTS localities CASCADE;
DROP TABLE IF EXISTS shelters CASCADE;
DROP TABLE IF EXISTS watersheds CASCADE;
DROP TABLE IF EXISTS hazard_zones CASCADE;
DROP TABLE IF EXISTS ffpi_raster CASCADE;

-- The surveyed state flash-flood hazard atlas. Covers only ~9.7% of
-- Uttarakhand's area, which is why ffpi_raster exists alongside it.
CREATE TABLE hazard_zones (
    id            serial PRIMARY KEY,
    hazard_class  text NOT NULL
                  CHECK (hazard_class IN ('LOW', 'MODERATE', 'SIGNIFICANT', 'EXTREME')),
    area_ha       double precision,
    geom          geometry(MultiPolygon, 4326) NOT NULL
);
CREATE INDEX hazard_zones_geom_idx ON hazard_zones USING GIST (geom);
CREATE INDEX hazard_zones_class_idx ON hazard_zones (hazard_class);

-- HydroSHEDS/HydroBASINS v1c level 8 sub-basins, clipped to the state.
CREATE TABLE watersheds (
    hybas_id      bigint PRIMARY KEY,
    up_area_km2   double precision,
    sub_area_km2  double precision,
    geom          geometry(MultiPolygon, 4326) NOT NULL
);
CREATE INDEX watersheds_geom_idx ON watersheds USING GIST (geom);

-- Guidance towns and the real OSM place nodes around them. `kind`
-- separates the nine anchor towns from the extracted localities.
CREATE TABLE localities (
    id            serial PRIMARY KEY,
    name          text NOT NULL,
    parent_town   text,
    kind          text NOT NULL CHECK (kind IN ('town', 'locality')),
    geom          geometry(Point, 4326) NOT NULL,
    UNIQUE (name, kind, parent_town)
);
CREATE INDEX localities_geom_idx ON localities USING GIST (geom);
CREATE INDEX localities_kind_idx ON localities (kind);

CREATE TABLE shelters (
    id            serial PRIMARY KEY,
    name          text,
    category      text NOT NULL,
    is_evacuation_target boolean NOT NULL DEFAULT false,
    geom          geometry(Point, 4326) NOT NULL
);
CREATE INDEX shelters_geom_idx ON shelters USING GIST (geom);
CREATE INDEX shelters_target_idx ON shelters (is_evacuation_target);

-- SoilGrids v2.0 texture sampled at the points the pipeline actually
-- queried. Deliberately sparse: server.py returns no soil rather than
-- guessing when a point is far from any sample, and the API keeps that
-- behaviour via SOIL_MAX_KM.
CREATE TABLE soil_samples (
    id            serial PRIMARY KEY,
    sand_pct      double precision,
    clay_pct      double precision,
    silt_pct      double precision,
    hydrologic_soil_group text
                  CHECK (hydrologic_soil_group IN ('A', 'B', 'C', 'D')),
    geom          geometry(Point, 4326) NOT NULL
);
CREATE INDEX soil_samples_geom_idx ON soil_samples USING GIST (geom);

-- Flash Flood Potential Index, tiled so ST_Value on a point touches
-- one small tile rather than the whole state.
CREATE TABLE ffpi_raster (
    rid           serial PRIMARY KEY,
    rast          raster NOT NULL
);
CREATE INDEX ffpi_raster_convexhull_idx
    ON ffpi_raster USING GIST (ST_ConvexHull(rast));

-- Per-location scores precomputed by the pipeline. Kept as a table
-- rather than recomputed from the raster because it also carries the
-- XGBoost probability and the component breakdown, neither of which
-- is derivable from the FFPI value alone.
CREATE TABLE ffpi_scores (
    locality_id   integer PRIMARY KEY REFERENCES localities(id) ON DELETE CASCADE,
    ffpi          double precision,
    ffpi_band     text,
    model_prob    double precision,
    components    jsonb,
    terrain       jsonb
);

-- ---------------------------------------------------------------
-- User-generated data (survives re-ingest; never dropped above)
-- ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS hazard_reports (
    id            uuid PRIMARY KEY,
    hazard_type   text NOT NULL,
    note          text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz,
    confirmations integer NOT NULL DEFAULT 0,
    geom          geometry(Point, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS hazard_reports_geom_idx ON hazard_reports USING GIST (geom);
CREATE INDEX IF NOT EXISTS hazard_reports_expiry_idx ON hazard_reports (expires_at);

-- ---------------------------------------------------------------
-- Classification helpers
-- ---------------------------------------------------------------

-- Hazard class for a point, mirroring server.py's classify_point:
-- an exact containment wins, otherwise the nearest polygon within
-- max_km, otherwise nothing. Returned distance is 0 for a containment.
CREATE OR REPLACE FUNCTION classify_hazard_point(
    in_lat double precision,
    in_lon double precision,
    max_km double precision DEFAULT 15.0
)
RETURNS TABLE (hazard_class text, exact_match boolean, distance_km double precision)
LANGUAGE sql STABLE AS $$
    WITH p AS (SELECT ST_SetSRID(ST_MakePoint(in_lon, in_lat), 4326) AS g)
    SELECT z.hazard_class,
           ST_Contains(z.geom, p.g) AS exact_match,
           ST_Distance(z.geom::geography, p.g::geography) / 1000.0 AS distance_km
    FROM hazard_zones z, p
    WHERE ST_DWithin(z.geom::geography, p.g::geography, max_km * 1000.0)
    ORDER BY ST_Contains(z.geom, p.g) DESC,
             z.geom::geography <-> p.g::geography
    LIMIT 1;
$$;

-- FFPI value at a point, or NULL outside the raster / in a nodata cell
-- (glaciated terrain, mostly).
CREATE OR REPLACE FUNCTION ffpi_at_point(
    in_lat double precision,
    in_lon double precision
)
RETURNS double precision
LANGUAGE sql STABLE AS $$
    SELECT ST_Value(r.rast, ST_SetSRID(ST_MakePoint(in_lon, in_lat), 4326))
    FROM ffpi_raster r
    WHERE ST_Intersects(r.rast, ST_SetSRID(ST_MakePoint(in_lon, in_lat), 4326))
    LIMIT 1;
$$;

CREATE OR REPLACE FUNCTION ffpi_band(value double precision)
RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN value IS NULL THEN NULL
        WHEN value < 3.5 THEN 'VERY LOW'
        WHEN value < 4.5 THEN 'LOW'
        WHEN value < 5.5 THEN 'MODERATE'
        WHEN value < 6.5 THEN 'HIGH'
        ELSE 'VERY HIGH'
    END;
$$;
