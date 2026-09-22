"""Runtime configuration, all overridable by environment variable."""

import os


DSN = os.environ.get(
    "FLOODSAFE_DSN", "postgresql://postgres@127.0.0.1:5433/floodsafe")

# Matches server.py: a point more than this far from any hazard polygon
# is treated as having no atlas coverage rather than borrowing a distant
# polygon's class.
HAZARD_MAX_KM = float(os.environ.get("FLOODSAFE_HAZARD_MAX_KM", "15"))

# SoilGrids was only sampled at known points, so soil context is only
# offered near one. Beyond this, soil is reported as null and simply
# drops out of the threshold multiplier.
SOIL_MAX_KM = float(os.environ.get("FLOODSAFE_SOIL_MAX_KM", "25"))

# One batched upstream call serves every client, cached this long. The
# Flask app learned this the hard way: per-browser fetching of the same
# shared zone list exhausted Open-Meteo's free daily quota.
RAINFALL_CACHE_TTL_SECONDS = int(
    os.environ.get("FLOODSAFE_RAINFALL_TTL", "600"))
RAINFALL_TIMEOUT_SECONDS = float(os.environ.get("FLOODSAFE_RAINFALL_TIMEOUT", "20"))
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

CORS_ORIGINS = [
    o.strip() for o in
    os.environ.get("FLOODSAFE_CORS_ORIGINS", "*").split(",")
    if o.strip()
]
