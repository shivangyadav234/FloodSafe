"""
FloodSafe API (FastAPI + PostGIS).

Serves the same contract as the Flask app's /ffgs endpoints so the
existing front-end can be pointed here unchanged, while the work itself
moves into the database -- see queries.py.

Run:
    uvicorn floodsafe.api.main:app --reload --port 8000
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.concurrency import run_in_threadpool

from . import config, db, queries, rainfall, thresholds


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.open_pool()
    try:
        yield
    finally:
        db.close_pool()


app = FastAPI(
    title="FloodSafe API",
    version="0.1.0",
    summary="Flash-flood guidance for Uttarakhand, backed by PostGIS.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _soil(row: dict) -> dict | None:
    if not row.get("hydrologic_soil_group"):
        return None
    return {
        "sand_pct": row.get("sand_pct"),
        "clay_pct": row.get("clay_pct"),
        "silt_pct": row.get("silt_pct"),
        "hydrologic_soil_group": row["hydrologic_soil_group"],
    }


def _watershed(row: dict) -> dict | None:
    if row.get("hybas_id") is None:
        return None
    return {
        "hybas_id": row["hybas_id"],
        "up_area_km2": row.get("up_area_km2"),
        "sub_area_km2": row.get("sub_area_km2"),
    }


def _build_zone(row: dict, rain: dict | None) -> dict:
    effective_class, source = thresholds.resolve_class(
        row.get("hazard_class"), row.get("ffpi_band"))

    static_mult = thresholds.static_multiplier(
        row.get("hydrologic_soil_group"), row.get("up_area_km2"))

    antecedent = (rain or {}).get("antecedent_48h")
    bands = thresholds.thresholds_for_class(effective_class, antecedent, static_mult)

    per_duration, overall = {}, None
    for duration in thresholds.FFGS_DURATIONS:
        rain_mm = (rain or {}).get(duration)
        status = thresholds.status_for_duration(rain_mm, (bands or {}).get(duration))
        per_duration[duration] = {"rain_mm": rain_mm, "status": status}
        overall = thresholds.worse_status(overall, status)

    return {
        "name": row["name"],
        "parent_town": row.get("parent_town"),
        "kind": row["kind"],
        "lat": row["lat"],
        "lon": row["lon"],
        "hazard_class": row.get("hazard_class"),
        "effective_class": effective_class,
        "hazard_source": source,
        "exact_match": row.get("exact_match"),
        "distance_km": (round(row["distance_km"], 1)
                        if row.get("distance_km") is not None else None),
        "ffpi": row.get("ffpi"),
        "ffpi_band": row.get("ffpi_band"),
        "ffpi_components": row.get("components"),
        "model_prob": row.get("model_prob"),
        "terrain": row.get("terrain"),
        "watershed": _watershed(row),
        "soil": _soil(row),
        "static_multiplier": static_mult,
        "thresholds_mm": bands,
        "live_rainfall": rain,
        "per_duration": per_duration,
        "overall_status": overall,
    }


@app.get("/health", tags=["meta"])
async def health():
    def check():
        row = db.fetch_one("SELECT postgis_version() AS v")
        counts = db.fetch_one("""
            SELECT (SELECT count(*) FROM hazard_zones)  AS hazard_zones,
                   (SELECT count(*) FROM watersheds)    AS watersheds,
                   (SELECT count(*) FROM localities)    AS localities,
                   (SELECT count(*) FROM shelters)      AS shelters,
                   (SELECT count(*) FROM soil_samples)  AS soil_samples,
                   (SELECT count(*) FROM ffpi_scores)   AS ffpi_scores,
                   (SELECT count(*) FROM ffpi_raster)   AS ffpi_raster_tiles
        """)
        return row, counts

    try:
        row, counts = await run_in_threadpool(check)
    except Exception as exc:
        raise HTTPException(503, f"database unavailable: {exc}") from exc

    return {
        "status": "ok",
        "postgis": row["v"],
        "tables": counts,
        "rainfall_cache_age_s": rainfall.cache_age_seconds(),
    }


@app.get("/ffgs/zones", tags=["ffgs"])
async def ffgs_zones():
    """Every monitored zone with its class, thresholds and live status."""
    rows = await run_in_threadpool(queries.zone_rows)

    # Only zones that actually have a class are worth fetching rain for.
    classified = [
        r for r in rows
        if thresholds.resolve_class(r.get("hazard_class"), r.get("ffpi_band"))[0]
    ]
    points = [(r["lat"], r["lon"]) for r in classified]
    rain_by_point = await run_in_threadpool(rainfall.for_points, points)

    zones = [
        _build_zone(r, rain_by_point.get((r["lat"], r["lon"])))
        for r in classified
    ]

    return {
        "available": True,
        "durations": list(thresholds.FFGS_DURATIONS),
        "count": len(zones),
        "rainfall_cache_age_s": rainfall.cache_age_seconds(),
        "zones": zones,
    }


@app.get("/ffgs/point", tags=["ffgs"])
async def ffgs_point(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    antecedent_48h_mm: float | None = Query(None, ge=0),
    with_rainfall: bool = Query(True, description="Fetch live rainfall for this point"),
):
    """Guidance for an arbitrary point, classified in the database."""
    row = await run_in_threadpool(queries.point_row, lat, lon)
    if row is None:
        raise HTTPException(500, "classification query returned nothing")

    # A precomputed score is sampled from the 90 m raster; the raster
    # lookup is a regridded approximation, so prefer the former.
    if row.get("exact_ffpi") is not None:
        row["ffpi"] = row["exact_ffpi"]
        row["ffpi_band"] = row["exact_ffpi_band"]

    rain = None
    if with_rainfall:
        rain = await run_in_threadpool(rainfall.for_single_point, lat, lon)
        if antecedent_48h_mm is not None:
            rain = {**rain, "antecedent_48h": antecedent_48h_mm}
    elif antecedent_48h_mm is not None:
        rain = {"1h": None, "3h": None, "24h": None,
                "antecedent_48h": antecedent_48h_mm}

    zone = _build_zone({**row, "name": "requested point", "kind": "point",
                        "parent_town": None, "lat": lat, "lon": lon}, rain)
    return zone


@app.get("/shelters", tags=["shelters"])
async def list_shelters(evacuation_only: bool = False):
    rows = await run_in_threadpool(queries.shelters, evacuation_only)
    return {"count": len(rows), "shelters": rows}


@app.get("/shelters/nearest", tags=["shelters"])
async def nearest_shelters(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    limit: int = Query(5, ge=1, le=50),
    evacuation_only: bool = True,
):
    rows = await run_in_threadpool(
        queries.nearest_shelters, lat, lon, limit, evacuation_only)
    for r in rows:
        r["distance_km"] = round(r["distance_km"], 2)
    return {"count": len(rows), "shelters": rows}


@app.get("/layers/hazard-atlas.geojson", tags=["layers"])
async def hazard_atlas():
    return await run_in_threadpool(
        queries.layer_geojson, "hazard_zones", ["id", "hazard_class", "area_ha"])


@app.get("/layers/watersheds.geojson", tags=["layers"])
async def watersheds(simplify: float = Query(0.001, ge=0, le=0.05)):
    return await run_in_threadpool(
        queries.layer_geojson, "watersheds",
        ["hybas_id", "up_area_km2", "sub_area_km2"], simplify)
