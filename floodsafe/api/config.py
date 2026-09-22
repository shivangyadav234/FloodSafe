"""Runtime configuration, all overridable by environment variable."""

import os


DSN = os.environ.get(
    "FLOODSAFE_DSN", "postgresql://postgres@127.0.0.1:5433/floodsafe")

# Matches server.py: a point more than this far from any hazard polygon
# is treated as having no atlas coverage rather than borrowing a distant
# polygon's class.
HAZARD_MAX_KM = float(os.environ.get("FLOODSAFE_HAZARD_MAX_KM", "15"))

# SoilGrids was only sampled at known points, so soil context is only
# offered essentially at one. Beyond this, soil is reported as null and
# drops out of the threshold multiplier.
#
# 0.5 km matches server.py's WATERSHED_SOIL_MAX_KM exactly, and the
# tightness is deliberate: soil texture is sampled per point, not
# interpolated, so borrowing a reading from even a few kilometres away
# would invent data. An earlier 25 km value here silently gave 296
# zones a soil group Flask correctly reports as unknown, which then
# scaled every one of their rainfall thresholds down by 10%.
SOIL_MAX_KM = float(os.environ.get("FLOODSAFE_SOIL_MAX_KM", "0.5"))

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
