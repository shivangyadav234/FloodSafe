"""
Read-only HTTP API over the FFGS Postgres DB, for the FLOODSAFE dashboard
(map layers, ward detail panel, rainfall panel, alert panel).

This is Phase 1 of the FFGS<->dashboard interface: exposes what the pipeline
already computes (grid_cells, terrain_features, rainfall, soil_moisture,
risk_predictions, ward_risk). It does NOT generate alerts on its own -- that
happens in src/alerts/authority_sos.py, which is meant to run after
ward_aggregation.py and push authority SMS via the FLOODSAFE Node backend.

Run with:
    uvicorn src.api.main:app --reload --port 8000
(from the ml-pipeline/ directory, with the same .env as the rest of the
pipeline -- it reads the same Postgres connection.)
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))
from config.settings import MODEL_VERSION
from database.db_utils import query_df
from src.hydrology.ffg_engine import dynamic_rainfall_threshold

app = FastAPI(title="FFGS API", version="0.1.0")

# MVP: wide open. Tighten to the deployed frontend origin(s) before going live.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# grid.geojson is served straight from grid_cells (439k rows for the pilot) --
# always bbox-scoped and capped, never returned whole. Lowered from 5000:
# the free-tier API instance (512MB RAM) was returning 500s on requests
# in the 4000+ row range, confirmed by reproducing the same query locally
# (where it succeeds fine) -- points at the deployed instance's memory
# ceiling, not a code bug. A smaller cap keeps worst-case per-request
# memory well under that limit.
MAX_GRID_CELLS_PER_REQUEST = 2000


def _latest_valid_for(model_version: str = MODEL_VERSION) -> Optional[pd.Timestamp]:
    df = query_df(
        "SELECT max(valid_for) AS v FROM risk_predictions WHERE model_version = :mv",
        {"mv": model_version},
    )
    v = df.iloc[0]["v"]
    return None if pd.isna(v) else v


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/wards.geojson")
def wards_geojson(
    min_lon: float = Query(...),
    min_lat: float = Query(...),
    max_lon: float = Query(...),
    max_lat: float = Query(...),
    valid_for: Optional[str] = None,
    model_version: str = MODEL_VERSION,
):
    """
    Wards intersecting the requested viewport bbox that have at least one
    grid cell (unscored wards -- e.g. those covering only uninhabited/
    unmapped terrain -- are omitted), with their most recent ward_risk row.
    Ward geometry comes from the real village boundaries loaded via
    admin_boundaries.py --level village.

    Always bbox-scoped, same reasoning as grid.geojson: all 5,043 scored
    wards at once (even simplified, ~3MB) was still a multi-second main-
    thread parse/tile cost on every page load -- see the ward-panel
    flicker investigation. A typical viewport only needs a few hundred.
    """
    ts = valid_for or _latest_valid_for(model_version)
    if ts is None:
        return {"type": "FeatureCollection", "features": []}

    # Simplified + rounded to 5 decimal places (~1m) rather than PostGIS's
    # default 9 (~0.1mm) -- neither survey precision nor unsimplified
    # boundaries are visible at this map's zoom levels (starts at 7, a
    # state-wide view).
    sql = """
        SELECT
            w.ward_id, w.ward_name, w.population,
            wr.avg_risk, wr.max_risk, wr.high_risk_area_pct,
            wr.exposure_score, wr.ward_risk_score, wr.risk_category,
            wr.confidence, wr.valid_for,
            ST_AsGeoJSON(ST_SimplifyPreserveTopology(w.geom, 0.0005), 5) AS geometry
        FROM wards w
        JOIN ward_risk wr ON wr.ward_id = w.ward_id AND wr.valid_for = :valid_for
        WHERE ST_Intersects(w.geom, ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326))
    """
    df = query_df(
        sql,
        {
            "valid_for": ts,
            "min_lon": min_lon,
            "min_lat": min_lat,
            "max_lon": max_lon,
            "max_lat": max_lat,
        },
    )
    return _to_feature_collection(df)


@app.get("/wards/search")
def wards_search(q: str = Query(..., min_length=1), valid_for: Optional[str] = None, model_version: str = MODEL_VERSION):
    """
    Ward name search for the map's search box -- name + risk summary + a
    center point to fly the map to, no geometry (this must stay light
    enough to call on every keystroke).
    """
    ts = valid_for or _latest_valid_for(model_version)
    if ts is None:
        return []

    sql = """
        SELECT
            w.ward_id, w.ward_name, w.population,
            wr.risk_category, wr.ward_risk_score,
            ST_X(ST_Centroid(w.geom)) AS lon, ST_Y(ST_Centroid(w.geom)) AS lat
        FROM wards w
        JOIN ward_risk wr ON wr.ward_id = w.ward_id AND wr.valid_for = :valid_for
        WHERE w.ward_name ILIKE :q
        ORDER BY w.ward_name
        LIMIT 20
    """
    df = query_df(sql, {"valid_for": ts, "q": f"%{q}%"})
    return df.to_dict(orient="records")


@app.get("/grid.geojson")
def grid_geojson(
    min_lon: float = Query(...),
    min_lat: float = Query(...),
    max_lon: float = Query(...),
    max_lat: float = Query(...),
    valid_for: Optional[str] = None,
    model_version: str = MODEL_VERSION,
):
    """
    Fine-grid (100-250m) cells within the requested viewport bbox, with their
    latest combined_risk. Always bbox-scoped: the pilot grid is 439k cells,
    far too large to ever return whole to a browser.
    """
    ts = valid_for or _latest_valid_for(model_version)
    if ts is None:
        return {"type": "FeatureCollection", "features": [], "truncated": False}

    sql = f"""
        SELECT gc.cell_id, gc.ward_id, rp.combined_risk, rp.ml_probability,
               rp.hydrological_threat, rp.confidence,
               ST_AsGeoJSON(gc.geom, 5) AS geometry
        FROM grid_cells gc
        JOIN risk_predictions rp ON rp.cell_id = gc.cell_id
            AND rp.valid_for = :valid_for AND rp.model_version = :model_version
        WHERE ST_Intersects(gc.geom, ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326))
        LIMIT {MAX_GRID_CELLS_PER_REQUEST + 1}
    """
    df = query_df(
        sql,
        {
            "valid_for": ts,
            "model_version": model_version,
            "min_lon": min_lon,
            "min_lat": min_lat,
            "max_lon": max_lon,
            "max_lat": max_lat,
        },
    )
    truncated = len(df) > MAX_GRID_CELLS_PER_REQUEST
    fc = _to_feature_collection(df.head(MAX_GRID_CELLS_PER_REQUEST))
    fc["truncated"] = truncated
    return fc


@app.get("/ward/{ward_id}")
def ward_detail(ward_id: int, valid_for: Optional[str] = None, model_version: str = MODEL_VERSION):
    """
    Full ward detail card: risk score/category/confidence, rainfall
    accumulations, soil moisture, hydrological threat, high-risk-area%,
    population exposed. Averaged across the ward's own grid cells.

    lead_time_minutes is always null -- the hydrological engine only scores
    already-observed rainfall (see ffg_engine.py), there is no forecast
    ingestion yet, so a genuine lead time cannot be computed. See TODO.md.
    """
    ts = valid_for or _latest_valid_for(model_version)
    if ts is None:
        raise HTTPException(404, "No scored predictions exist yet.")

    ward_sql = """
        SELECT w.ward_id, w.ward_name, w.population, wr.avg_risk, wr.max_risk,
               wr.high_risk_area_pct, wr.exposure_score, wr.ward_risk_score,
               wr.risk_category, wr.confidence, wr.valid_for
        FROM wards w
        JOIN ward_risk wr ON wr.ward_id = w.ward_id AND wr.valid_for = :valid_for
        WHERE w.ward_id = :ward_id
    """
    ward_df = query_df(ward_sql, {"ward_id": ward_id, "valid_for": ts})
    if ward_df.empty:
        raise HTTPException(404, f"No ward_risk row for ward {ward_id} at {ts}.")
    ward = ward_df.iloc[0].to_dict()

    cell_sql = """
        SELECT
            avg(r.rain_mm_1h) AS rain_mm_1h, avg(r.rain_mm_3h) AS rain_mm_3h,
            avg(r.rain_mm_6h) AS rain_mm_6h, avg(r.rain_mm_12h) AS rain_mm_12h,
            avg(r.rain_mm_24h) AS rain_mm_24h,
            avg(sm.soil_moisture_pct) AS soil_moisture_pct,
            avg(rp.hydrological_threat) AS hydrological_threat,
            avg(rp.ml_probability) AS ml_probability
        FROM grid_cells gc
        JOIN rainfall r ON r.cell_id = gc.cell_id AND r.observed_at = :valid_for
        JOIN risk_predictions rp ON rp.cell_id = gc.cell_id
            AND rp.valid_for = :valid_for AND rp.model_version = :model_version
        LEFT JOIN LATERAL (
            SELECT soil_moisture_pct FROM soil_moisture sm
            WHERE sm.cell_id = gc.cell_id AND sm.observed_at <= r.observed_at
            ORDER BY sm.observed_at DESC LIMIT 1
        ) sm ON true
        WHERE gc.ward_id = :ward_id
    """
    cell_df = query_df(cell_sql, {"ward_id": ward_id, "valid_for": ts, "model_version": model_version})
    cell = cell_df.iloc[0].to_dict() if not cell_df.empty else {}

    return {
        "ward_id": ward["ward_id"],
        "ward_name": ward["ward_name"],
        "population_exposed": ward["population"],
        "flood_probability": cell.get("ml_probability"),
        "risk_level": ward["risk_category"],
        "confidence": ward["confidence"],
        "lead_time_minutes": None,  # no forecast ingestion yet -- see TODO.md
        "rainfall_mm": {
            "1h": cell.get("rain_mm_1h"),
            "3h": cell.get("rain_mm_3h"),
            "6h": cell.get("rain_mm_6h"),
            "12h": cell.get("rain_mm_12h"),
            "24h": cell.get("rain_mm_24h"),
        },
        "soil_moisture_pct": cell.get("soil_moisture_pct"),
        "hydrological_threat": cell.get("hydrological_threat"),
        "high_risk_area_pct": ward["high_risk_area_pct"],
        "avg_risk": ward["avg_risk"],
        "max_risk": ward["max_risk"],
        "ward_risk_score": ward["ward_risk_score"],
        "valid_for": ward["valid_for"],
    }


_TWI_BOUNDS_CACHE: dict[str, float] = {}


def _global_twi_bounds() -> tuple[float, float]:
    """
    5th/95th percentile of twi across the WHOLE grid, cached for the process
    lifetime (terrain_features doesn't change between pipeline reruns).
    Needed because a single ward's cells are too few to normalize against
    on their own -- see dynamic_rainfall_threshold()'s twi_low/twi_high docstring.
    """
    if not _TWI_BOUNDS_CACHE:
        df = query_df("SELECT percentile_cont(0.05) WITHIN GROUP (ORDER BY twi) AS low, "
                       "percentile_cont(0.95) WITHIN GROUP (ORDER BY twi) AS high FROM terrain_features")
        _TWI_BOUNDS_CACHE["low"] = float(df.iloc[0]["low"])
        _TWI_BOUNDS_CACHE["high"] = float(df.iloc[0]["high"])
    return _TWI_BOUNDS_CACHE["low"], _TWI_BOUNDS_CACHE["high"]


@app.get("/ward/{ward_id}/timeseries")
def ward_timeseries(ward_id: int):
    """
    Rainfall (rain_mm_6h) vs. the dynamic flood threshold for this ward's
    cells, over every timestamp the pipeline has scored -- what the rainfall
    panel's chart plots. dynamic_threshold_mm isn't persisted anywhere (only
    the derived hydrological_threat is), so it's recomputed here with the
    same function ffg_engine.py uses.
    """
    sql = """
        SELECT r.observed_at, r.rain_mm_6h, tf.slope_deg, tf.twi, tf.distance_to_stream_m,
               sm.antecedent_wetness_index
        FROM grid_cells gc
        JOIN rainfall r ON r.cell_id = gc.cell_id
        JOIN terrain_features tf ON tf.cell_id = gc.cell_id
        LEFT JOIN LATERAL (
            SELECT antecedent_wetness_index FROM soil_moisture sm
            WHERE sm.cell_id = gc.cell_id AND sm.observed_at <= r.observed_at
            ORDER BY sm.observed_at DESC LIMIT 1
        ) sm ON true
        WHERE gc.ward_id = :ward_id
    """
    df = query_df(sql, {"ward_id": ward_id})
    if df.empty:
        raise HTTPException(404, f"No rainfall history for ward {ward_id}.")

    df = df.dropna(subset=["slope_deg", "twi", "distance_to_stream_m", "antecedent_wetness_index"])
    twi_low, twi_high = _global_twi_bounds()
    df["dynamic_threshold_mm"] = dynamic_rainfall_threshold(
        df["antecedent_wetness_index"], df["slope_deg"], df["twi"], df["distance_to_stream_m"],
        twi_low=twi_low, twi_high=twi_high,
    )

    grouped = df.groupby("observed_at").agg(
        rain_mm_6h=("rain_mm_6h", "mean"), dynamic_threshold_mm=("dynamic_threshold_mm", "mean")
    ).reset_index().sort_values("observed_at")
    grouped["observed_at"] = grouped["observed_at"].astype(str)

    # NaN isn't valid JSON -- .where() on a float64 column coerces None back
    # to NaN unless cast to object first (same gotcha as db_utils.upsert_dataframe).
    grouped = grouped.astype(object).where(pd.notnull(grouped), None)
    return grouped.to_dict(orient="records")


@app.get("/wards/nearest")
def nearest_ward(lat: float, lon: float):
    """Default-selected ward for a user's location (item 3: 'ward user is in')."""
    sql = """
        SELECT ward_id, ward_name,
               ST_Distance(geom::geography, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography) AS distance_m
        FROM wards
        ORDER BY geom <-> ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)
        LIMIT 1
    """
    df = query_df(sql, {"lat": lat, "lon": lon})
    if df.empty:
        raise HTTPException(404, "No wards loaded.")
    return df.iloc[0].to_dict()


@app.get("/alerts")
def active_alerts(valid_for: Optional[str] = None, model_version: str = MODEL_VERSION):
    """
    Wards currently at HIGH/CRITICAL, for the alert panel. Not a persisted
    'alerts' table -- Phase 6 (real alert generation/dedup/lifecycle) isn't
    built; this is a live snapshot computed from the latest ward_risk.
    lead_time is always null for the same reason as /ward/{id}: no forecast
    rainfall is ingested, so there is nothing to project forward from.
    """
    ts = valid_for or _latest_valid_for(model_version)
    if ts is None:
        return []

    sql = """
        SELECT w.ward_id, w.ward_name, wr.risk_category, wr.ward_risk_score,
               wr.confidence, wr.valid_for
        FROM wards w
        JOIN ward_risk wr ON wr.ward_id = w.ward_id AND wr.valid_for = :valid_for
        WHERE wr.risk_category IN ('HIGH', 'CRITICAL')
        ORDER BY wr.ward_risk_score DESC
    """
    df = query_df(sql, {"valid_for": ts})
    df["lead_time_minutes"] = None
    return df.to_dict(orient="records")


@app.get("/watersheds.geojson")
def watersheds_geojson():
    """HydroBASINS level-8 sub-basin boundaries (optional map layer)."""
    df = query_df(
        "SELECT watershed_id, hybas_id, upstream_area_km2, "
        "ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom, 0.0005), 5) AS geometry FROM watersheds"
    )
    return _to_feature_collection(df)


@app.get("/iot-sensors.geojson")
def iot_sensors_geojson():
    """IoT sensor locations (map layer). Currently all synthetic -- see ml-pipeline/TODO.md."""
    df = query_df(
        "SELECT sensor_id, sensor_type, status, ST_AsGeoJSON(geom) AS geometry FROM iot_sensors"
    )
    return _to_feature_collection(df)


@app.get("/landslides.geojson")
def landslides_geojson():
    """Historical landslide points (map layer). Currently all synthetic -- see ml-pipeline/TODO.md."""
    df = query_df(
        "SELECT landslide_id, event_date, severity, source, ST_AsGeoJSON(geom) AS geometry FROM landslides"
    )
    return _to_feature_collection(df)


def _to_feature_collection(df: pd.DataFrame) -> dict:
    import json

    features = []
    for row in df.to_dict(orient="records"):
        geometry = json.loads(row.pop("geometry"))
        for k, v in row.items():
            if isinstance(v, pd.Timestamp):
                row[k] = v.isoformat()
        features.append({"type": "Feature", "geometry": geometry, "properties": row})
    return {"type": "FeatureCollection", "features": features}
