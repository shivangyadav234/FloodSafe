import os
import json
import math
import time
import uuid
import requests

# ============================================================
# PROJ FIX
# ============================================================

PROJ_DATA = os.environ.get("PROJ_DATA_OVERRIDE", "")

if PROJ_DATA and os.path.isdir(PROJ_DATA):

    os.environ["PROJ_DATA"] = PROJ_DATA
    os.environ["PROJ_LIB"] = PROJ_DATA

elif PROJ_DATA:

    # Don't blindly point pyproj at a folder that doesn't exist —
    # that silently breaks all CRS transforms. Fall back to
    # whatever pyproj ships with instead of crashing at import time.
    print(
        "WARNING: PROJ_DATA_OVERRIDE path not found, "
        "falling back to pyproj's bundled data:",
        PROJ_DATA
    )

import pyproj

if PROJ_DATA and os.path.isdir(PROJ_DATA):
    pyproj.datadir.set_data_dir(PROJ_DATA)

# ============================================================
# FLASK
# ============================================================

from flask import Flask, jsonify, request, send_file, Response
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

# ============================================================
# ROUTING ENGINE
# ============================================================
#
# The routing engine is imported defensively. If the data files,
# graph, or dependencies behind it are missing or broken, the
# whole server should NOT fail to start — the map, search, and
# status endpoints should keep working, and /route should return
# a clear 503 explaining what's wrong instead of the app dying.
# ============================================================

ROUTING_ENGINE_AVAILABLE = False
ROUTING_ENGINE_ERROR = None

try:

    from routing_engine import (
        calculate_route, coordinates, shelters,
        find_nearest_shelter, find_nearest_hospital
    )

    ROUTING_ENGINE_AVAILABLE = True

except Exception as e:

    calculate_route = None
    coordinates = None
    shelters = []
    find_nearest_shelter = None
    find_nearest_hospital = None

    ROUTING_ENGINE_ERROR = str(e)

    print()
    print("WARNING: routing engine failed to load.")
    print("Routing (/route) will be unavailable until this is fixed.")
    print("Details:", repr(e))

# ============================================================
# APP
# ============================================================

app = Flask(__name__)
CORS(app)


# ============================================================
# GLOBAL ERROR HANDLING
#
# Any exception that isn't already caught inside a route ends up
# here instead of crashing the request or returning Flask's raw
# HTML error page. Normal HTTP errors (404, etc.) are passed
# through unchanged.
# ============================================================

@app.errorhandler(HTTPException)
def handle_http_exception(e):

    return jsonify({
        "status": "error",
        "error": e.name,
        "details": e.description
    }), e.code


@app.errorhandler(Exception)
def handle_unexpected_error(e):

    print()
    print("UNHANDLED SERVER ERROR:", repr(e))

    return jsonify({
        "status": "error",
        "error": "Unexpected server error",
        "details": str(e)
    }), 500

# ============================================================
# MAP FILE
# ============================================================

DATA_DIR = os.environ.get(
    "FLOODSAFE_DATA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
)

MAP_FILE = os.path.join(DATA_DIR, "uttarakhand_flood_map.html")


# ============================================================
# CROWDSOURCED HAZARD REPORTS
#
# Stored as a flat JSON file next to the other data files so
# reports survive a server restart without needing a database.
# Each report expires on its own after REPORT_EXPIRY_SECONDS so
# stale reports don't silently keep rerouting traffic forever.
# ============================================================

REPORTS_FILE = os.path.join(DATA_DIR, "reports.json")
REPORT_EXPIRY_SECONDS = 6 * 60 * 60
REPORT_DESCRIPTION_MAX_LENGTH = 300
REPORT_NAME_MAX_LENGTH = 80


def _load_reports():

    if not os.path.exists(REPORTS_FILE):
        return []

    try:

        with open(REPORTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, list):
            return []

        return data

    except (OSError, ValueError) as e:

        print("WARNING: failed to load reports.json:", repr(e))
        return []


def _save_reports(reports):
    """
    Write the report list atomically.

    Writing straight into reports.json meant a crash, restart or full
    disk partway through json.dump left a truncated file, which then
    failed to parse on next boot and silently reset every live hazard
    report to an empty list. Writing to a temporary file in the same
    directory and renaming it over the target makes the swap atomic on
    both POSIX and Windows, so a reader either sees the whole previous
    file or the whole new one.
    """

    tmp_path = REPORTS_FILE + ".tmp"

    try:

        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(reports, f)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_path, REPORTS_FILE)

    except OSError as e:

        print("WARNING: failed to save reports.json:", repr(e))

        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


# ============================================================
# RATE LIMITING
#
# Every write endpoint was previously unmetered, so a single client
# could submit hazard reports, resolutions or confirmations as fast as
# it could issue requests. For a public disaster feed that is both an
# abuse vector (flooding the map with false hazards, or resolving real
# ones) and a denial-of-service one, since each write rewrites the
# whole report file.
#
# Implemented in-process rather than with a dependency: the free tier
# runs a single gunicorn worker, so one process sees every request and
# a shared dict is sufficient. It would need Redis behind more than one
# worker, which is noted here so the assumption is not silently broken
# later.
#
# This is a courtesy limit against accidents and casual abuse, not a
# security control -- it keys on client IP, which a determined caller
# can vary.
# ============================================================

RATE_LIMITS = {
    # endpoint label -> (max requests, window seconds)
    "report": (5, 300),       # 5 new hazard reports per 5 minutes
    "report_action": (20, 300),  # resolve/confirm are cheaper, allow more
}

_rate_buckets = {}


def _client_ip():
    # Render terminates TLS upstream, so the real client address is in
    # X-Forwarded-For; take the first hop and fall back to the socket.
    forwarded = request.headers.get("X-Forwarded-For", "")

    if forwarded:
        return forwarded.split(",")[0].strip()

    return request.remote_addr or "unknown"


def _rate_limited(label):
    """
    True if this caller has exhausted `label`'s allowance. Sliding
    window: timestamps older than the window are dropped on each call,
    which also keeps the bucket from growing without bound.
    """

    max_requests, window = RATE_LIMITS[label]
    now = time.time()
    key = (label, _client_ip())

    hits = [t for t in _rate_buckets.get(key, []) if now - t < window]

    if len(hits) >= max_requests:
        _rate_buckets[key] = hits
        return True

    hits.append(now)
    _rate_buckets[key] = hits

    # Opportunistic cleanup so idle clients' buckets don't accumulate
    # for the lifetime of the process.
    if len(_rate_buckets) > 2000:
        for stale_key, stale_hits in list(_rate_buckets.items()):
            if not any(now - t < window for t in stale_hits):
                _rate_buckets.pop(stale_key, None)

    return False


def _rate_limit_response(label):
    max_requests, window = RATE_LIMITS[label]

    return jsonify({
        "status": "error",
        "error": (
            f"Too many requests. Limit is {max_requests} per "
            f"{window // 60} minutes."
        ),
    }), 429


_reports = _load_reports()

# Tracks which client IPs have already confirmed which report, purely
# to stop the same visitor inflating a count by clicking repeatedly.
# Deliberately in-memory only (not persisted) — losing this on a
# restart just means a handful of IPs could each confirm once more,
# which is a low-stakes trade-off for a soft, best-effort guard, not
# a real identity system.
_confirmed_ips_by_report = {}


def _get_client_ip():

    forwarded_for = request.headers.get("X-Forwarded-For", "")

    if forwarded_for:
        return forwarded_for.split(",")[0].strip()

    return request.remote_addr or "unknown"


def _active_reports():

    now = time.time()

    fresh = [
        report for report in _reports
        if now - report.get("timestamp", 0) < REPORT_EXPIRY_SECONDS
    ]

    if len(fresh) != len(_reports):

        expired_ids = (
            {r.get("id") for r in _reports}
            - {r.get("id") for r in fresh}
        )

        for report_id in expired_ids:
            _confirmed_ips_by_report.pop(report_id, None)

        _reports[:] = fresh
        _save_reports(_reports)

    return fresh


# ============================================================
# FLOOD GUIDANCE (headroom-to-risk panel)
#
# Answers one question per location: "how much more rain, right now,
# before this specific place is at flood risk?" It pairs the static
# hazard-atlas classification for a point with the live rainfall this
# app already pulls elsewhere (see RAINFALL_STATIONS in the landing
# page JS, and /town-rainfall below) and reports the remaining "headroom" —
# the gap between current rainfall and the level at which that hazard
# class is considered to be escalating.
#
# Each hazard class gets two thresholds instead of one flat trigger:
#   watch_mm    — rainfall at which the location moves from SAFE to WATCH
#   critical_mm — rainfall at which the location is treated as at risk
# watch_mm is set relative to critical_mm per class (not a fixed mm
# offset), so a low-tolerance EXTREME zone and a high-tolerance LOW
# zone each get a WATCH window sized to their own scale. These are a
# simplified heuristic calibrated against this app's own
# RAIN_HIGH_THRESHOLD_MM (see /town-rainfall) — not an official CWC/IMD
# Flash Flood Guidance value, which would need the full hydrological
# model this repo doesn't have.
#
# The hazard atlas only maps specific hazard-prone corridors
# (~5,340 km² of Uttarakhand's ~53,483 km²), not the whole state, so
# most points fall outside every polygon. Those are classified by
# nearest mapped zone when it's close enough, rather than silently
# defaulting to LOW.
# ============================================================

GUIDANCE_HAZARD_THRESHOLDS_MM = {
    "EXTREME": {"watch": 4.0, "critical": 8.0},
    "SIGNIFICANT": {"watch": 12.0, "critical": 20.0},
    "MODERATE": {"watch": 20.0, "critical": 35.0},
    "LOW": {"watch": 35.0, "critical": 60.0},
}

GUIDANCE_NEAREST_ZONE_MAX_KM = 15.0

# Same towns as RAINFALL_STATIONS in the landing page JS — kept in
# sync by name so the browser can pair this endpoint's hazard/threshold
# data with the rainfall it already fetches client-side, without a
# second round trip through this server.
GUIDANCE_TOWNS = [
    {"name": "Dehradun", "lat": 30.3165, "lon": 78.0322},
    {"name": "Rishikesh", "lat": 30.0869, "lon": 78.2676},
    {"name": "Haridwar", "lat": 29.9457, "lon": 78.1642},
    {"name": "Mussoorie", "lat": 30.4598, "lon": 78.0664},
    {"name": "Nainital", "lat": 29.3803, "lon": 79.4636},
    {"name": "Haldwani", "lat": 29.2183, "lon": 79.5130},
    {"name": "Almora", "lat": 29.5892, "lon": 79.6467},
    {"name": "Pithoragarh", "lat": 29.5822, "lon": 80.2181},
    {"name": "Joshimath", "lat": 30.5551, "lon": 79.5643},
]

_guidance_atlas = []
GUIDANCE_AVAILABLE = False
GUIDANCE_ERROR = None


def _load_guidance_atlas():
    """
    Loads the hazard atlas into a flat list of (geometry, hazard_class)
    pairs. Kept as a function (rather than inline module-level code) so
    the one try/except covers the whole load and any single malformed
    feature just gets skipped instead of aborting the load.
    """

    from shapely.geometry import shape as shapely_shape

    geojson_path = os.path.join(DATA_DIR, "uttarakhand_flash_flood_hazard_clean.geojson")

    with open(geojson_path, "r", encoding="utf-8") as f:
        atlas_geojson = json.load(f)

    polygons = []

    for feature in atlas_geojson.get("features", []):

        try:
            geom = shapely_shape(feature["geometry"])
            hazard_class = feature["properties"]["hazard"]
            polygons.append((geom, hazard_class))
        except Exception:
            continue

    return polygons


try:

    _guidance_atlas = _load_guidance_atlas()
    GUIDANCE_AVAILABLE = len(_guidance_atlas) > 0

    if not GUIDANCE_AVAILABLE:
        GUIDANCE_ERROR = "Hazard atlas loaded but contained no usable polygons."

except Exception as e:

    GUIDANCE_ERROR = str(e)
    print("WARNING: flood guidance hazard atlas unavailable:", repr(e))


def _haversine_km(lat1, lon1, lat2, lon2):
    """
    Great-circle distance in km. Used instead of a flat 111 km/degree
    scalar because a degree of longitude is only ~96-97 km (not 111)
    at Uttarakhand's latitude (~29-31N) — a flat conversion of a raw
    lon/lat Euclidean distance overstates real-world distance whenever
    the gap to a polygon is longitude-dominant.
    """

    r = 6371.0

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    )

    return 2 * r * math.asin(math.sqrt(a))


def classify_point(lat, lon):
    """
    Static hazard classification at a point. Returns
    (hazard_class, exact_match, distance_km) — hazard_class and
    distance_km are None if the atlas isn't loaded or the nearest
    mapped zone is farther than GUIDANCE_NEAREST_ZONE_MAX_KM away.
    """

    if not GUIDANCE_AVAILABLE:
        return None, False, None

    from shapely.geometry import Point as ShapelyPoint
    from shapely.ops import nearest_points

    point = ShapelyPoint(lon, lat)

    nearest_class = None
    nearest_km = None

    for geom, hazard_class in _guidance_atlas:

        if geom.contains(point):
            return hazard_class, True, 0.0

        # Rank by true ground distance, not by raw lon/lat degrees.
        #
        # This used to use geom.distance(point), whose degrees are
        # anisotropic: at 30N one degree of longitude is ~96.5 km
        # against ~111 km for latitude, so degree-distance understates
        # north-south separation by about 15%. That was assumed too
        # small to flip a ranking. It is not — the PostGIS parity check
        # caught three of 127 zones where it does, and every one of them
        # flipped between LOW and EXTREME:
        #
        #   Gagarigol  degrees picked EXTREME (0.828 km) over LOW (0.781 km)
        #   Garaser    degrees picked EXTREME (0.587 km) over LOW (0.579 km)
        #   shyaldoba  degrees picked LOW (0.799 km) over EXTREME (0.777 km)
        #
        # shyaldoba is the one that matters: a genuine EXTREME zone was
        # being served LOW thresholds, so it would not have reached
        # CRITICAL until 54 mm/h instead of 7.2 mm/h.
        #
        # nearest_points + haversine per polygon costs more than a
        # degree comparison, but there are only 191 polygons and the
        # result has to be right.
        nearest_on_geom = nearest_points(geom, point)[0]
        d = _haversine_km(lat, lon, nearest_on_geom.y, nearest_on_geom.x)

        if nearest_km is None or d < nearest_km:
            nearest_km = d
            nearest_class = hazard_class

    if nearest_km is None:
        return None, False, None

    if nearest_km > GUIDANCE_NEAREST_ZONE_MAX_KM:
        return None, False, nearest_km

    return nearest_class, False, nearest_km


def guidance_for_point(lat, lon):
    """
    Everything the client needs to render guidance for one point:
    its hazard classification plus the watch/critical rainfall
    thresholds for that class. Rainfall itself is fetched by the
    browser (same pattern as RAINFALL_STATIONS) and combined with
    this on the client, so headroom always reflects the freshest
    reading without an extra hop through this server.
    """

    hazard_class, exact, distance_km = classify_point(lat, lon)

    # Same atlas-then-FFPI resolution the FFGS page uses. Without it the
    # two pages disagreed about the same town: the landing page called
    # Mussoorie unmapped and offered no guidance at all, while /ffgs
    # classified it EXTREME with a 6.8 mm/h critical threshold. Six of
    # the nine towns were blank here purely because the surveyed atlas
    # does not reach them.
    ffpi = resolve_ffpi(lat, lon)
    effective_class, hazard_source = resolve_effective_class(hazard_class, ffpi)

    thresholds = (GUIDANCE_HAZARD_THRESHOLDS_MM.get(effective_class)
                  if effective_class else None)

    return {
        "lat": lat,
        "lon": lon,
        "hazard_class": hazard_class,
        "effective_class": effective_class,
        "hazard_source": hazard_source,
        "ffpi": ffpi["ffpi"] if ffpi else None,
        "exact_match": exact,
        "distance_km": round(distance_km, 1) if distance_km is not None else None,
        "watch_mm": thresholds["watch"] if thresholds else None,
        "critical_mm": thresholds["critical"] if thresholds else None,
    }


# GUIDANCE_ZONES is built further down, once ffpi_for_point exists --
# see below the FFPI section.


# ============================================================
# FLASH FLOOD GUIDANCE SYSTEM (FFGS) — duration-bucketed thresholds
#
# Extends the single watch/critical pair above (which only covers a
# "right now" reading) with thresholds for three rainfall durations:
# 1h, 3h and 24h. A short, sharp burst and a long, steady soak are
# different hazards even at the same total mm, so real Flash Flood
# Guidance products are duration-specific — this is a heuristic
# approximation of that shape, not an official CWC/IMD value (same
# caveat as GUIDANCE_HAZARD_THRESHOLDS_MM above, which this leaves
# untouched so the existing /flood-guidance panel keeps working
# unchanged while this is built out beside it).
#
# 1h values match GUIDANCE_HAZARD_THRESHOLDS_MM exactly. 3h and 24h
# scale up sub-linearly (longer windows need proportionally more total
# rain to reach the same runoff risk, since some of it infiltrates or
# runs off before the window closes) — roughly 1.8x at 3h and 4-5x at
# 24h relative to the 1h critical value, tightest for EXTREME zones
# (steep terrain, least infiltration capacity) and loosest for LOW.
# ============================================================

FFGS_DURATION_THRESHOLDS_MM = {
    "EXTREME": {
        "1h":  {"watch": 4.0,  "critical": 8.0},
        "3h":  {"watch": 8.0,  "critical": 15.0},
        "24h": {"watch": 20.0, "critical": 40.0},
    },
    "SIGNIFICANT": {
        "1h":  {"watch": 12.0, "critical": 20.0},
        "3h":  {"watch": 20.0, "critical": 35.0},
        "24h": {"watch": 45.0, "critical": 80.0},
    },
    "MODERATE": {
        "1h":  {"watch": 20.0, "critical": 35.0},
        "3h":  {"watch": 35.0, "critical": 55.0},
        "24h": {"watch": 70.0, "critical": 120.0},
    },
    "LOW": {
        "1h":  {"watch": 35.0,  "critical": 60.0},
        "3h":  {"watch": 55.0,  "critical": 90.0},
        "24h": {"watch": 110.0, "critical": 180.0},
    },
}

FFGS_DURATIONS = ("1h", "3h", "24h")

# Antecedent-rainfall adjustment: heuristic only — this repo has no
# soil-moisture model, so 48h antecedent rainfall is used as a rough
# stand-in for how saturated the ground already is. Wetter ground needs
# less fresh rain to produce the same runoff, so heavy antecedent rain
# scales every threshold above down. The multiplier is applied
# uniformly across all three duration buckets rather than modelling
# duration-specific saturation effects there's no data here to
# calibrate. Sorted highest floor first; the first breakpoint the
# antecedent total clears wins.
FFGS_ANTECEDENT_BREAKPOINTS_MM = [
    (100.0, 0.70),  # ground already very wet -> thresholds cut 30%
    (50.0, 0.85),   # moderately wet -> thresholds cut 15%
    (0.0, 1.0),     # dry / no data -> no adjustment
]


def _antecedent_multiplier(antecedent_48h_mm):

    if antecedent_48h_mm is None:
        return 1.0

    for floor_mm, multiplier in FFGS_ANTECEDENT_BREAKPOINTS_MM:
        if antecedent_48h_mm >= floor_mm:
            return multiplier

    return 1.0


# ============================================================
# WATERSHED + SOIL — real physical context feeding the same
# thresholds above, on top of the hazard-atlas class and antecedent
# rainfall.
#
# Watershed: HydroSHEDS/HydroBASINS v1c level-8 sub-basins (WWF),
# clipped to Uttarakhand — real, freely published watershed
# delineation, not derived in-house from a DEM. Classified live via
# point-in-polygon (same pattern as classify_point below), so it
# works for any point, including an arbitrary "check my location"
# lookup.
#
# Soil: SoilGrids v2.0 (ISRIC) sand/clay/silt texture, sampled once
# per known FFGS point by extract_watershed_soil.py and cached to
# watershed_soil.json — SoilGrids' own point-query API takes
# 2-20+ seconds per call and times out under load, so this is never
# fetched live. That means soil context is only available within
# WATERSHED_SOIL_MAX_KM of a point that script actually sampled (the
# 9 towns + every FFGS locality); an arbitrary point elsewhere gets
# None rather than a misleading nearby guess.
#
# Both feed a single static multiplier applied to the duration
# thresholds, alongside (not instead of) the antecedent-rainfall
# multiplier above. This is still a heuristic, same as every other
# adjustment in this file — real datasets feeding a simplified rule,
# not a calibrated hydrological model.
# ============================================================

_watershed_polygons = []
WATERSHED_AVAILABLE = False


def _load_watersheds():

    from shapely.geometry import shape as shapely_shape

    geojson_path = os.path.join(DATA_DIR, "uttarakhand_watersheds.geojson")

    with open(geojson_path, "r", encoding="utf-8") as f:
        watershed_geojson = json.load(f)

    basins = []

    for feature in watershed_geojson.get("features", []):
        try:
            geom = shapely_shape(feature["geometry"])
            props = feature["properties"]
            basins.append((geom, {
                "hybas_id": int(props["HYBAS_ID"]),
                "up_area_km2": float(props["UP_AREA"]),
                "sub_area_km2": float(props["SUB_AREA"]),
            }))
        except Exception:
            continue

    return basins


try:

    _watershed_polygons = _load_watersheds()
    WATERSHED_AVAILABLE = len(_watershed_polygons) > 0

except Exception as e:

    print("WARNING: watershed boundaries unavailable:", repr(e))


def classify_watershed(lat, lon):
    """
    The HydroBASINS sub-basin containing this point: hybas_id,
    upstream contributing area, and this sub-basin's own local area
    (both km^2). None if watershed data isn't loaded or the point
    falls outside every mapped sub-basin.
    """

    if not WATERSHED_AVAILABLE:
        return None

    from shapely.geometry import Point as ShapelyPoint

    point = ShapelyPoint(lon, lat)

    for geom, info in _watershed_polygons:
        if geom.contains(point):
            return info

    return None


WATERSHED_SOIL_FILE = os.path.join(DATA_DIR, "watershed_soil.json")
WATERSHED_SOIL_MAX_KM = 0.5


def _load_watershed_soil():

    if not os.path.exists(WATERSHED_SOIL_FILE):
        return []

    with open(WATERSHED_SOIL_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


_WATERSHED_SOIL_POINTS = _load_watershed_soil()


def soil_for_point(lat, lon):
    """
    Precomputed SoilGrids texture/Hydrologic Soil Group for this
    point (see the module docstring above for why this is a lookup,
    not a live fetch).
    """

    nearest, nearest_km = None, None

    for p in _WATERSHED_SOIL_POINTS:

        if not p.get("soil"):
            continue

        d = _haversine_km(lat, lon, p["lat"], p["lon"])

        if nearest_km is None or d < nearest_km:
            nearest_km, nearest = d, p

    if nearest is None or nearest_km > WATERSHED_SOIL_MAX_KM:
        return None

    return nearest["soil"]


# Soil infiltration adjustment: poorer-draining soil (Hydrologic Soil
# Group C/D) needs less rain to produce the same runoff as
# free-draining soil (A/B), so it scales thresholds down; better
# drainage scales them up slightly. Missing soil data leaves
# thresholds unchanged rather than guessing.
FFGS_SOIL_GROUP_MULTIPLIER = {
    "A": 1.15,
    "B": 1.0,
    "C": 0.9,
    "D": 0.8,
}

# Catchment-size adjustment: a small, steep headwater catchment
# concentrates a rain burst into runoff far faster than a point that
# already sits on a large river system with a huge, slower-responding
# upstream area — the flash-flood mechanism this system is named
# for. Smaller upstream catchments get a lower (more cautious)
# threshold. Sorted highest floor first, same convention as the
# antecedent breakpoints above.
FFGS_CATCHMENT_BREAKPOINTS_KM2 = [
    (1000.0, 1.0),   # large river system -> no additional adjustment
    (100.0, 0.95),   # medium catchment
    (0.0, 0.85),      # small, steep headwater catchment -> most cautious
]


def _soil_group_multiplier(hydrologic_soil_group):

    return FFGS_SOIL_GROUP_MULTIPLIER.get(hydrologic_soil_group, 1.0)


def _catchment_multiplier(up_area_km2):

    if up_area_km2 is None:
        return 1.0

    for floor_km2, multiplier in FFGS_CATCHMENT_BREAKPOINTS_KM2:
        if up_area_km2 >= floor_km2:
            return multiplier

    return 1.0


def physical_factors_for_point(lat, lon):
    """
    Real watershed + soil context for a point, plus the combined
    static threshold multiplier derived from them. Independent of
    live rainfall (unlike the antecedent multiplier), so this only
    needs the point's coordinates, not a rainfall reading.
    """

    watershed = classify_watershed(lat, lon)
    soil = soil_for_point(lat, lon)

    multiplier = 1.0

    if soil and soil.get("hydrologic_soil_group"):
        multiplier *= _soil_group_multiplier(soil["hydrologic_soil_group"])

    if watershed:
        multiplier *= _catchment_multiplier(watershed["up_area_km2"])

    return {
        "watershed": watershed,
        "soil": soil,
        "static_multiplier": round(multiplier, 3),
    }


# ============================================================
# FLASH FLOOD POTENTIAL INDEX (FFPI)
#
# The hazard atlas only covers ~9.7% of Uttarakhand's area, so
# classify_point() returns None for most of the state and those zones
# have always rendered as UNMAPPED with no thresholds at all. FFPI
# fills that gap.
#
# It is a physically-based susceptibility index on a 1-10 scale
# (Smith 2003, the approach used by NWS river forecast centres),
# computed offline by floodsafe/pipeline/build_ffpi.py as a weighted
# mean of four real datasets reindexed onto a common scale:
#
#   slope (x2)    Copernicus DEM GLO-30 (ESA) via WhiteboxTools
#   soil          SoilGrids v2.0 (ISRIC) -> NRCS Hydrologic Soil Group
#   land cover    ESA WorldCover 2021 v200
#   convergence   topographic wetness index from the same DEM
#
# Slope carries double weight because runoff concentration time is the
# dominant control on flash flooding.
#
# No training labels are involved, which is deliberate. A supervised
# model trained directly on the atlas classes reaches only ROC-AUC 0.66
# under honest spatial cross-validation (GroupKFold on source polygon;
# a random split inflates that to 0.99 by leaking neighbouring points).
# FFPI, having never seen those labels, independently reaches 0.626
# against them (p=0.004) and covers 100% of the state rather than 9.7%.
#
# This is a susceptibility index, not a probability and not a forecast,
# and is labelled as such wherever it surfaces. Thresholds derived from
# it are marked hazard_source="ffpi" so the UI never presents a modelled
# class as a surveyed one.
#
# Stored as a small uint8 lat/lon grid rather than a GeoTIFF so the web
# app needs only numpy -- see build_ffpi_lookup.py.
# ============================================================

FFPI_LOOKUP_FILE = os.path.join(DATA_DIR, "ffpi_lookup.npz")
LOCATION_SCORES_FILE = os.path.join(DATA_DIR, "location_scores.json")

_ffpi_grid = None
_ffpi_bounds = None
_ffpi_step = None
_ffpi_scale = None
_ffpi_nodata = None
FFPI_AVAILABLE = False
FFPI_ERROR = None

# Upper bound -> band name, matching build_ffpi.py's FFPI_BANDS.
FFPI_BANDS = [
    (3.5, "VERY LOW"),
    (4.5, "LOW"),
    (5.5, "MODERATE"),
    (6.5, "HIGH"),
    (99.0, "VERY HIGH"),
]

# FFPI band -> the hazard class whose thresholds best match it. Used
# only where the atlas has no coverage; an atlas class always wins.
FFPI_BAND_TO_HAZARD_CLASS = {
    "VERY HIGH": "EXTREME",
    "HIGH": "SIGNIFICANT",
    "MODERATE": "MODERATE",
    "LOW": "LOW",
    "VERY LOW": "LOW",
}


def _load_ffpi():

    global _ffpi_grid, _ffpi_bounds, _ffpi_step, _ffpi_scale, _ffpi_nodata

    import numpy as np

    with np.load(FFPI_LOOKUP_FILE) as z:
        _ffpi_grid = z["ffpi"]
        _ffpi_bounds = z["bounds"].tolist()
        _ffpi_step = float(z["step"])
        _ffpi_scale = float(z["scale"])
        _ffpi_nodata = int(z["nodata"])


try:
    _load_ffpi()
    FFPI_AVAILABLE = True
    print(f"FFPI grid loaded: {_ffpi_grid.shape[1]}x{_ffpi_grid.shape[0]} cells")
except Exception as exc:
    FFPI_ERROR = str(exc)
    print("WARNING: FFPI grid unavailable:", FFPI_ERROR)


def ffpi_band_for(value):

    if value is None:
        return None

    for upper, name in FFPI_BANDS:
        if value < upper:
            return name

    return FFPI_BANDS[-1][1]


def ffpi_for_point(lat, lon):
    """
    FFPI value and band for any point, or None outside the grid or
    where the source rasters had no data (glaciated terrain, mostly).
    """

    if not FFPI_AVAILABLE:
        return None

    lon_min, lat_min, lon_max, lat_max = _ffpi_bounds

    if not (lon_min <= lon <= lon_max and lat_min <= lat <= lat_max):
        return None

    col = int((lon - lon_min) / _ffpi_step)
    row = int((lat_max - lat) / _ffpi_step)

    rows, cols = _ffpi_grid.shape

    if not (0 <= row < rows and 0 <= col < cols):
        return None

    raw = int(_ffpi_grid[row, col])

    if raw == _ffpi_nodata:
        return None

    value = round(raw / _ffpi_scale, 2)

    return {"ffpi": value, "band": ffpi_band_for(value)}


def resolve_ffpi(lat, lon):
    """
    FFPI for a point, preferring the value score_locations.py sampled
    straight from the 90 m raster over the regridded lookup grid.

    The grid is an approximation: in steep terrain it can smooth a point
    clean across a band boundary -- Mussoorie reads 6.8 exactly and 6.28
    off the grid, which is the difference between EXTREME and
    SIGNIFICANT. Both pages must therefore resolve it the same way, so
    they share this rather than each holding a copy. They already
    disagreed about three towns when they did not.
    """

    scores = LOCATION_SCORES.get((round(lat, 5), round(lon, 5)))

    if scores and scores.get("ffpi") is not None:
        return {"ffpi": scores["ffpi"], "band": ffpi_band_for(scores["ffpi"])}

    return ffpi_for_point(lat, lon)


def resolve_effective_class(hazard_class, ffpi):
    """
    (effective_class, hazard_source) for a point.

    The surveyed atlas always wins where it has coverage. FFPI only
    stands in where it has none, and never silently: the source travels
    with the class so the UI can label a modelled one as modelled.
    """

    if hazard_class:
        return hazard_class, "atlas"

    if ffpi:
        return FFPI_BAND_TO_HAZARD_CLASS.get(ffpi["band"]), "ffpi"

    return None, None


def _load_location_scores():

    if not os.path.exists(LOCATION_SCORES_FILE):
        return {}

    with open(LOCATION_SCORES_FILE, "r", encoding="utf-8") as f:
        payload = json.load(f)

    return {
        (round(loc["lat"], 5), round(loc["lon"], 5)): loc
        for loc in payload.get("locations", [])
    }


# Keyed by rounded lat/lon so the fixed zone list can pick up the
# XGBoost probability and FFPI component breakdown that
# score_locations.py precomputed. Arbitrary points get FFPI from the
# grid above but no model probability -- running XGBoost at request
# time would mean shipping the model and its feature rasters to the web
# instance for a signal that is explicitly secondary.
LOCATION_SCORES = _load_location_scores()


def ffgs_thresholds_for_class(hazard_class, antecedent_48h_mm=None, static_multiplier=1.0):
    """
    Returns {"1h": {"watch", "critical"}, "3h": {...}, "24h": {...}}
    for one hazard class, with the antecedent-rainfall adjustment and
    the watershed/soil static_multiplier both applied. None if
    hazard_class is unmapped/unknown.
    """

    base = FFGS_DURATION_THRESHOLDS_MM.get(hazard_class)

    if base is None:
        return None

    multiplier = _antecedent_multiplier(antecedent_48h_mm) * static_multiplier

    return {
        duration: {
            "watch": round(vals["watch"] * multiplier, 1),
            "critical": round(vals["critical"] * multiplier, 1),
        }
        for duration, vals in base.items()
    }


def ffgs_guidance_for_point(lat, lon, antecedent_48h_mm=None):
    """
    Like guidance_for_point() above, but returns the full
    duration-bucketed threshold set instead of a single watch/critical
    pair, plus watershed and soil context. This function itself never
    touches rainfall — /ffgs/point (a single arbitrary point) still
    has the browser fetch rainfall directly from Open-Meteo, same
    reasoning as the /town-rainfall rate-limit note further down; /ffgs/zones
    (the fixed, shared zone list) instead merges in a server-side
    cached rainfall fetch — see _fetch_ffgs_live_rainfall above.
    """

    hazard_class, exact, distance_km = classify_point(lat, lon)
    physical = physical_factors_for_point(lat, lon)

    scores = LOCATION_SCORES.get((round(lat, 5), round(lon, 5)))
    ffpi = resolve_ffpi(lat, lon)

    effective_class, hazard_source = resolve_effective_class(hazard_class, ffpi)

    thresholds = ffgs_thresholds_for_class(
        effective_class, antecedent_48h_mm, physical["static_multiplier"])

    return {
        "lat": lat,
        "lon": lon,
        "hazard_class": hazard_class,
        "effective_class": effective_class,
        "hazard_source": hazard_source,
        "exact_match": exact,
        "distance_km": round(distance_km, 1) if distance_km is not None else None,
        "thresholds_mm": thresholds,
        "watershed": physical["watershed"],
        "soil": physical["soil"],
        "static_multiplier": physical["static_multiplier"],
        "ffpi": ffpi["ffpi"] if ffpi else None,
        "ffpi_band": ffpi["band"] if ffpi else None,
        "ffpi_components": scores["ffpi_components"] if scores else None,
        # Trained on 206 real recorded disasters (NASA Global Landslide
        # Catalog); spatially blocked CV ROC-AUC 0.82. Supersedes
        # model_prob, which was trained on the hazard atlas and reaches
        # only 0.66 because those labels separate on elevation and
        # little else. Both are kept so the improvement stays visible.
        "event_prob": scores["event_prob"] if scores else None,
        "model_prob": scores["model_prob"] if scores else None,
    }


# Computed once at startup, same reasoning as GUIDANCE_ZONES above.
# Antecedent rainfall isn't known at startup (or in bulk, without
# re-introducing the server-side Open-Meteo fan-out that caused the
# Render rate-limit bug), so these carry unadjusted base thresholds;
# the antecedent adjustment is only applied on the single-point
# /ffgs/point lookup, where the browser supplies the antecedent total
# it already fetched for that one location.
FFGS_TOWN_ZONES = [
    dict(ffgs_guidance_for_point(town["lat"], town["lon"]), name=town["name"], kind="town", parent_town=None)
    for town in GUIDANCE_TOWNS
]


# ============================================================
# FFGS locality-level granularity
#
# extract_localities.py pulls real, named OSM localities via two
# passes: near a known anchor town (place=suburb/neighbourhood/
# quarter), and separately anything within 1km of a MODERATE/
# SIGNIFICANT/EXTREME hazard polygon specifically (place=village/
# hamlet included there too — see its own header comment for why
# that's fine for the second pass but not the first). Whether any of
# them end up in FFGS_LOCALITY_ZONES depends entirely on the hazard
# atlas separately covering that exact point — same
# nearest-zone-within-15km rule as everything else in this file,
# applied per locality rather than per town.
# ============================================================

LOCALITIES_FILE = os.path.join(DATA_DIR, "localities.json")


def _load_localities():

    if not os.path.exists(LOCALITIES_FILE):
        return []

    with open(LOCALITIES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


# Loaded once and reused both for FFGS's hazard-filtered zone list
# below and for the nearest-locality lookup used to label hazard
# reports and shelters further down (see nearest_locality_for_point).
ALL_LOCALITIES = _load_localities()

FFGS_LOCALITY_ZONES = []

for _loc in ALL_LOCALITIES:

    _zone = ffgs_guidance_for_point(_loc["lat"], _loc["lon"])

    # Previously this required hazard-atlas coverage, which dropped most
    # localities. FFPI now supplies a class wherever the atlas doesn't,
    # so a zone only drops out if neither source can place it.
    if not _zone["effective_class"]:
        continue

    FFGS_LOCALITY_ZONES.append(dict(
        _zone,
        name=_loc["name"],
        kind="locality",
        parent_town=_loc["town"]
    ))

FFGS_ZONES = FFGS_TOWN_ZONES + FFGS_LOCALITY_ZONES


# Deferred from its definition above so it can use ffpi_for_point.
# Computed once at startup: the atlas and the town list are both static,
# so there is no reason to redo ~191-polygon point checks per request.
GUIDANCE_ZONES = [
    dict(guidance_for_point(town["lat"], town["lon"]), name=town["name"])
    for town in GUIDANCE_TOWNS
]


# ============================================================
# NEAREST-LOCALITY LOOKUP (for labeling hazard reports and shelters)
#
# Unlike FFGS_LOCALITY_ZONES above, this isn't gated on hazard-atlas
# coverage — it's purely "what's the closest named place to this
# point," used as human-readable context on a report or shelter
# ("near Muni Ki Reti, Rishikesh") regardless of whether that spot
# happens to fall inside a mapped hazard zone. Returns None past
# NEAREST_LOCALITY_MAX_KM rather than always attaching some distant,
# misleading locality name.
# ============================================================

NEAREST_LOCALITY_MAX_KM = 5.0


def nearest_locality_for_point(lat, lon):

    nearest, nearest_km = None, None

    for loc in ALL_LOCALITIES:
        d = _haversine_km(lat, lon, loc["lat"], loc["lon"])
        if nearest_km is None or d < nearest_km:
            nearest_km, nearest = d, loc

    if nearest is None or nearest_km > NEAREST_LOCALITY_MAX_KM:
        return None

    return {
        "name": nearest["name"],
        "town": nearest["town"],
        "distance_km": round(nearest_km, 2)
    }


# Computed once at startup, same reasoning as FFGS_ZONES above --
# shelters is a static list loaded once at import time (see the
# routing_engine import near the top of this file), so there's no
# reason to redo this per shelter on every /shelters request.
SHELTERS_WITH_LOCALITY = [
    dict(shelter, nearest_locality=nearest_locality_for_point(shelter["lat"], shelter["lon"]))
    for shelter in shelters
]


# ============================================================
# LANDING PAGE
#
# The site's front door. Pulls live numbers from this same
# server's own endpoints (/status, /shelters, /reports, /town-rainfall)
# via same-origin fetches in the browser, so the page always
# reflects the live deployment instead of baked-in copy.
# ============================================================

LANDING_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FloodSafe — flood-aware road advisory for Uttarakhand</title>
<style>

:root {
  --navy: #0b3558;
  --navy-dark: #062338;
  --ink: #1a1f24;
  --muted: #4a5560;
  --faint: #6b7680;
  --border: #c9d2d9;
  --border-strong: #9fb0bd;
  --bg: #f3f5f6;
  --panel: #ffffff;
  --notice-bg: #fff8e1;
  --notice-border: #b5860f;
  --safe: #14532d;
  --safe-bg: #eaf3ec;
  --risk: #7a1f1f;
  --risk-bg: #f7eceb;
  --link: #0b3558;
}

* { box-sizing: border-box; }

html {
  font-size: 100%;
}

body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: -apple-system, "Segoe UI", Verdana, Arial, sans-serif;
  font-size: 0.9375rem;
  line-height: 1.55;
}

h1, h2, h3 {
  color: var(--navy);
  margin: 0;
  font-weight: 700;
  text-wrap: balance;
}

a { color: var(--link); }
a:hover { text-decoration: none; }

.wrap { max-width: 1080px; margin: 0 auto; padding: 0 20px; }

code, .mono { font-family: 'Consolas', 'Courier New', monospace; }

/* ---------------- SKIP LINK ---------------- */

.skip-link {
  position: absolute;
  left: -999px;
  top: 0;
  background: var(--navy);
  color: white;
  padding: 8px 14px;
  z-index: 100;
}
.skip-link:focus { left: 8px; top: 8px; }

/* ---------------- UTILITY BAR ---------------- */

.utility-bar {
  background: var(--navy-dark);
  color: #cfe0ee;
  font-size: 0.75rem;
}
.utility-bar .wrap {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 5px 20px;
  gap: 14px;
  flex-wrap: wrap;
}
.utility-bar a { color: #cfe0ee; text-decoration: none; }
.utility-bar a:hover { text-decoration: underline; }

.utility-right { display: flex; align-items: center; gap: 18px; flex-wrap: wrap; }

.lang-toggle { display: flex; align-items: center; gap: 6px; }
.lang-toggle button {
  background: transparent;
  border: none;
  color: #9db4c9;
  font-size: 0.75rem;
  cursor: pointer;
  padding: 2px 3px;
  font-family: inherit;
}
.lang-toggle button.active {
  color: white;
  font-weight: 700;
  text-decoration: underline;
}
.lang-toggle .sep { color: #3a5674; }

.text-size-controls { display: flex; align-items: center; gap: 6px; }
.text-size-controls button {
  background: transparent;
  border: 1px solid #3a5674;
  color: #cfe0ee;
  border-radius: 3px;
  padding: 1px 7px;
  cursor: pointer;
  font-size: 0.6875rem;
}
.text-size-controls button:hover { background: #123553; }

/* ---------------- HEADER ---------------- */

header.site-header {
  background: var(--panel);
  border-bottom: 3px solid var(--navy);
}
header.site-header .wrap {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 20px;
  flex-wrap: wrap;
  gap: 12px;
}

.brand { display: flex; align-items: center; gap: 12px; text-decoration: none; }
.brand .mark {
  width: 42px; height: 42px;
  border: 2px solid var(--navy);
  border-radius: 4px;
  display: flex; align-items: center; justify-content: center;
  font-size: 1.25rem;
  color: var(--navy);
  background: #eef3f7;
  flex-shrink: 0;
}
.brand-text .name { font-size: 1.1875rem; font-weight: 700; color: var(--navy); line-height: 1.1; }
.brand-text .tagline { font-size: 0.75rem; color: var(--muted); }

nav.main-nav { display: flex; align-items: center; gap: 22px; flex-wrap: wrap; }
nav.main-nav a.nav-link {
  color: var(--ink);
  text-decoration: none;
  font-size: 0.875rem;
  font-weight: 600;
  border-bottom: 2px solid transparent;
  padding-bottom: 3px;
}
nav.main-nav a.nav-link:hover { border-bottom-color: var(--navy); }

.btn-official {
  display: inline-block;
  background: var(--navy);
  color: white !important;
  border: 1px solid var(--navy);
  padding: 8px 16px;
  font-size: 0.84375rem;
  font-weight: 700;
  text-decoration: none;
  border-radius: 3px;
}
.btn-official:hover { background: var(--navy-dark); }

.btn-outline {
  display: inline-block;
  background: white;
  color: var(--navy) !important;
  border: 1px solid var(--navy);
  padding: 8px 16px;
  font-size: 0.84375rem;
  font-weight: 700;
  text-decoration: none;
  border-radius: 3px;
}
.btn-outline:hover { background: #eef3f7; }

/* ---------------- SECTIONS (shared) ---------------- */

main section {
  padding: 30px 0;
  border-bottom: 1px solid var(--border);
}

.section-label {
  font-size: 0.71875rem;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--muted);
  margin-bottom: 8px;
}

.panel {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 3px;
}

/* ---------------- HERO ---------------- */

.advisory-box {
  background: var(--notice-bg);
  border: 1px solid var(--notice-border);
  border-left: 5px solid var(--notice-border);
  border-radius: 2px;
  padding: 10px 14px;
  font-size: 0.8125rem;
  color: #5c4300;
  margin-bottom: 18px;
  display: flex;
  gap: 8px;
  align-items: baseline;
}
.advisory-box b { color: #4a3600; }

.hero-grid {
  display: grid;
  grid-template-columns: 1.1fr 0.9fr;
  gap: 30px;
  align-items: start;
  padding-top: 8px;
}

.hero h1 {
  font-size: 2rem;
  line-height: 1.25;
}
.hero h1 em { font-style: normal; color: var(--safe); }

.hero p.sub {
  font-size: 0.90625rem;
  color: var(--muted);
  max-width: 480px;
  margin: 14px 0 20px;
}

.hero-ctas { display: flex; gap: 10px; flex-wrap: wrap; }

/* ---------------- DIAGRAM PANEL ---------------- */

.diagram-panel {
  padding: 14px;
}
.diagram-panel .diagram-caption {
  font-size: 0.75rem;
  color: var(--muted);
  margin-bottom: 8px;
  text-align: center;
}
.diagram-panel svg { width: 100%; height: auto; display: block; }

.route-hit { fill: none; stroke: transparent; stroke-width: 22; cursor: pointer; pointer-events: stroke; }
.route-safe-line { transition: opacity .2s ease, stroke-width .2s ease; }
.route-risk-line { transition: opacity .2s ease, stroke-width .2s ease; stroke-dasharray: 5 6; }
.route-dimmed { opacity: 0.25 !important; }
.route-active { stroke-width: 4.5; }
.route-dot { opacity: 0; pointer-events: none; }

.extreme-marker { opacity: 0; transition: opacity .2s ease; pointer-events: none; }
.extreme-marker.show { opacity: 1; }
.extreme-marker text {
  font-family: 'Consolas', monospace;
  font-size: 0.5625rem;
  font-weight: 700;
  fill: var(--risk);
}

.diagram-legend {
  display: flex; gap: 16px; justify-content: center;
  font-size: 0.75rem; color: var(--muted);
  margin-top: 8px; flex-wrap: wrap;
}
.diagram-legend span { display: inline-flex; align-items: center; gap: 5px; }
.diagram-legend .sw { width: 14px; height: 3px; display: inline-block; }

.diagram-hint {
  text-align: center;
  font-size: 0.71875rem;
  color: var(--faint);
  margin-top: 6px;
}

/* ---------------- LIVE STATUS ---------------- */

.status-line {
  display: flex; align-items: center; gap: 8px;
  font-size: 0.8125rem; color: var(--muted);
  margin-bottom: 14px;
}
.status-dot {
  width: 9px; height: 9px; border-radius: 50%;
  background: var(--safe);
  border: 1px solid #0d3d1f;
}
.status-dot.off { background: var(--risk); border-color: #5c1414; }

table.stats-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.84375rem;
}
table.stats-table th, table.stats-table td {
  border: 1px solid var(--border);
  padding: 10px 12px;
  text-align: left;
}
table.stats-table th {
  background: var(--navy);
  color: white;
  font-size: 0.71875rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
table.stats-table tr:nth-child(even) td { background: #f7f9fa; }
table.stats-table .stat-value {
  font-family: 'Consolas', monospace;
  font-weight: 700;
  font-size: 0.9375rem;
  color: var(--navy);
}

.stat-station {
  font-family: -apple-system, "Segoe UI", Verdana, Arial, sans-serif;
  font-weight: 400;
  font-size: 0.78125rem;
  color: var(--muted);
}

.legend-row {
  display: flex; gap: 18px; flex-wrap: wrap;
  font-size: 0.78125rem; color: var(--muted);
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px dashed var(--border);
}
.legend-row span { display: inline-flex; align-items: center; gap: 6px; }
.legend-row .dot { width: 10px; height: 10px; border-radius: 2px; }

/* ---------------- COMPARISON TABLE ---------------- */

table.compare-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.84375rem;
  margin-top: 14px;
}
table.compare-table th, table.compare-table td {
  border: 1px solid var(--border);
  padding: 9px 12px;
  text-align: left;
}
table.compare-table th {
  background: var(--navy);
  color: white;
  font-size: 0.71875rem;
  text-transform: uppercase;
}
table.compare-table td.num { font-family: 'Consolas', monospace; text-align: right; }
table.compare-table tr.row-safe td { background: var(--safe-bg); }
table.compare-table tr.row-risk td { background: var(--risk-bg); }

.compare-note {
  font-size: 0.78125rem;
  color: var(--muted);
  margin-top: 8px;
  font-style: italic;
}

/* ---------------- FEATURES ---------------- */

.feature-list {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 0;
  margin-top: 14px;
  border: 1px solid var(--border);
  border-radius: 3px;
  overflow: hidden;
}
.feature-item {
  padding: 16px 18px;
  border-right: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
  background: var(--panel);
}
.feature-item:nth-child(2n) { border-right: none; }
.feature-item h3 {
  font-size: 0.90625rem;
  margin-bottom: 6px;
  display: flex; align-items: center; gap: 7px;
}
.feature-item .num {
  display: inline-flex; align-items: center; justify-content: center;
  width: 20px; height: 20px;
  border: 1px solid var(--navy);
  color: var(--navy);
  font-size: 0.6875rem; font-weight: 700;
  border-radius: 2px;
  flex-shrink: 0;
}
.feature-item p { font-size: 0.8125rem; color: var(--muted); margin: 0; }

/* ---------------- SOURCES ---------------- */

.sources-list {
  margin-top: 10px;
  font-size: 0.8125rem;
  color: var(--muted);
}
.sources-list dt { font-weight: 700; color: var(--ink); float: left; clear: left; width: 130px; }
.sources-list dd { margin: 0 0 6px 140px; }

/* ---------------- FINAL CTA ---------------- */

.final-cta { text-align: center; padding: 36px 0; }
.final-cta h2 { font-size: 1.375rem; margin-bottom: 8px; }
.final-cta p { color: var(--muted); margin: 0 0 16px; font-size: 0.84375rem; }

/* ---------------- FOOTER ---------------- */

footer.site-footer {
  background: var(--navy-dark);
  color: #b9cce0;
  font-size: 0.78125rem;
  padding: 22px 0;
}
footer.site-footer .footer-links {
  display: flex; gap: 16px; flex-wrap: wrap;
  margin-bottom: 10px;
}
footer.site-footer a { color: #cfe0ee; text-decoration: none; }
footer.site-footer a:hover { text-decoration: underline; }
footer.site-footer .disclaimer {
  border-top: 1px solid #1c3f5c;
  padding-top: 10px;
  margin-top: 10px;
  color: #92a8bd;
  font-size: 0.71875rem;
  line-height: 1.6;
}

@media (max-width: 820px) {
  .hero-grid { grid-template-columns: 1fr; }
  .feature-list { grid-template-columns: 1fr; }
  .feature-item:nth-child(2n) { border-right: 1px solid var(--border); }
  .sources-list dt { float: none; width: auto; }
  .sources-list dd { margin-left: 0; }
}

/* ---------------- FLOOD GUIDANCE ---------------- */

.guidance-intro { font-size: 0.84375rem; color: var(--muted); max-width: 720px; margin: 8px 0 16px; }

table.guidance-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.84375rem;
}
table.guidance-table th, table.guidance-table td {
  border: 1px solid var(--border);
  padding: 9px 12px;
  text-align: left;
}
table.guidance-table th {
  background: var(--navy);
  color: white;
  font-size: 0.71875rem;
  text-transform: uppercase;
}
table.guidance-table td.num { font-family: 'Consolas', monospace; text-align: right; }
table.guidance-table tr:nth-child(even) td { background: #f7f9fa; }

.modelled-note { color: #6b7680; font-weight: normal; font-size: 11px; font-style: italic; }
.guidance-badge {
  display: inline-block;
  padding: 2px 9px;
  border-radius: 3px;
  font-size: 0.71875rem;
  font-weight: 700;
  text-transform: uppercase;
}
.guidance-safe { background: var(--safe-bg); color: var(--safe); }
.guidance-watch { background: #fff2d9; color: #8a5a00; }
.guidance-critical { background: var(--risk-bg); color: var(--risk); }
.guidance-unmapped { background: #eceff1; color: var(--muted); }

.guidance-mylocation {
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px dashed var(--border);
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
}
.guidance-my-result { font-size: 0.84375rem; color: var(--ink); }

</style>
<script src="/rainfall-fallback.js"></script>
</head>
<body>

<a class="skip-link" href="#main-content" data-i18n="skipLink">Skip to main content</a>

<div class="utility-bar">
  <div class="wrap">
    <span data-i18n="utilityTitle">FloodSafe Portal — Flood-Aware Road Advisory Service</span>
    <div class="utility-right">
      <div class="lang-toggle" role="group" aria-label="Language selector">
        <button type="button" data-lang="en" class="active">English</button>
        <span class="sep">|</span>
        <button type="button" data-lang="hi">हिंदी</button>
      </div>
      <div class="text-size-controls">
        <span data-i18n="textSizeLabel">Text size:</span>
        <button type="button" id="textSmaller" aria-label="Decrease text size">A-</button>
        <button type="button" id="textReset" aria-label="Reset text size">A</button>
        <button type="button" id="textLarger" aria-label="Increase text size">A+</button>
      </div>
    </div>
  </div>
</div>

<header class="site-header">
  <div class="wrap">
    <a class="brand" href="/">
      <span class="mark">⚑</span>
      <span class="brand-text">
        <span class="name">FloodSafe</span>
        <span class="tagline" data-i18n="brandTagline">Flood-Aware Road Advisory — Uttarakhand</span>
      </span>
    </a>
    <nav class="main-nav">
      <a class="nav-link" href="#status" data-i18n="navStatus">Live Status</a>
      <a class="nav-link" href="#flood-guidance" data-i18n="navGuidance">Flood Guidance</a>
      <a class="nav-link" href="/ffgs" data-i18n="navFfgs">FFGS</a>
      <a class="nav-link" href="#features" data-i18n="navServices">Services</a>
      <a class="nav-link" href="/reports-view" data-i18n="navReports">Hazard Reports</a>
      <a class="btn-official" href="/app" data-i18n="navOpenMap">Open Map Tool</a>
    </nav>
  </div>
</header>

<main id="main-content">

<section class="hero">
  <div class="wrap">
    <div class="advisory-box">
      <span>⚠</span>
      <span><b data-i18n="advisoryLabel">Public Advisory:</b> <span data-i18n="advisoryText">Road conditions can change rapidly during monsoon season. Always verify local conditions before travel.</span></span>
    </div>
    <div class="hero-grid">
      <div>
        <h1 data-i18n-html="heroTitle">Fastest isn't always <em>safe</em>.</h1>
        <p class="sub" data-i18n="heroSub">FloodSafe advises road routes across Uttarakhand using a georeferenced flash-flood hazard classification, live rainfall data, and citizen-reported hazards — instead of distance alone.</p>
        <div class="hero-ctas">
          <a class="btn-official" href="/app" data-i18n="heroCta1">Open the Map Tool →</a>
          <a class="btn-outline" href="#status" data-i18n="heroCta2">View Live Status</a>
        </div>
      </div>
      <div class="panel diagram-panel">
        <div class="diagram-caption" data-i18n="diagramCaption">Illustrative example — hover a route below</div>
        <svg viewBox="0 0 340 280" xmlns="http://www.w3.org/2000/svg" id="heroSvg">
          <path id="riskPath" class="route-risk-line" d="M20 230 Q 90 160 140 180 T 260 110 Q 300 80 320 40" fill="none" stroke="#7a1f1f" stroke-width="2.5" stroke-linecap="round"/>
          <path id="safePath" class="route-safe-line" d="M20 230 Q 70 210 100 225 Q 150 250 180 210 Q 210 170 190 130 Q 170 85 210 65 Q 260 40 320 40" fill="none" stroke="#14532d" stroke-width="3" stroke-linecap="round"/>

          <path id="riskHit" class="route-hit" d="M20 230 Q 90 160 140 180 T 260 110 Q 300 80 320 40"/>
          <path id="safeHit" class="route-hit" d="M20 230 Q 70 210 100 225 Q 150 250 180 210 Q 210 170 190 130 Q 170 85 210 65 Q 260 40 320 40"/>

          <circle cx="20" cy="230" r="4.5" fill="#1a1f24"/>
          <circle cx="320" cy="40" r="4.5" fill="#1a1f24"/>

          <circle id="riskDot" class="route-dot" r="5" fill="#7a1f1f"/>
          <circle id="safeDot" class="route-dot" r="5" fill="#14532d"/>

          <g id="extremeMarker" class="extreme-marker">
            <circle cx="197" cy="171" r="5" fill="#7a1f1f"/>
            <text x="206" y="168">EXTREME</text>
          </g>
        </svg>
        <div class="diagram-legend">
          <span><span class="sw" style="background:#14532d;"></span><span data-i18n="legendSafe">Safest route</span></span>
          <span><span class="sw" style="background:#7a1f1f;"></span><span data-i18n="legendRisk">Fastest route</span></span>
        </div>
        <div class="diagram-hint" data-i18n="diagramHint">Hover either route to trace it live</div>
      </div>
    </div>
  </div>
</section>

<section id="status">
  <div class="wrap">
    <div class="section-label" data-i18n="statusLabel">Live System Status</div>
    <div class="status-line">
      <span class="status-dot" id="statusDot"></span>
      <span id="statusText" data-i18n="statusChecking">Checking live status…</span>
    </div>
    <table class="stats-table">
      <thead>
        <tr>
          <th data-i18n="tableMetric">Metric</th>
          <th data-i18n="tableValue">Value</th>
        </tr>
      </thead>
      <tbody>
        <tr><td data-i18n="statNodesLabel">Road network nodes covered</td><td class="stat-value" id="statNodes">—</td></tr>
        <tr><td data-i18n="statSheltersLabel">Shelters &amp; hospitals mapped</td><td class="stat-value" id="statShelters">—</td></tr>
        <tr><td data-i18n="statReportsLabel">Active hazard reports right now</td><td class="stat-value" id="statReports">—</td></tr>
        <tr><td data-i18n="statWeatherWetLabel">Live rainfall — currently wettest station</td><td class="stat-value"><span id="statWeatherWet">—</span> <span class="stat-station" id="statWeatherWetName"></span></td></tr>
        <tr><td data-i18n="statWeatherDryLabel">Live rainfall — currently driest station</td><td class="stat-value"><span id="statWeatherDry">—</span> <span class="stat-station" id="statWeatherDryName"></span></td></tr>
      </tbody>
    </table>
    <div class="legend-row">
      <span><span class="dot" style="background:#4c8c4a"></span><span data-i18n="hazardLow">LOW hazard</span></span>
      <span><span class="dot" style="background:#c99a2e"></span><span data-i18n="hazardModerate">MODERATE hazard</span></span>
      <span><span class="dot" style="background:#cf7a2a"></span><span data-i18n="hazardSignificant">SIGNIFICANT hazard</span></span>
      <span><span class="dot" style="background:#7a1f1f"></span><span data-i18n="hazardExtreme">EXTREME hazard</span></span>
      <span style="margin-left:auto;" data-i18n="hazardSource">Source: georeferenced state flash-flood hazard atlas</span>
    </div>
  </div>
</section>

<section id="flood-guidance">
  <div class="wrap">
    <div class="section-label" data-i18n="guidanceLabel">Flood Guidance</div>
    <h2 data-i18n="guidanceTitle">How much more rain before it's dangerous, here?</h2>
    <p class="guidance-intro" data-i18n="guidanceIntro">Pairs each location's static hazard classification with its live rainfall right now to show the remaining headroom before that location's flood risk escalates.</p>
    <div style="margin-bottom:18px;">
      <a class="btn-official" href="/ffgs" data-i18n="guidanceOpenFfgs">Open full Flash Flood Guidance System →</a>
    </div>
    <table class="guidance-table">
      <thead>
        <tr>
          <th data-i18n="guidanceColTown">Location</th>
          <th data-i18n="guidanceColHazard">Hazard Zone</th>
          <th data-i18n="guidanceColRain">Live Rain Now</th>
          <th data-i18n="guidanceColHeadroom">Headroom</th>
          <th data-i18n="guidanceColStatus">Status</th>
        </tr>
      </thead>
      <tbody id="guidanceTableBody">
        <tr><td colspan="5" data-i18n="guidanceLoading">Loading…</td></tr>
      </tbody>
    </table>
    <div class="guidance-mylocation">
      <button type="button" id="guidanceMyLocationBtn" class="btn-outline" data-i18n="guidanceCheckLocation">Check guidance at my location</button>
      <div id="guidanceMyLocationResult" class="guidance-my-result" hidden></div>
    </div>
  </div>
</section>

<section>
  <div class="wrap">
    <div class="section-label" data-i18n="caseLabel">Case Reference</div>
    <h2 data-i18n="caseTitle">Route comparison: Pachora to Chamun, Pithoragarh District</h2>
    <p style="color:var(--muted); font-size:0.84375rem; max-width:640px;" data-i18n="caseText">The direct road between these two points crosses 10 road segments classified EXTREME. The advisory system reroutes around all of them for a 32% longer, but demonstrably safer, trip.</p>
    <table class="compare-table">
      <thead>
        <tr><th data-i18n="compareMode">Route Mode</th><th data-i18n="compareDistance">Distance</th><th data-i18n="compareSegments">Extreme-Risk Segments Crossed</th></tr>
      </thead>
      <tbody>
        <tr class="row-risk"><td>⚡ <span data-i18n="modeFastest">Fastest</span></td><td class="num">90.2 km</td><td class="num">10</td></tr>
        <tr class="row-safe"><td>🛡 <span data-i18n="modeSafest">Safest</span></td><td class="num">119.3 km</td><td class="num">0</td></tr>
      </tbody>
    </table>
    <div class="compare-note" data-i18n="compareNote">+29 km travelled to eliminate every extreme-risk segment on this route.</div>
  </div>
</section>

<section id="features">
  <div class="wrap">
    <div class="section-label" data-i18n="servicesLabel">Available Services</div>
    <h2 data-i18n="servicesTitle">Advisory services offered</h2>
    <div class="feature-list">
      <div class="feature-item">
        <h3><span class="num">1</span><span data-i18n="f1h">Risk-weighted routing</span></h3>
        <p data-i18n="f1p">Fastest and Safest modes run on the same road network, weighted by official per-road hazard classification — not a flat "avoid this area" toggle.</p>
      </div>
      <div class="feature-item">
        <h3><span class="num">2</span><span data-i18n="f2h">Evacuation routing</span></h3>
        <p data-i18n="f2p">Routes from the traveler's current location to the nearest reachable shelter or community facility, by real road distance.</p>
      </div>
      <div class="feature-item">
        <h3><span class="num">3</span><span data-i18n="f3h">Nearest hospital routing</span></h3>
        <p data-i18n="f3p">The same shortlist-then-route logic, applied to the nearest reachable hospital instead of a shelter.</p>
      </div>
      <div class="feature-item">
        <h3><span class="num">4</span><span data-i18n="f4h">Live rainfall adjustment</span></h3>
        <p data-i18n="f4p">Current and forecast rainfall feed directly into Safest-mode routing weights — the advisory becomes more cautious while it is actively raining.</p>
      </div>
      <div class="feature-item">
        <h3><span class="num">5</span><span data-i18n="f5h">Citizen hazard reporting</span></h3>
        <p data-i18n="f5p">Any user may report a flooded or blocked road. Active reports block that road for every routing mode and expire automatically after 6 hours, or can be marked resolved earlier.</p>
      </div>
      <div class="feature-item">
        <h3><span class="num">6</span><span data-i18n="f6h">Live rerouting</span></h3>
        <p data-i18n="f6p">A new report immediately recalculates the reporting traveler's route, and every open session is checked every 30 seconds for reports submitted by others.</p>
      </div>
    </div>
  </div>
</section>

<section>
  <div class="wrap">
    <div class="section-label" data-i18n="sourcesLabel">Data Sources &amp; Attribution</div>
    <dl class="sources-list">
      <dt data-i18n="src1dt">Hazard data</dt><dd data-i18n="src1dd">Georeferenced state flash-flood hazard atlas</dd>
      <dt data-i18n="src2dt">Road network</dt><dd>OpenStreetMap</dd>
      <dt data-i18n="src3dt">Weather data</dt><dd>Open-Meteo forecast API</dd>
      <dt data-i18n="src4dt">Place search</dt><dd>Nominatim (OpenStreetMap)</dd>
      <dt data-i18n="src5dt">Routing engine</dt><dd data-i18n="src5dd">SciPy sparse-graph Dijkstra shortest-path algorithm</dd>
      <dt data-i18n="src6dt">Watershed data</dt><dd>HydroSHEDS / HydroBASINS (WWF)</dd>
      <dt data-i18n="src7dt">Soil data</dt><dd>SoilGrids v2.0 (ISRIC)</dd>
    </dl>
  </div>
</section>

<section class="final-cta" style="border-bottom:none;">
  <div class="wrap">
    <h2 data-i18n="finalTitle">Access the flood-aware map tool</h2>
    <p data-i18n="finalText">No registration required. Available to all road users in Uttarakhand.</p>
    <a class="btn-official" href="/app" style="padding:11px 22px; font-size:0.90625rem;" data-i18n="finalCta">Open FloodSafe Map Tool →</a>
  </div>
</section>

</main>

<footer class="site-footer">
  <div class="wrap">
    <div class="footer-links">
      <a href="/app" data-i18n="footerMap">Map Tool</a>
      <a href="/reports-view" data-i18n="footerReports">Hazard Reports</a>
      <a href="/status" data-i18n="footerStatus">System Status (API)</a>
    </div>
    <div data-i18n="footerTagline">FloodSafe — Flood-Aware Road Advisory Service for Uttarakhand.</div>
    <div class="disclaimer" data-i18n="footerDisclaimer">
      FloodSafe is an independent citizen-safety project and is not an official
      service of the Government of Uttarakhand or the Government of India.
      Hazard classifications are derived from published government flash-flood
      hazard data; road conditions should always be independently verified
      before travel, particularly during active monsoon or alert conditions.
    </div>
  </div>
</footer>

<script>

// ---- Translations ----

const translations = {
  en: {
    skipLink: "Skip to main content",
    utilityTitle: "FloodSafe Portal — Flood-Aware Road Advisory Service",
    textSizeLabel: "Text size:",
    brandTagline: "Flood-Aware Road Advisory — Uttarakhand",
    navStatus: "Live Status",
    navServices: "Services",
    navReports: "Hazard Reports",
    navOpenMap: "Open Map Tool",
    advisoryLabel: "Public Advisory:",
    advisoryText: "Road conditions can change rapidly during monsoon season. Always verify local conditions before travel.",
    heroTitle: "Fastest isn't always <em>safe</em>.",
    heroSub: "FloodSafe advises road routes across Uttarakhand using a georeferenced flash-flood hazard classification, live rainfall data, and citizen-reported hazards — instead of distance alone.",
    heroCta1: "Open the Map Tool →",
    heroCta2: "View Live Status",
    diagramCaption: "Illustrative example — hover a route below",
    legendSafe: "Safest route",
    legendRisk: "Fastest route",
    diagramHint: "Hover either route to trace it live",
    statusLabel: "Live System Status",
    statusChecking: "Checking live status…",
    statusOnline: "Routing engine online — figures below are live on this page",
    statusOffline: "Routing engine temporarily unavailable",
    statusUnreachable: "Could not reach the server",
    weatherUnavailable: "unavailable",
    tableMetric: "Metric",
    tableValue: "Value",
    statNodesLabel: "Road network nodes covered",
    statSheltersLabel: "Shelters & hospitals mapped",
    statReportsLabel: "Active hazard reports right now",
    statWeatherWetLabel: "Live rainfall — currently wettest station",
    statWeatherDryLabel: "Live rainfall — currently driest station",
    hazardLow: "LOW hazard",
    hazardModerate: "MODERATE hazard",
    hazardSignificant: "SIGNIFICANT hazard",
    hazardExtreme: "EXTREME hazard",
    hazardSource: "Source: georeferenced state flash-flood hazard atlas",
    caseLabel: "Case Reference",
    caseTitle: "Route comparison: Pachora to Chamun, Pithoragarh District",
    caseText: "The direct road between these two points crosses 10 road segments classified EXTREME. The advisory system reroutes around all of them for a 32% longer, but demonstrably safer, trip.",
    compareMode: "Route Mode",
    compareDistance: "Distance",
    compareSegments: "Extreme-Risk Segments Crossed",
    modeFastest: "Fastest",
    modeSafest: "Safest",
    compareNote: "+29 km travelled to eliminate every extreme-risk segment on this route.",
    servicesLabel: "Available Services",
    servicesTitle: "Advisory services offered",
    f1h: "Risk-weighted routing",
    f1p: 'Fastest and Safest modes run on the same road network, weighted by official per-road hazard classification — not a flat "avoid this area" toggle.',
    f2h: "Evacuation routing",
    f2p: "Routes from the traveler's current location to the nearest reachable shelter or community facility, by real road distance.",
    f3h: "Nearest hospital routing",
    f3p: "The same shortlist-then-route logic, applied to the nearest reachable hospital instead of a shelter.",
    f4h: "Live rainfall adjustment",
    f4p: "Current and forecast rainfall feed directly into Safest-mode routing weights — the advisory becomes more cautious while it is actively raining.",
    f5h: "Citizen hazard reporting",
    f5p: "Any user may report a flooded or blocked road. Active reports block that road for every routing mode and expire automatically after 6 hours, or can be marked resolved earlier.",
    f6h: "Live rerouting",
    f6p: "A new report immediately recalculates the reporting traveler's route, and every open session is checked every 30 seconds for reports submitted by others.",
    sourcesLabel: "Data Sources & Attribution",
    src1dt: "Hazard data", src1dd: "Georeferenced state flash-flood hazard atlas",
    src2dt: "Road network",
    src3dt: "Weather data",
    src4dt: "Place search",
    src5dt: "Routing engine", src5dd: "SciPy sparse-graph Dijkstra shortest-path algorithm",
    src6dt: "Watershed data",
    src7dt: "Soil data",
    finalTitle: "Access the flood-aware map tool",
    finalText: "No registration required. Available to all road users in Uttarakhand.",
    finalCta: "Open FloodSafe Map Tool →",
    footerMap: "Map Tool",
    footerReports: "Hazard Reports",
    footerStatus: "System Status (API)",
    footerTagline: "FloodSafe — Flood-Aware Road Advisory Service for Uttarakhand.",
    footerDisclaimer: "FloodSafe is an independent citizen-safety project and is not an official service of the Government of Uttarakhand or the Government of India. Hazard classifications are derived from published government flash-flood hazard data; road conditions should always be independently verified before travel, particularly during active monsoon or alert conditions.",
    navGuidance: "Flood Guidance",
    navFfgs: "FFGS",
    noGeolocationSupport: "Your browser doesn't support geolocation.",
    guidanceLabel: "Flood Guidance",
    guidanceOpenFfgs: "Open full Flash Flood Guidance System →",
    guidanceTitle: "How much more rain before it's dangerous, here?",
    guidanceIntro: "Pairs each location's static hazard classification with its live rainfall right now to show the remaining headroom before that location's flood risk escalates.",
    guidanceColTown: "Location",
    guidanceColHazard: "Hazard Zone",
    guidanceColRain: "Live Rain Now",
    guidanceColHeadroom: "Headroom",
    guidanceColStatus: "Status",
    guidanceLoading: "Loading…",
    guidanceUnavailable: "Flood guidance data is unavailable right now.",
    guidanceUnmapped: "Not mapped by the hazard atlas",
    guidanceModelled: "modelled",
    guidanceLevelSAFE: "Safe",
    guidanceLevelWATCH: "Watch",
    guidanceLevelCRITICAL: "At risk now",
    guidanceCheckLocation: "Check guidance at my location",
    guidanceLocating: "Getting your location…",
    guidanceLocationDenied: "Location permission denied.",
    guidanceLocationError: "Couldn't fetch guidance for your location.",
    guidanceApproxNote: "(nearest mapped zone, ~{km} km away)",
    townDehradun: "Dehradun",
    townRishikesh: "Rishikesh",
    townHaridwar: "Haridwar",
    townMussoorie: "Mussoorie",
    townNainital: "Nainital",
    townHaldwani: "Haldwani",
    townAlmora: "Almora",
    townPithoragarh: "Pithoragarh",
    townJoshimath: "Joshimath"
  },
  hi: {
    skipLink: "मुख्य सामग्री पर जाएं",
    utilityTitle: "FloodSafe पोर्टल — बाढ़-जागरूक सड़क परामर्श सेवा",
    textSizeLabel: "टेक्स्ट आकार:",
    brandTagline: "बाढ़-जागरूक सड़क परामर्श — उत्तराखंड",
    navStatus: "लाइव स्थिति",
    navServices: "सेवाएं",
    navReports: "खतरा रिपोर्ट",
    navOpenMap: "मानचित्र खोलें",
    advisoryLabel: "सार्वजनिक सूचना:",
    advisoryText: "मानसून के दौरान सड़क की स्थिति तेज़ी से बदल सकती है। यात्रा से पहले हमेशा स्थानीय परिस्थितियों की पुष्टि करें।",
    heroTitle: "सबसे तेज़ रास्ता हमेशा <em>सुरक्षित</em> नहीं होता।",
    heroSub: "FloodSafe केवल दूरी के बजाय, भू-संदर्भित बाढ़ खतरा वर्गीकरण, लाइव वर्षा डेटा, और नागरिकों द्वारा रिपोर्ट किए गए खतरों का उपयोग करके उत्तराखंड में सड़क मार्गों की सलाह देता है।",
    heroCta1: "मानचित्र खोलें →",
    heroCta2: "लाइव स्थिति देखें",
    diagramCaption: "उदाहरण — नीचे किसी मार्ग पर कर्सर ले जाएं",
    legendSafe: "सबसे सुरक्षित मार्ग",
    legendRisk: "सबसे तेज़ मार्ग",
    diagramHint: "मार्ग देखने के लिए उस पर कर्सर ले जाएं",
    statusLabel: "लाइव सिस्टम स्थिति",
    statusChecking: "लाइव स्थिति जांची जा रही है…",
    statusOnline: "रूटिंग इंजन ऑनलाइन है — नीचे दिए गए आंकड़े इस पेज पर लाइव हैं",
    statusOffline: "रूटिंग इंजन अस्थायी रूप से अनुपलब्ध है",
    statusUnreachable: "सर्वर से संपर्क नहीं हो सका",
    weatherUnavailable: "अनुपलब्ध",
    tableMetric: "मापदंड",
    tableValue: "मान",
    statNodesLabel: "सड़क नेटवर्क नोड्स शामिल",
    statSheltersLabel: "आश्रय स्थल और अस्पताल मैप किए गए",
    statReportsLabel: "अभी सक्रिय खतरा रिपोर्टें",
    statWeatherWetLabel: "लाइव वर्षा — अभी सबसे अधिक वर्षा वाला स्टेशन",
    statWeatherDryLabel: "लाइव वर्षा — अभी सबसे कम वर्षा वाला स्टेशन",
    hazardLow: "कम खतरा",
    hazardModerate: "मध्यम खतरा",
    hazardSignificant: "उच्च खतरा",
    hazardExtreme: "अत्यधिक खतरा",
    hazardSource: "स्रोत: भू-संदर्भित राज्य बाढ़ खतरा एटलस",
    caseLabel: "केस संदर्भ",
    caseTitle: "मार्ग तुलना: पचोरा से चमुन, पिथौरागढ़ जिला",
    caseText: "इन दोनों बिंदुओं के बीच सीधी सड़क 10 सड़क खंडों से होकर गुजरती है जिन्हें अत्यधिक खतरे के रूप में वर्गीकृत किया गया है। परामर्श प्रणाली इन सभी से बचते हुए मार्ग बदलती है — दूरी 32% अधिक है, लेकिन यात्रा स्पष्ट रूप से अधिक सुरक्षित है।",
    compareMode: "मार्ग प्रकार",
    compareDistance: "दूरी",
    compareSegments: "पार किए गए अत्यधिक-जोखिम खंड",
    modeFastest: "सबसे तेज़",
    modeSafest: "सबसे सुरक्षित",
    compareNote: "इस मार्ग पर हर अत्यधिक-जोखिम खंड से बचने के लिए 29 किमी अतिरिक्त यात्रा की गई।",
    servicesLabel: "उपलब्ध सेवाएं",
    servicesTitle: "प्रदान की जाने वाली परामर्श सेवाएं",
    f1h: "जोखिम-भारित रूटिंग",
    f1p: 'सबसे तेज़ और सबसे सुरक्षित दोनों मोड एक ही सड़क नेटवर्क पर काम करते हैं, जिन्हें आधिकारिक प्रति-सड़क खतरा वर्गीकरण के आधार पर भारित किया जाता है — न कि केवल "इस क्षेत्र से बचें" जैसा एक सरल विकल्प।',
    f2h: "निकासी रूटिंग",
    f2p: "यात्री के वर्तमान स्थान से निकटतम पहुंच योग्य आश्रय स्थल या सामुदायिक सुविधा तक, वास्तविक सड़क दूरी के आधार पर मार्ग बताता है।",
    f3h: "निकटतम अस्पताल रूटिंग",
    f3p: "आश्रय स्थल के बजाय निकटतम पहुंच योग्य अस्पताल पर वही शॉर्टलिस्ट-फिर-रूट तर्क लागू किया जाता है।",
    f4h: "लाइव वर्षा समायोजन",
    f4p: "वर्तमान और पूर्वानुमानित वर्षा सीधे सबसे-सुरक्षित मोड की रूटिंग गणना में शामिल होती है — सक्रिय बारिश के दौरान परामर्श अधिक सतर्क हो जाता है।",
    f5h: "नागरिक खतरा रिपोर्टिंग",
    f5p: "कोई भी उपयोगकर्ता जलमग्न या अवरुद्ध सड़क की रिपोर्ट कर सकता है। सक्रिय रिपोर्टें उस सड़क को हर रूटिंग मोड के लिए अवरुद्ध कर देती हैं और 6 घंटे बाद स्वतः समाप्त हो जाती हैं, या पहले भी हल के रूप में चिह्नित की जा सकती हैं।",
    f6h: "लाइव रीरूटिंग",
    f6p: "नई रिपोर्ट तुरंत रिपोर्ट करने वाले यात्री के मार्ग की पुनर्गणना करती है, और हर खुला सत्र हर 30 सेकंड में दूसरों द्वारा सबमिट की गई रिपोर्टों के लिए जांचा जाता है।",
    sourcesLabel: "डेटा स्रोत और श्रेय",
    src1dt: "खतरा डेटा", src1dd: "भू-संदर्भित राज्य बाढ़ खतरा एटलस",
    src2dt: "सड़क नेटवर्क",
    src3dt: "मौसम डेटा",
    src4dt: "स्थान खोज",
    src5dt: "रूटिंग इंजन", src5dd: "SciPy स्पार्स-ग्राफ Dijkstra शॉर्टेस्ट-पाथ एल्गोरिथम",
    src6dt: "जलग्रहण डेटा",
    src7dt: "मिट्टी डेटा",
    finalTitle: "बाढ़-जागरूक मानचित्र टूल खोलें",
    finalText: "कोई पंजीकरण आवश्यक नहीं। उत्तराखंड के सभी सड़क उपयोगकर्ताओं के लिए उपलब्ध।",
    finalCta: "FloodSafe मानचित्र टूल खोलें →",
    footerMap: "मानचित्र टूल",
    footerReports: "खतरा रिपोर्ट",
    footerStatus: "सिस्टम स्थिति (API)",
    footerTagline: "FloodSafe — उत्तराखंड के लिए बाढ़-जागरूक सड़क परामर्श सेवा।",
    footerDisclaimer: "FloodSafe एक स्वतंत्र नागरिक-सुरक्षा परियोजना है और यह उत्तराखंड सरकार या भारत सरकार की कोई आधिकारिक सेवा नहीं है। खतरा वर्गीकरण प्रकाशित सरकारी बाढ़ खतरा डेटा से लिया गया है; यात्रा से पहले सड़क की स्थिति की हमेशा स्वतंत्र रूप से पुष्टि करें, विशेष रूप से सक्रिय मानसून या चेतावनी की स्थिति के दौरान।",
    navGuidance: "बाढ़ मार्गदर्शन",
    navFfgs: "FFGS",
    noGeolocationSupport: "आपका ब्राउज़र जियोलोकेशन का समर्थन नहीं करता।",
    guidanceLabel: "बाढ़ मार्गदर्शन",
    guidanceOpenFfgs: "पूर्ण फ्लैश फ्लड गाइडेंस सिस्टम खोलें →",
    guidanceTitle: "यहाँ खतरनाक होने से पहले और कितनी बारिश बाकी है?",
    guidanceIntro: "प्रत्येक स्थान के स्थिर खतरा वर्गीकरण को उसकी वर्तमान लाइव वर्षा के साथ जोड़कर, यह दिखाता है कि उस स्थान का बाढ़ जोखिम बढ़ने से पहले कितनी गुंजाइश बची है।",
    guidanceColTown: "स्थान",
    guidanceColHazard: "खतरा क्षेत्र",
    guidanceColRain: "अभी लाइव वर्षा",
    guidanceColHeadroom: "गुंजाइश",
    guidanceColStatus: "स्थिति",
    guidanceLoading: "लोड हो रहा है…",
    guidanceUnavailable: "बाढ़ मार्गदर्शन डेटा अभी उपलब्ध नहीं है।",
    guidanceUnmapped: "खतरा एटलस में मैप नहीं किया गया",
    guidanceModelled: "अनुमानित",
    guidanceLevelSAFE: "सुरक्षित",
    guidanceLevelWATCH: "सतर्क रहें",
    guidanceLevelCRITICAL: "अभी जोखिम में",
    guidanceCheckLocation: "मेरे स्थान पर मार्गदर्शन जांचें",
    guidanceLocating: "आपका स्थान प्राप्त किया जा रहा है…",
    guidanceLocationDenied: "स्थान की अनुमति अस्वीकृत।",
    guidanceLocationError: "आपके स्थान के लिए मार्गदर्शन प्राप्त नहीं हो सका।",
    guidanceApproxNote: "(निकटतम मैप किया गया क्षेत्र, ~{km} किमी दूर)",
    townDehradun: "देहरादून",
    townRishikesh: "ऋषिकेश",
    townHaridwar: "हरिद्वार",
    townMussoorie: "मसूरी",
    townNainital: "नैनीताल",
    townHaldwani: "हल्द्वानी",
    townAlmora: "अल्मोड़ा",
    townPithoragarh: "पिथौरागढ़",
    townJoshimath: "जोशीमठ"
  }
};

let currentLang = 'en';

function t(key) {
    const dict = translations[currentLang] || translations.en;
    return dict[key] !== undefined ? dict[key] : (translations.en[key] || key);
}

function applyLanguage(lang) {

    currentLang = translations[lang] ? lang : 'en';
    document.documentElement.lang = currentLang;

    const dict = translations[currentLang];

    document.querySelectorAll('[data-i18n]').forEach(function(el) {
        const key = el.getAttribute('data-i18n');
        if (dict[key] !== undefined) el.textContent = dict[key];
    });

    document.querySelectorAll('[data-i18n-html]').forEach(function(el) {
        const key = el.getAttribute('data-i18n-html');
        if (dict[key] !== undefined) el.innerHTML = dict[key];
    });

    document.querySelectorAll('[data-i18n-placeholder]').forEach(function(el) {
        const key = el.getAttribute('data-i18n-placeholder');
        if (dict[key] !== undefined) el.placeholder = dict[key];
    });

    document.querySelectorAll('.lang-toggle button').forEach(function(btn) {
        btn.classList.toggle('active', btn.getAttribute('data-lang') === currentLang);
    });

    try {
        localStorage.setItem('floodsafeLang', currentLang);
    } catch (error) {
        // Private browsing / storage disabled -- just skip remembering it.
    }

    // Text set asynchronously after a live fetch (status line, weather
    // fallback) isn't covered by the data-i18n scan above since it's
    // set imperatively in JS, not present in the page's static markup.
    // Re-render it here too so switching languages after those fetches
    // already resolved doesn't leave it stuck in the old language.
    renderDynamicText();
}

function townName(name) {
    if (!name) return '';
    const key = 'town' + name;
    const val = t(key);
    return val !== key ? val : name;
}

// ---- Dynamic (fetched) text that also needs to track the current language ----

let statusStateKey = null;
let weatherStateKey = null;
let latestWeatherStats = null;
let myLocationData = null;

// Flood Guidance panel state lives here too (rather than down next to
// its render functions) so it's initialized before applyLanguage()'s
// first synchronous call to renderDynamicText() below — declaring it
// later would leave it in the temporal dead zone at that first call
// and throw a ReferenceError that aborts the rest of this script.
let guidanceRows = [];
let guidanceLoading = true;

function renderDynamicText() {
    if (statusStateKey) {
        document.getElementById('statusText').textContent = t(statusStateKey);
    }
    if (weatherStateKey) {
        document.getElementById('statWeatherWet').textContent = t(weatherStateKey);
        document.getElementById('statWeatherDry').textContent = t(weatherStateKey);
        document.getElementById('statWeatherWetName').textContent = '';
        document.getElementById('statWeatherDryName').textContent = '';
    } else if (latestWeatherStats) {
        document.getElementById('statWeatherWet').textContent = latestWeatherStats.wettest.mm.toFixed(1) + ' mm';
        document.getElementById('statWeatherWetName').textContent = '(' + townName(latestWeatherStats.wettest.name) + ')';
        document.getElementById('statWeatherDry').textContent = latestWeatherStats.driest.mm.toFixed(1) + ' mm';
        document.getElementById('statWeatherDryName').textContent = '(' + townName(latestWeatherStats.driest.name) + ')';
    }
    if (typeof renderGuidanceTable === 'function') {
        renderGuidanceTable();
    }
    if (typeof renderMyLocationGuidance === 'function') {
        renderMyLocationGuidance();
    }
}

document.querySelectorAll('.lang-toggle button').forEach(function(btn) {
    btn.addEventListener('click', function() {
        applyLanguage(btn.getAttribute('data-lang'));
    });
});

(function initLanguage() {
    let saved = 'en';
    try {
        saved = localStorage.getItem('floodsafeLang') || 'en';
    } catch (error) {
        saved = 'en';
    }
    applyLanguage(saved);
})();

// ---- Text size control ----

let fontStep = 0;

function applyFontStep() {
    // Scales the root element, not body — every font-size in this
    // stylesheet is in rem (relative to the root), so this single line
    // resizes the entire page proportionally, not just body's own text.
    document.documentElement.style.fontSize = (100 + fontStep * 12.5) + '%';
}

document.getElementById('textSmaller').addEventListener('click', function() {
    fontStep = Math.max(fontStep - 1, -2);
    applyFontStep();
});
document.getElementById('textLarger').addEventListener('click', function() {
    fontStep = Math.min(fontStep + 1, 3);
    applyFontStep();
});
document.getElementById('textReset').addEventListener('click', function() {
    fontStep = 0;
    applyFontStep();
});

// ---- Live status strip ----

// A spread of towns across different districts of Uttarakhand, used
// to show the current wettest and driest reporting points side by
// side (see loadLiveStrip below) rather than one fixed reference
// point, so the live-data claim is visibly demonstrated rather than
// just asserted.
const RAINFALL_STATIONS = [
    { name: 'Dehradun', lat: 30.3165, lon: 78.0322 },
    { name: 'Rishikesh', lat: 30.0869, lon: 78.2676 },
    { name: 'Haridwar', lat: 29.9457, lon: 78.1642 },
    { name: 'Mussoorie', lat: 30.4598, lon: 78.0664 },
    { name: 'Nainital', lat: 29.3803, lon: 79.4636 },
    { name: 'Haldwani', lat: 29.2183, lon: 79.5130 },
    { name: 'Almora', lat: 29.5892, lon: 79.6467 },
    { name: 'Pithoragarh', lat: 29.5822, lon: 80.2181 },
    { name: 'Joshimath', lat: 30.5551, lon: 79.5643 }
];

// Current rainfall for RAINFALL_STATIONS as {name: mm}. The server's
// cached /town-rainfall comes first; only when it has nothing at all
// (Open-Meteo refusing Render's shared IP) does this browser fetch the
// towns itself -- see /rainfall-fallback.js. The live strip and the
// guidance panel both call this, and the fallback's own cache makes
// that one upstream call between them, not two.
async function loadTownRainfall() {
    let towns = {};
    try {
        const payload = await (await fetch('/town-rainfall')).json();
        towns = (payload && payload.towns) || {};
    } catch (error) {
        towns = {};
    }

    if (Object.keys(towns).length === 0 && window.FloodSafeRainfall) {
        towns = await window.FloodSafeRainfall.currentByName(RAINFALL_STATIONS);
    }
    return towns;
}

function animateCount(el, target, suffix, duration) {
    suffix = suffix || '';
    duration = duration || 700;
    const start = performance.now();
    function tick(now) {
        const progress = Math.min((now - start) / duration, 1);
        const value = Math.round(target * progress);
        el.textContent = value.toLocaleString('en-US') + suffix;
        if (progress < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
}

async function loadLiveStrip() {

    try {
        const status = await (await fetch('/status')).json();
        const dot = document.getElementById('statusDot');
        if (status.routing === 'online') {
            statusStateKey = 'statusOnline';
        } else {
            dot.classList.add('off');
            statusStateKey = 'statusOffline';
        }
        if (typeof status.node_count === 'number') {
            animateCount(document.getElementById('statNodes'), status.node_count);
        } else {
            document.getElementById('statNodes').textContent = '—';
        }
    } catch (error) {
        document.getElementById('statusDot').classList.add('off');
        statusStateKey = 'statusUnreachable';
        document.getElementById('statNodes').textContent = '—';
    }
    renderDynamicText();

    try {
        const shelters = await (await fetch('/shelters')).json();
        animateCount(document.getElementById('statShelters'), Array.isArray(shelters) ? shelters.length : 0);
    } catch (error) {
        document.getElementById('statShelters').textContent = '—';
    }

    try {
        const reports = await (await fetch('/reports')).json();
        animateCount(document.getElementById('statReports'), Array.isArray(reports) ? reports.length : 0);
    } catch (error) {
        document.getElementById('statReports').textContent = '—';
    }

    try {
        // Read from this server's cached /town-rainfall, not from
        // Open-Meteo directly. These nine towns are the same for every
        // visitor, so fetching them per browser multiplied one reading
        // by the number of people watching -- and the guidance panel
        // below fetched the identical nine all over again, eighteen
        // calls per page load. Open-Meteo's free tier counts each
        // location separately against a daily per-IP quota, so a
        // roomful of people behind one venue IP could exhaust it.
        //
        // Checking several towns spread across the state (rather than
        // one fixed point) and showing the current wettest and driest
        // side by side still makes it obvious this is a live reading,
        // not a hard-coded number — if it isn't raining anywhere right
        // now, both genuinely show 0.0mm rather than looking stuck.
        const towns = await loadTownRainfall();

        // Current conditions only — not blended with the next few
        // hours' forecast. This figure exists specifically to
        // demonstrate live data, so it needs to match what a visitor
        // can independently verify is happening right now (e.g.
        // against any weather app), not read as "raining" purely
        // because rain is forecast soon.
        const valid = RAINFALL_STATIONS
            .filter(function(station) { return towns[station.name] != null; })
            .map(function(station) {
                return { name: station.name, mm: Number(towns[station.name]) };
            });

        if (valid.length > 0) {
            valid.sort(function(a, b) { return b.mm - a.mm; });
            const wettest = valid[0];
            const driest = valid[valid.length - 1];

            latestWeatherStats = { wettest: wettest, driest: driest };
            weatherStateKey = null;
        } else {
            latestWeatherStats = null;
            weatherStateKey = 'weatherUnavailable';
        }
    } catch (error) {
        latestWeatherStats = null;
        weatherStateKey = 'weatherUnavailable';
    }
    renderDynamicText();
}

loadLiveStrip();

// ---- Flood Guidance panel ----
//
// Each zone from /flood-guidance-zones carries its hazard class plus
// watch_mm/critical_mm thresholds. Rainfall is fetched directly from
// Open-Meteo by the browser (same pattern as RAINFALL_STATIONS above),
// then combined here into a status + headroom figure per row.
// (guidanceRows / guidanceLoading are declared earlier, alongside the
// other dynamic-text state — see the comment there.)

function computeGuidanceLevel(zoneOrPoint, rainMm) {
    // effective_class, not hazard_class: a zone classified by FFPI has
    // thresholds to breach just the same as a surveyed one.
    if (!zoneOrPoint || !zoneOrPoint.effective_class || rainMm == null) return null;
    if (zoneOrPoint.critical_mm == null || zoneOrPoint.watch_mm == null) return null;
    if (rainMm >= zoneOrPoint.critical_mm) return 'CRITICAL';
    if (rainMm >= zoneOrPoint.watch_mm) return 'WATCH';
    return 'SAFE';
}

function guidanceHeadroomMm(zoneOrPoint, rainMm) {
    if (!zoneOrPoint || zoneOrPoint.critical_mm == null || rainMm == null) return null;
    return Math.max(zoneOrPoint.critical_mm - rainMm, 0);
}

function hazardI18nKey(hazardClass) {
    if (!hazardClass) return null;
    return 'hazard' + hazardClass.charAt(0) + hazardClass.slice(1).toLowerCase();
}

function renderGuidanceTable() {
    const tbody = document.getElementById('guidanceTableBody');
    if (!tbody) return;

    if (guidanceLoading) {
        tbody.innerHTML = '<tr><td colspan="5">' + t('guidanceLoading') + '</td></tr>';
        return;
    }

    if (guidanceRows.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5">' + t('guidanceUnavailable') + '</td></tr>';
        return;
    }

    tbody.innerHTML = guidanceRows.map(function(row) {
        const zone = row.zone;
        const rainMm = row.rainMm;
        const hazardKey = hazardI18nKey(zone.effective_class);
        const hazardText = hazardKey ? t(hazardKey) : t('guidanceUnmapped');
        // A modelled class is always marked, never shown as surveyed.
        const hazardLabel = zone.hazard_source === 'ffpi'
            ? hazardText + " <span class='modelled-note'>· " + t('guidanceModelled') + "</span>"
            : hazardText;
        const rainText = rainMm == null ? '—' : rainMm.toFixed(1) + ' mm';
        const level = computeGuidanceLevel(zone, rainMm);
        const headroom = guidanceHeadroomMm(zone, rainMm);
        const headroomText = headroom == null ? '—' : headroom.toFixed(0) + ' mm';
        const levelClass = 'guidance-badge guidance-' + (level ? level.toLowerCase() : 'unmapped');
        const levelText = level ? t('guidanceLevel' + level) : t('guidanceUnmapped');
        return '<tr><td>' + townName(zone.name) + '</td>' +
               '<td>' + hazardLabel + '</td>' +
               '<td class="num">' + rainText + '</td>' +
               '<td class="num">' + headroomText + '</td>' +
               '<td><span class="' + levelClass + '">' + levelText + '</span></td></tr>';
    }).join('');
}

async function loadGuidancePanel() {
    let zones = [];

    try {
        const data = await (await fetch('/flood-guidance-zones')).json();
        zones = (data && Array.isArray(data.zones)) ? data.zones : [];
    } catch (error) {
        guidanceLoading = false;
        guidanceRows = [];
        renderGuidanceTable();
        return;
    }

    // Same cached server-side reading the live-stations strip uses.
    // These are the same nine towns, so fetching them again here was
    // doubling the page's Open-Meteo cost for no new information.
    const townRain = await loadTownRainfall();

    const rainResults = zones.map(function(zone) {
        const mm = townRain[zone.name];
        return mm == null ? null : Number(mm);
    });

    guidanceLoading = false;
    guidanceRows = zones.map(function(zone, i) {
        return { zone: zone, rainMm: rainResults[i] };
    });

    renderGuidanceTable();
}

loadGuidancePanel();

function renderMyLocationGuidance() {
    const resultEl = document.getElementById('guidanceMyLocationResult');
    if (!resultEl || !myLocationData) return;

    const point = myLocationData.point;
    const rainMm = myLocationData.rainMm;
    const level = computeGuidanceLevel(point, rainMm);
    const hazardKey = hazardI18nKey(point.effective_class);
    const hazardText = hazardKey ? t(hazardKey) : t('guidanceUnmapped');
    const rainText = rainMm == null ? '—' : rainMm.toFixed(1) + ' mm';
    const levelText = level ? t('guidanceLevel' + level) : t('guidanceUnmapped');

    let approxNote = '';
    if (point.hazard_source === 'atlas' && !point.exact_match && point.distance_km != null) {
        approxNote = ' ' + t('guidanceApproxNote').replace('{km}', point.distance_km);
    }

    resultEl.innerHTML = '<b>' + hazardText + '</b> · ' + rainText + ' · <span class="guidance-badge guidance-' + (level ? level.toLowerCase() : 'unmapped') + '">' + levelText + '</span>' + approxNote;
}

document.getElementById('guidanceMyLocationBtn').addEventListener('click', function() {
    const resultEl = document.getElementById('guidanceMyLocationResult');
    resultEl.hidden = false;
    resultEl.textContent = t('guidanceLocating');

    if (!navigator.geolocation) {
        resultEl.textContent = t('noGeolocationSupport');
        return;
    }

    navigator.geolocation.getCurrentPosition(async function(pos) {
        const lat = pos.coords.latitude;
        const lon = pos.coords.longitude;

        try {
            const results = await Promise.all([
                fetch('/flood-guidance?lat=' + lat + '&lon=' + lon).then(function(r) { return r.json(); }),
                fetch('https://api.open-meteo.com/v1/forecast?latitude=' + lat + '&longitude=' + lon + '&current=precipitation&timezone=auto').then(function(r) { return r.json(); })
            ]);
            const point = results[0];
            const weatherPayload = results[1];

            const rainMm = (weatherPayload && weatherPayload.current) ? Number(weatherPayload.current.precipitation || 0) : null;
            myLocationData = { point: point, rainMm: rainMm };
            renderMyLocationGuidance();
        } catch (error) {
            resultEl.textContent = t('guidanceLocationError');
        }
    }, function() {
        resultEl.textContent = t('guidanceLocationDenied');
    }, { timeout: 10000 });
});

// ---- Hero diagram tracing ----

function initHeroTracing() {

    const riskPath = document.getElementById('riskPath');
    const safePath = document.getElementById('safePath');
    const riskHit = document.getElementById('riskHit');
    const safeHit = document.getElementById('safeHit');
    const riskDot = document.getElementById('riskDot');
    const safeDot = document.getElementById('safeDot');
    const extremeMarker = document.getElementById('extremeMarker');

    if (!riskPath || !safePath || !riskHit || !safeHit) return;

    const EXTREME_POINT = { x: 197, y: 171 };
    const REVEAL_THRESHOLD = 14;

    function makeTracer(pathEl, dotEl, otherPathEl, durationMs) {

        let generation = 0;

        function start() {

            generation += 1;
            const myGeneration = generation;

            let total;
            try {
                total = pathEl.getTotalLength();
            } catch (error) {
                return;
            }

            const startTime = performance.now();
            let revealed = false;

            dotEl.style.opacity = '1';
            pathEl.classList.add('route-active');
            otherPathEl.classList.add('route-dimmed');

            function frame(now) {

                if (myGeneration !== generation) return;

                const elapsed = now - startTime;
                const t = Math.min(elapsed / durationMs, 1);
                const point = pathEl.getPointAtLength(t * total);

                dotEl.setAttribute('cx', point.x);
                dotEl.setAttribute('cy', point.y);

                if (!revealed) {
                    const dist = Math.hypot(point.x - EXTREME_POINT.x, point.y - EXTREME_POINT.y);
                    if (dist < REVEAL_THRESHOLD) {
                        revealed = true;
                        if (extremeMarker) extremeMarker.classList.add('show');
                    }
                }

                if (t < 1) {
                    requestAnimationFrame(frame);
                } else {
                    requestAnimationFrame(() => start());
                }
            }

            requestAnimationFrame(frame);
        }

        function stop() {
            generation += 1;
            dotEl.style.opacity = '0';
            pathEl.classList.remove('route-active');
            otherPathEl.classList.remove('route-dimmed');
            if (extremeMarker) extremeMarker.classList.remove('show');
        }

        return { start, stop };
    }

    const riskTracer = makeTracer(riskPath, riskDot, safePath, 1500);
    const safeTracer = makeTracer(safePath, safeDot, riskPath, 1900);

    riskHit.addEventListener('mouseenter', riskTracer.start);
    riskHit.addEventListener('mouseleave', riskTracer.stop);
    safeHit.addEventListener('mouseenter', safeTracer.start);
    safeHit.addEventListener('mouseleave', safeTracer.stop);
}

initHeroTracing();

</script>

</body>
</html>
"""


@app.route("/")
def landing():

    return LANDING_PAGE_HTML


# ============================================================
# APP (the actual map tool)
# ============================================================

@app.route("/app")
def app_map():

    if not os.path.exists(MAP_FILE):

        return (
            "Map HTML not found.<br><br>"
            "Expected:<br>"
            f"{MAP_FILE}<br><br>"
            "Run map_app.py first.",
            500
        )

    return send_file(MAP_FILE)


# ============================================================
# STATUS
# ============================================================

@app.route("/status")
def status():

    return jsonify({
        "service": "FloodSafe",
        "status": "ok",
        "routing": "online" if ROUTING_ENGINE_AVAILABLE else "unavailable",
        "routing_error": None if ROUTING_ENGINE_AVAILABLE else ROUTING_ENGINE_ERROR,
        "node_count": int(len(coordinates)) if ROUTING_ENGINE_AVAILABLE else None
    })


# ============================================================
# SEARCH
#
# Geocoding (place search + hazard-report reverse geocoding) now
# happens CLIENT-SIDE, in each visitor's own browser — see
# map_app.py. Every visitor talking to Nominatim from their own
# IP, instead of all proxying through this one server, is both
# more resilient (one shared server IP getting rate-limited no
# longer breaks search for everyone) and closer to how Nominatim's
# usage policy expects it to be used. This server no longer makes
# any outbound geocoding calls itself.
# ============================================================


# ============================================================
# HAZARD REPORTS
# ============================================================

@app.route("/reports")
def get_reports():

    return jsonify(_active_reports())


REPORTS_VIEW_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FloodSafe — Hazard Reports</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet@1.9.3/dist/leaflet.css"/>
<script src="https://cdn.jsdelivr.net/npm/leaflet@1.9.3/dist/leaflet.js"></script>
<style>

* { box-sizing: border-box; }

body {
    margin: 0;
    font-family: Arial, Helvetica, sans-serif;
    background: #f4f6f8;
    color: #333;
}

header {
    background: #c62828;
    color: white;
    padding: 18px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 10px;
}

header h1 {
    margin: 0;
    font-size: 22px;
}

header .subtitle {
    font-size: 13px;
    opacity: 0.9;
    margin-top: 2px;
    font-weight: normal;
}

header a.back-link {
    color: white;
    text-decoration: none;
    background: rgba(255,255,255,0.15);
    padding: 8px 14px;
    border-radius: 8px;
    font-size: 14px;
}

header a.back-link:hover {
    background: rgba(255,255,255,0.28);
}

#refreshBtn {
    background: white;
    color: #c62828;
    border: none;
    padding: 8px 14px;
    border-radius: 8px;
    font-size: 14px;
    font-weight: 700;
    cursor: pointer;
}

#refreshBtn:hover {
    background: #f0f0f0;
}

#map {
    height: 360px;
    width: 100%;
}

#summaryBar {
    padding: 12px 24px;
    font-size: 14px;
    color: #555;
    background: white;
    border-bottom: 1px solid #e0e0e0;
}

#reportList {
    max-width: 900px;
    margin: 20px auto;
    padding: 0 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
}

.report-card {
    background: white;
    border-radius: 10px;
    padding: 14px 16px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.1);
    cursor: pointer;
    border-left: 4px solid #c62828;
}

.report-card:hover {
    box-shadow: 0 2px 10px rgba(0,0,0,0.15);
}

.report-place {
    font-weight: 700;
    font-size: 15px;
    margin-bottom: 4px;
}

.report-ward {
    font-size: 12px;
    color: #888;
    margin-bottom: 6px;
}

.report-description {
    font-size: 14px;
    color: #444;
    margin-bottom: 6px;
}

.report-reporter {
    font-size: 12px;
    color: #555;
    margin-bottom: 6px;
}

.report-meta {
    font-size: 12px;
    color: #888;
    display: flex;
    justify-content: space-between;
}

.confirm-count {
    margin-top: 8px;
    font-size: 12px;
    color: #6a4a00;
}

.report-actions {
    display: flex;
    gap: 8px;
    margin-top: 8px;
    flex-wrap: wrap;
}

.resolve-btn {
    padding: 7px 12px;
    border: 1px solid #2e7d32;
    border-radius: 8px;
    background: #e8f5e9;
    color: #2e7d32;
    font-size: 12.5px;
    font-weight: 700;
    cursor: pointer;
}

.resolve-btn:hover {
    background: #d5ecd6;
}

.confirm-btn {
    padding: 7px 12px;
    border: 1px solid #b8860b;
    border-radius: 8px;
    background: #fff8e1;
    color: #8a6300;
    font-size: 12.5px;
    font-weight: 700;
    cursor: pointer;
}

.confirm-btn:hover {
    background: #ffedb3;
}

.confirm-btn:disabled {
    opacity: 0.65;
    cursor: default;
}

.empty-state {
    text-align: center;
    color: #888;
    padding: 60px 20px;
    font-size: 15px;
}

</style>
</head>
<body>

<header>
    <div>
        <h1>🚨 FloodSafe — Community Hazard Reports</h1>
        <div class="subtitle">
            Roads reported flooded or blocked by other users
        </div>
    </div>
    <div style="display:flex; gap:10px; align-items:center;">
        <button id="refreshBtn" onclick="loadReportsView()">↻ Refresh</button>
        <a class="back-link" href="/app">← Back to map</a>
    </div>
</header>

<div id="map"></div>

<div id="summaryBar">Loading reports...</div>

<div id="reportList"></div>

<script>

const map = L.map("map").setView([30.0668, 79.0193], 8);

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap contributors",
    maxZoom: 19
}).addTo(map);

let markers = {};

function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
}

function timeAgo(timestampSeconds) {

    const diffSeconds = Math.floor(Date.now() / 1000 - timestampSeconds);

    if (diffSeconds < 60) return "just now";

    const minutes = Math.floor(diffSeconds / 60);
    if (minutes < 60) return minutes + " minute" + (minutes === 1 ? "" : "s") + " ago";

    const hours = Math.floor(minutes / 60);
    if (hours < 24) return hours + " hour" + (hours === 1 ? "" : "s") + " ago";

    const days = Math.floor(hours / 24);
    return days + " day" + (days === 1 ? "" : "s") + " ago";
}

function focusReport(id) {

    const marker = markers[id];

    if (!marker) return;

    map.setView(marker.getLatLng(), 14);
    marker.openPopup();
}

async function resolveReport(id) {

    const confirmed = confirm(
        "Mark this hazard as resolved? It will be removed for everyone " +
        "immediately, and routes will no longer avoid it."
    );

    if (!confirmed) return;

    try {

        const response = await fetch(
            "/report/" + encodeURIComponent(id) + "/resolve",
            { method: "POST" }
        );

        if (!response.ok) {

            const data = await response.json().catch(function() {
                return null;
            });

            alert((data && data.error) || "Failed to mark this report resolved.");
            return;
        }

        loadReportsView();

    } catch (error) {

        console.error(error);
        alert("Failed to mark this report resolved. Please try again.");
    }
}

function getConfirmedReportIds() {

    try {
        const raw = localStorage.getItem("floodsafeConfirmedReports");
        return raw ? JSON.parse(raw) : [];
    } catch (error) {
        return [];
    }
}

function markReportConfirmedLocally(id) {

    try {

        const ids = getConfirmedReportIds();

        if (!ids.includes(id)) {
            ids.push(id);
            localStorage.setItem(
                "floodsafeConfirmedReports",
                JSON.stringify(ids)
            );
        }

    } catch (error) {
        // Private browsing / storage disabled -- just skip remembering it.
    }
}

async function confirmReportCard(id, e) {

    if (e) e.stopPropagation();

    try {

        const response = await fetch(
            "/report/" + encodeURIComponent(id) + "/confirm",
            { method: "POST" }
        );

        const data = await response.json().catch(function() {
            return null;
        });

        if (!response.ok) {
            alert((data && data.error) || "Failed to confirm this report.");
            return;
        }

        markReportConfirmedLocally(id);
        loadReportsView();

    } catch (error) {

        console.error(error);
        alert("Failed to confirm this report. Please try again.");
    }
}

async function loadReportsView() {

    const summaryBar = document.getElementById("summaryBar");
    const listEl = document.getElementById("reportList");

    summaryBar.textContent = "Loading reports...";

    try {

        const response = await fetch("/reports");
        const reports = await response.json();

        Object.values(markers).forEach(m => map.removeLayer(m));
        markers = {};
        listEl.innerHTML = "";

        if (!Array.isArray(reports) || reports.length === 0) {
            summaryBar.textContent = "No active hazard reports right now.";
            listEl.innerHTML =
                '<div class="empty-state">✓ No flooded or blocked roads ' +
                'have been reported. Reports made from the map appear ' +
                'here automatically.</div>';
            return;
        }

        const sorted = reports.slice().sort((a, b) => b.timestamp - a.timestamp);

        summaryBar.textContent =
            sorted.length + " active hazard report" +
            (sorted.length === 1 ? "" : "s") +
            " (auto-expire after 6 hours)";

        sorted.forEach(function(report) {

            const label = report.place_name ?
                report.place_name.split(",").slice(0, 2).join(",") :
                report.lat.toFixed(5) + ", " + report.lon.toFixed(5);

            const reporterLabel = report.reporter_name ?
                escapeHtml(report.reporter_name) : "Anonymous";

            const wardLabel = report.nearest_locality ?
                "Near " + escapeHtml(report.nearest_locality.name) + ", " + escapeHtml(report.nearest_locality.town) :
                "";

            const marker = L.marker([report.lat, report.lon]).addTo(map);

            marker.bindPopup(
                "<b>" + escapeHtml(label) + "</b><br>" +
                (wardLabel ? "<span style='color:#888; font-size:12px;'>" + wardLabel + "</span><br>" : "") +
                escapeHtml(report.description) + "<br>" +
                "<span style='color:#888; font-size:12px;'>Reported by " +
                reporterLabel + "</span>"
            );

            markers[report.id] = marker;

            const confirmations = Number(report.confirmations) || 0;

            const confirmCountText = confirmations > 0 ?
                "Confirmed by " + confirmations +
                (confirmations === 1 ? " other traveler" : " other travelers") :
                "Not yet confirmed by anyone else";

            const alreadyConfirmed = getConfirmedReportIds().includes(report.id);

            const confirmBtnHtml = alreadyConfirmed ?
                '<button class="confirm-btn" disabled>✓ You confirmed this</button>' :
                '<button class="confirm-btn">👍 Still an issue?</button>';

            const card = document.createElement("div");
            card.className = "report-card";
            card.onclick = function() { focusReport(report.id); };

            card.innerHTML =
                '<div class="report-place">📍 ' + escapeHtml(label) + '</div>' +
                (wardLabel ? '<div class="report-ward">' + wardLabel + '</div>' : '') +
                '<div class="report-description">' +
                escapeHtml(report.description) + '</div>' +
                '<div class="report-reporter">👤 ' + reporterLabel + '</div>' +
                '<div class="report-meta">' +
                '<span>' + timeAgo(report.timestamp) + '</span>' +
                '<span>' + report.lat.toFixed(5) + ', ' +
                report.lon.toFixed(5) + '</span>' +
                '</div>' +
                '<div class="confirm-count">' + confirmCountText + '</div>' +
                '<div class="report-actions">' +
                confirmBtnHtml +
                '<button class="resolve-btn">✓ Mark resolved — road is clear</button>' +
                '</div>';

            const resolveBtn = card.querySelector(".resolve-btn");

            resolveBtn.onclick = function(e) {
                e.stopPropagation();
                resolveReport(report.id);
            };

            const confirmBtn = card.querySelector(".confirm-btn");

            if (confirmBtn && !confirmBtn.disabled) {
                confirmBtn.onclick = function(e) {
                    confirmReportCard(report.id, e);
                };
            }

            listEl.appendChild(card);
        });

    } catch (error) {

        console.error(error);
        summaryBar.textContent = "Failed to load reports. Try refreshing.";
    }
}

loadReportsView();

// Auto-refresh every 30s so this page stays current if left open.
setInterval(loadReportsView, 30000);

</script>

</body>
</html>
"""


@app.route("/reports-view")
def reports_view():

    return REPORTS_VIEW_HTML


@app.route("/report", methods=["POST"])
def post_report():

    if _rate_limited("report"):
        return _rate_limit_response("report")

    data = request.get_json(force=True, silent=True)

    if not isinstance(data, dict):

        return jsonify({
            "status": "error",
            "error": "Request body must be valid JSON with lat, lon."
        }), 400

    try:

        lat = float(data.get("lat"))
        lon = float(data.get("lon"))

    except (TypeError, ValueError):

        return jsonify({
            "status": "error",
            "error": "lat and lon must be numbers."
        }), 400

    if not (-90.0 <= lat <= 90.0):

        return jsonify({
            "status": "error",
            "error": "lat must be between -90 and 90."
        }), 400

    if not (-180.0 <= lon <= 180.0):

        return jsonify({
            "status": "error",
            "error": "lon must be between -180 and 180."
        }), 400

    description = str(data.get("description", "")).strip()

    if len(description) > REPORT_DESCRIPTION_MAX_LENGTH:

        description = description[:REPORT_DESCRIPTION_MAX_LENGTH]

    if not description:

        description = "Flooded / blocked road reported"

    # The client resolves this itself (its own browser calling
    # Nominatim's reverse endpoint directly) — this server makes no
    # outbound geocoding calls at all. Best-effort only: fall back
    # to no place name rather than fail the report.
    place_name = data.get("place_name")

    if not isinstance(place_name, str) or not place_name.strip():
        place_name = None
    else:
        place_name = place_name.strip()[:300]

    reporter_name = data.get("reporter_name")

    if not isinstance(reporter_name, str) or not reporter_name.strip():
        reporter_name = None
    else:
        reporter_name = reporter_name.strip()[:REPORT_NAME_MAX_LENGTH]

    report = {
        "id": uuid.uuid4().hex,
        "lat": lat,
        "lon": lon,
        "place_name": place_name,
        "nearest_locality": nearest_locality_for_point(lat, lon),
        "description": description,
        "reporter_name": reporter_name,
        "timestamp": time.time(),
        "confirmations": 0
    }

    print()
    print("================================")
    print("HAZARD REPORT")
    print("================================")
    print("Location:", lat, lon, "(", place_name, ")")
    print("Reporter:", reporter_name or "anonymous")
    print("Description:", description)

    _reports.append(report)
    _save_reports(_reports)

    return jsonify(report), 201


# ============================================================
# RESOLVE REPORT
#
# Lets anyone mark a hazard report resolved before its normal
# 6-hour expiry (e.g. the road has actually been cleared). No
# ownership check, deliberately — the same open-trust model as
# submitting a report in the first place. Once removed, it no
# longer blocks any road for any routing mode.
# ============================================================

@app.route("/report/<report_id>/resolve", methods=["POST"])
def resolve_report(report_id):

    if _rate_limited("report_action"):
        return _rate_limit_response("report_action")

    before_count = len(_reports)

    _reports[:] = [
        report for report in _reports
        if report.get("id") != report_id
    ]

    if len(_reports) == before_count:

        return jsonify({
            "status": "error",
            "error":
                "Report not found — it may have already been resolved "
                "or expired."
        }), 404

    _save_reports(_reports)

    print()
    print("Report resolved:", report_id)

    _confirmed_ips_by_report.pop(report_id, None)

    return jsonify({"status": "ok", "id": report_id})


# ============================================================
# CONFIRM REPORT
#
# A lightweight "still an issue" signal, separate from resolving.
# Turns a lone, unverifiable report into a visible trust signal
# ("confirmed by 3 travelers") without requiring accounts. Confirming
# doesn't change routing at all — the road is already blocked by the
# report's mere existence — it only affects the displayed count.
# ============================================================

@app.route("/report/<report_id>/confirm", methods=["POST"])
def confirm_report(report_id):

    if _rate_limited("report_action"):
        return _rate_limit_response("report_action")

    report = next(
        (r for r in _reports if r.get("id") == report_id),
        None
    )

    if report is None:

        return jsonify({
            "status": "error",
            "error":
                "Report not found — it may have already been resolved "
                "or expired."
        }), 404

    client_ip = _get_client_ip()
    already_confirmed_ips = _confirmed_ips_by_report.setdefault(report_id, set())

    already_confirmed = client_ip in already_confirmed_ips

    if not already_confirmed:

        already_confirmed_ips.add(client_ip)
        report["confirmations"] = int(report.get("confirmations", 0)) + 1
        _save_reports(_reports)

    return jsonify({
        "status": "ok",
        "id": report_id,
        "confirmations": report.get("confirmations", 0),
        "already_confirmed": already_confirmed
    })


# ============================================================
# TOWN RAINFALL — one cached reading for the nine guidance towns
#
# Replaces /weather, which was removed. That endpoint proxied
# Open-Meteo for an arbitrary caller-supplied coordinate, and by the
# time it was audited nothing called it: it was unreferenced code that
# was nonetheless publicly reachable, accepted any lat/lon on Earth,
# issued one upstream request per distinct ~1km cell, and cached them
# in an unbounded dict. That is an open proxy onto a 10,000/day quota
# shared across Render's outbound IP, drainable by anyone walking
# query strings, and a slow memory leak besides.
#
# What the landing page actually needs is far narrower: current
# precipitation for the same nine towns, twice over. The live-stations
# strip and the flood-guidance panel were each fetching all nine from
# the browser -- eighteen Open-Meteo calls per page load for nine
# distinct points, from the visitor's own IP. Behind one shared
# venue IP that exhausts the daily quota in roughly 555 page views.
#
# So both now read this single server-side cached endpoint: one
# batched call for nine towns every ten minutes, 1,296 calls/day
# regardless of how many people are watching, and no Open-Meteo
# traffic from visitors' browsers at all. "Check my location" stays
# client-side, because that genuinely is a different point per caller.
#
# This is the same lesson as the FFGS refresh and the client-side
# fan-out before it: data identical for every visitor gets fetched
# once, server-side.
# ============================================================

RAIN_LOW_THRESHOLD_MM = 5.0
RAIN_HIGH_THRESHOLD_MM = 15.0

TOWN_RAINFALL_CACHE_TTL_SECONDS = 10 * 60
_town_rainfall_cache = {
    "timestamp": 0.0,
    "data": {},
    "last_error": None,
    "last_attempt": 0.0,
}

# After a failed refresh, wait this long before asking Open-Meteo again.
#
# Without it the caches had no failure path at all: the freshness check
# needs non-empty data and a recent *successful* timestamp, so once a
# fetch failed -- and always after a restart, when data is {} -- every
# request went straight upstream. Both pages poll every 60s, so each
# open tab became its own Open-Meteo client, 34 location-calls a time
# for /ffgs/zones, for as long as the outage lasted. A quota block
# therefore fed itself. Backing off for one TTL means a failing cache
# costs no more than a healthy one.
RAINFALL_FAILURE_BACKOFF_SECONDS = 10 * 60


def _rainfall_backoff_active(cache, now):
    return bool(cache["last_error"]) and (now - cache["last_attempt"]) < RAINFALL_FAILURE_BACKOFF_SECONDS


def _describe_open_meteo_failure(response, error):
    """
    Human-readable reason for a failed Open-Meteo call. For a 429 this
    is Open-Meteo's own "reason" field, which says which limit was hit
    (minutely, hourly or daily) -- each implies a different cause, and
    reporting every 429 as "daily" hid that.
    """

    if response is not None and response.status_code == 429:
        try:
            reason = (response.json() or {}).get("reason")
        except ValueError:
            reason = None
        return f"Open-Meteo rate limit (HTTP 429): {reason or 'no reason given'}"[:200]

    return str(error)[:200]


def _fetch_town_rainfall():
    """
    Current precipitation in mm for each guidance town, keyed by name,
    from one batched Open-Meteo call. Returns the last good reading if
    a refresh fails, so a quota block degrades to stale rather than
    blank — same contract as _fetch_ffgs_live_rainfall.
    """

    now = time.time()
    cache = _town_rainfall_cache

    if cache["data"] and (now - cache["timestamp"]) < TOWN_RAINFALL_CACHE_TTL_SECONDS:
        return cache["data"]

    if _rainfall_backoff_active(cache, now):
        return cache["data"]

    towns = GUIDANCE_TOWNS
    response = None
    cache["last_attempt"] = now

    try:

        response = requests.get(
            "https://api.open-meteo.com/v1/forecast",

            params={
                "latitude": ",".join(str(t["lat"]) for t in towns),
                "longitude": ",".join(str(t["lon"]) for t in towns),
                "current": "precipitation",
                "timezone": "auto",
            },

            timeout=20
        )

        response.raise_for_status()
        payload = response.json()

        per_location = payload if isinstance(payload, list) else [payload]

        fresh = {}

        for town, loc in zip(towns, per_location):
            current = (loc or {}).get("current") or {}
            fresh[town["name"]] = round(float(current.get("precipitation") or 0.0), 2)

        cache["timestamp"] = now
        cache["data"] = fresh
        cache["last_error"] = None

        print(f"Town rainfall refreshed: {len(fresh)} towns", flush=True)

        return fresh

    except Exception as e:

        cache["last_error"] = _describe_open_meteo_failure(response, e)
        print("WARNING: town rainfall refresh failed:", cache["last_error"], flush=True)

        return cache["data"]


@app.route("/town-rainfall")
def town_rainfall():

    data = _fetch_town_rainfall()
    cache = _town_rainfall_cache

    return jsonify({
        "towns": data,
        "age_seconds": (round(time.time() - cache["timestamp"], 1)
                        if cache["timestamp"] else None),
        "last_error": cache["last_error"],
    })


# ============================================================
# RAINFALL BROWSER FALLBACK
#
# Open-Meteo's free tier counts calls per IP, and Render's free tier
# shares its outbound IP between customers. Other apps on that IP can
# spend the whole daily quota before this server makes a single call --
# confirmed live, when a freshly deployed process was refused with
# "Daily API request limit exceeded" on its very first request. No
# server-side code can make a shared IP's quota ours.
#
# So when the server has no reading at all, each visitor's browser
# fetches the same points itself, under its own IP and quota. The
# server stays the first choice: the fallback only runs when the
# server returned nothing, and it caches its own result (success or
# failure) for ten minutes, so a page polling every 60s costs one
# batched call per ten minutes, not one per poll.
#
# The browser's reading is used on that page only. It is never sent
# back to the server, which would let any client inject rainfall
# figures into what everyone else sees.
# ============================================================

RAINFALL_FALLBACK_JS = r"""
(function () {
    "use strict";

    var TTL_MS = 10 * 60 * 1000;
    var cache = {};

    // One request per key per TTL, shared by every caller on the page.
    // A failure is cached as null for the same TTL -- the browser-side
    // copy of the server's failure backoff.
    function once(key, load) {
        var hit = cache[key];
        if (hit && Date.now() - hit.at < TTL_MS) return hit.promise;

        var entry = {
            at: Date.now(),
            promise: load().catch(function (error) {
                console.warn("Browser rainfall fallback failed:", error);
                return null;
            })
        };
        cache[key] = entry;
        return entry.promise;
    }

    function forecast(points, query) {
        var url = "https://api.open-meteo.com/v1/forecast" +
            "?latitude=" + points.map(function (p) { return p[0]; }).join(",") +
            "&longitude=" + points.map(function (p) { return p[1]; }).join(",") +
            query + "&timezone=auto";

        return fetch(url).then(function (response) {
            if (!response.ok) throw new Error("Open-Meteo HTTP " + response.status);
            return response.json();
        }).then(function (payload) {
            // A bare object for one location, a list for several.
            return Array.isArray(payload) ? payload : [payload];
        });
    }

    // Mirrors _parse_open_meteo_durations in server.py.
    function parseDurations(payload) {
        var hourly = (payload && payload.hourly) || {};
        var times = hourly.time || [];
        var precip = hourly.precipitation || [];
        var currentTime = payload && payload.current && payload.current.time;

        var idx = currentTime ? times.indexOf(currentTime) : -1;
        if (idx === -1) idx = times.length - 1;

        function sumLast(n) {
            if (idx < 0) return null;
            var total = 0;
            for (var i = Math.max(0, idx - n + 1); i <= idx; i++) total += Number(precip[i]) || 0;
            return Math.round(total * 100) / 100;
        }

        return { "1h": sumLast(1), "3h": sumLast(3), "24h": sumLast(24), antecedent_48h: sumLast(48) };
    }

    window.FloodSafeRainfall = {

        parseDurations: parseDurations,

        // Fills live_rainfall on a /ffgs/zones payload in place, but only
        // when the server had no reading for any zone. Resolves to true
        // if the browser supplied the rainfall.
        fillZones: function (data) {
            var rainfall = data && data.rainfall;
            if (!rainfall || rainfall.zones_with_data > 0 || !rainfall.cells || !rainfall.cells.length) {
                return Promise.resolve(false);
            }

            return once("zones:" + JSON.stringify(rainfall.cells), function () {
                return forecast(rainfall.cells,
                    "&current=precipitation&hourly=precipitation&past_days=2&forecast_days=1"
                ).then(function (locations) { return locations.map(parseDurations); });
            }).then(function (readings) {
                if (!readings) return false;
                data.zones.forEach(function (zone) {
                    if (zone.rain_cell != null && readings[zone.rain_cell]) {
                        zone.live_rainfall = readings[zone.rain_cell];
                    }
                });
                return true;
            });
        },

        // Current precipitation (mm) for named points, as {name: mm}.
        currentByName: function (stations) {
            return once("current:" + JSON.stringify(stations), function () {
                return forecast(stations.map(function (s) { return [s.lat, s.lon]; }),
                    "&current=precipitation"
                ).then(function (locations) {
                    var byName = {};
                    stations.forEach(function (station, i) {
                        var current = locations[i] && locations[i].current;
                        if (current) byName[station.name] = Math.round(Number(current.precipitation || 0) * 100) / 100;
                    });
                    return byName;
                });
            }).then(function (byName) { return byName || {}; });
        }
    };
})();
"""


@app.route("/rainfall-fallback.js")
def rainfall_fallback_js():

    return Response(RAINFALL_FALLBACK_JS, mimetype="application/javascript")


# ============================================================
# FLOOD GUIDANCE — endpoints
# ============================================================

@app.route("/flood-guidance-zones")
def flood_guidance_zones():

    return jsonify({
        "available": GUIDANCE_AVAILABLE,
        "error": None if GUIDANCE_AVAILABLE else GUIDANCE_ERROR,
        "zones": GUIDANCE_ZONES
    })


@app.route("/flood-guidance")
def flood_guidance_point():

    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
    except (TypeError, ValueError):
        return jsonify({
            "error": "lat and lon query parameters are required numbers."
        }), 400

    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return jsonify({
            "error": "lat/lon out of range."
        }), 400

    return jsonify(guidance_for_point(lat, lon))


# ============================================================
# FLASH FLOOD GUIDANCE SYSTEM (FFGS) — page
#
# Its own standalone page (same pattern as REPORTS_VIEW_HTML above)
# rather than folded into the landing page's design system, since it
# has its own map + table + alert banner. Rainfall for the map markers,
# table and "check my location" panel is fetched by the browser
# directly from Open-Meteo (never proxied through this server) for the
# same reason as everywhere else in this app — see the comment on
# ffgs_guidance_for_point() above and the /town-rainfall rate-limit note
# further down.
# ============================================================

FFGS_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FloodSafe — Flash Flood Guidance System</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet@1.9.3/dist/leaflet.css"/>
<script src="https://cdn.jsdelivr.net/npm/leaflet@1.9.3/dist/leaflet.js"></script>
<script src="/rainfall-fallback.js"></script>
<style>

:root {
  --navy: #0b3558;
  --navy-dark: #062338;
  --ink: #1a1f24;
  --muted: #4a5560;
  --faint: #6b7680;
  --border: #c9d2d9;
  --bg: #f3f5f6;
  --panel: #ffffff;
  --notice-bg: #fff8e1;
  --notice-border: #b5860f;
  --safe: #14532d;
  --safe-bg: #eaf3ec;
  --watch: #8a5a00;
  --watch-bg: #fff2d9;
  --risk: #7a1f1f;
  --risk-bg: #f7eceb;
}

* { box-sizing: border-box; }

body {
    margin: 0;
    font-family: -apple-system, Segoe UI, Arial, Helvetica, sans-serif;
    background: var(--bg);
    color: var(--ink);
}

.utility-bar {
    background: var(--navy-dark);
    color: #cfe0ee;
    font-size: 12px;
}
.utility-bar .wrap {
    display: flex;
    justify-content: flex-end;
    align-items: center;
    padding: 5px 20px;
    gap: 18px;
    flex-wrap: wrap;
}
.lang-toggle { display: flex; align-items: center; gap: 6px; }
.lang-toggle button {
    background: transparent;
    border: none;
    color: #9db4c9;
    font-size: 12px;
    cursor: pointer;
    padding: 2px 3px;
    font-family: inherit;
}
.lang-toggle button.active { color: white; font-weight: 700; text-decoration: underline; }
.lang-toggle .sep { color: #3a5674; }
.text-size-controls { display: flex; align-items: center; gap: 6px; }
.text-size-controls button {
    background: transparent;
    border: 1px solid #3a5674;
    color: #cfe0ee;
    border-radius: 3px;
    padding: 1px 7px;
    cursor: pointer;
    font-size: 11px;
}
.text-size-controls button:hover { background: #123553; }

header {
    background: var(--navy);
    color: white;
    padding: 18px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 10px;
}

header h1 { margin: 0; font-size: 21px; }

header .subtitle {
    font-size: 13px;
    opacity: 0.88;
    margin-top: 2px;
    font-weight: normal;
}

header nav { display: flex; gap: 10px; align-items: center; }

header nav a {
    color: white;
    text-decoration: none;
    background: rgba(255,255,255,0.15);
    padding: 8px 14px;
    border-radius: 8px;
    font-size: 13.5px;
}

header nav a:hover { background: rgba(255,255,255,0.28); }

.container { max-width: 1100px; margin: 0 auto; padding: 0 16px; }

.notice {
    background: var(--notice-bg);
    border-left: 4px solid var(--notice-border);
    padding: 12px 16px;
    margin: 18px 0;
    font-size: 13.5px;
    color: #5c4400;
    border-radius: 4px;
}

#ffgsAlert {
    display: none;
    margin: 0 0 18px;
    padding: 12px 16px;
    border-radius: 6px;
    font-size: 14px;
    font-weight: 600;
}

.ffgs-alert-critical { background: var(--risk-bg); color: var(--risk); border: 1px solid var(--risk); }
.ffgs-alert-watch { background: var(--watch-bg); color: var(--watch); border: 1px solid var(--watch); }

.panel {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    margin-bottom: 20px;
    overflow: hidden;
}

/* The zone picker's suggestion list is absolutely positioned and has to
   escape the panel; .panel's overflow:hidden (which keeps the map's
   corners inside the rounded border) would otherwise clip it to a
   sliver. The map sits in its own rounded wrapper below, so nothing
   here needs the clipping. */
.panel--picker { overflow: visible; }
.panel--picker #mapWrap { border-radius: 6px; overflow: hidden; }

.panel h2 {
    margin: 0;
    padding: 14px 18px;
    font-size: 15px;
    border-bottom: 1px solid var(--border);
    background: #f8fafb;
}

#map { height: 420px; width: 100%; }

/* Zone picker: the map starts hidden and is revealed once a zone is
   chosen, so the default view is a single question rather than 127
   overlapping markers. */
.zone-picker-hint { margin: 0 0 10px; color: var(--faint); font-size: 13px; }
.zone-picker { display: flex; gap: 10px; align-items: flex-start; flex-wrap: wrap; }
.combo { position: relative; flex: 1 1 320px; min-width: 240px; }
.combo input {
    width: 100%; box-sizing: border-box; padding: 10px 32px 10px 12px;
    font-size: 15px; font-family: inherit; border: 1px solid #b9c2cc;
    border-radius: 4px; background: #fff; color: inherit;
}
.combo input:focus { outline: 2px solid #0b3558; outline-offset: 1px; border-color: #0b3558; }
#zoneClearBtn {
    position: absolute; right: 4px; top: 50%; transform: translateY(-50%);
    border: 0; background: none; font-size: 20px; line-height: 1;
    cursor: pointer; color: var(--faint); padding: 2px 6px;
}
#zoneSuggestions {
    position: absolute; z-index: 1200; left: 0; right: 0; top: calc(100% + 2px);
    margin: 0; padding: 4px 0; list-style: none; max-height: 300px; overflow-y: auto;
    background: #fff; border: 1px solid #b9c2cc; border-radius: 4px;
    box-shadow: 0 6px 18px rgba(0,0,0,0.14);
}
#zoneSuggestions li {
    padding: 8px 12px; cursor: pointer; font-size: 14px;
    display: flex; align-items: center; gap: 8px;
}
#zoneSuggestions li[aria-selected="true"], #zoneSuggestions li:hover { background: #eef3f8; }
#zoneSuggestions li .sug-town { color: var(--faint); font-size: 12px; }
#zoneSuggestions li .sug-status { margin-left: auto; font-size: 11px; }
#zoneSuggestions .sug-empty { color: var(--faint); cursor: default; }
#zoneShowAllBtn {
    padding: 10px 14px; font-size: 14px; font-family: inherit; cursor: pointer;
    border: 1px solid #b9c2cc; border-radius: 4px; background: #f4f6f8; color: inherit;
}
#zoneShowAllBtn:hover { background: #e8edf2; }
#zoneDetail {
    margin: 14px 0 0; padding: 12px 14px; border: 1px solid #dfe4ea;
    border-radius: 4px; background: #fbfcfd;
}
#zoneDetail h3 { margin: 0 0 4px; font-size: 16px; }
#zoneDetail .zd-meta { color: var(--faint); font-size: 12px; margin-bottom: 8px; }
#mapWrap { margin-top: 14px; }

.mylocation-body { padding: 16px 18px; }

#ffgsMyLocationBtn {
    background: var(--navy);
    color: white;
    border: none;
    padding: 10px 16px;
    border-radius: 6px;
    font-size: 13.5px;
    font-weight: 600;
    cursor: pointer;
}

#ffgsMyLocationBtn:hover { background: var(--navy-dark); }

#ffgsMyLocationResult { margin-top: 12px; font-size: 13.5px; }

table.ffgs-table, table.popup-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13.5px;
}

table.ffgs-table th, table.ffgs-table td,
table.popup-table th, table.popup-table td {
    padding: 9px 14px;
    text-align: left;
    border-bottom: 1px solid var(--border);
}

table.ffgs-table td.num, table.popup-table td.num { text-align: right; }

table.ffgs-table thead th {
    background: #f8fafb;
    font-weight: 700;
    color: var(--muted);
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.03em;
}

.table-scroll { overflow-x: auto; }

.ffgs-badge {
    display: inline-block;
    padding: 3px 10px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 700;
}

.parent-town { color: var(--faint); font-weight: normal; font-size: 12px; }
.ffpi-modelled { color: var(--faint); font-weight: normal; font-size: 11px; font-style: italic; }

.ffgs-safe { background: var(--safe-bg); color: var(--safe); }
.ffgs-watch { background: var(--watch-bg); color: var(--watch); }
.ffgs-critical { background: var(--risk-bg); color: var(--risk); }
.ffgs-unmapped { background: #eceff1; color: var(--faint); }

.legend {
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    padding: 12px 18px;
    font-size: 12.5px;
    color: var(--muted);
    border-top: 1px solid var(--border);
}

.legend .dot {
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    margin-right: 6px;
}

footer {
    max-width: 1100px;
    margin: 0 auto;
    padding: 8px 16px 40px;
    font-size: 12px;
    color: var(--faint);
}

</style>
</head>
<body>

<div class="utility-bar">
    <div class="wrap">
        <div class="lang-toggle" role="group" aria-label="Language selector">
            <button type="button" data-lang="en" class="active">English</button>
            <span class="sep">|</span>
            <button type="button" data-lang="hi">हिंदी</button>
        </div>
        <div class="text-size-controls">
            <span data-i18n="textSizeLabel">Text size:</span>
            <button type="button" id="textSmaller" aria-label="Decrease text size">A-</button>
            <button type="button" id="textReset" aria-label="Reset text size">A</button>
            <button type="button" id="textLarger" aria-label="Increase text size">A+</button>
        </div>
    </div>
</div>

<header>
    <div>
        <h1 data-i18n="pageTitle">Flash Flood Guidance System</h1>
        <div class="subtitle" data-i18n="pageSubtitle">Duration-based rainfall guidance for Uttarakhand</div>
    </div>
    <nav>
        <a href="/app" data-i18n="navMapTool">Map tool →</a>
        <a href="/" data-i18n="navBackDashboard">← Back to dashboard</a>
    </nav>
</header>

<div class="container">

    <div class="notice" data-i18n-html="noticeHtml">
        This page pairs each mapped hazard zone's static classification with live rainfall
        over three windows (1h / 3h / 24h) to show whether it is SAFE, in WATCH, or in
        CRITICAL status right now. Thresholds are adjusted using real watershed data
        (upstream catchment area, from HydroSHEDS/HydroBASINS) and soil data (texture-based
        drainage class, from SoilGrids) on top of the hazard atlas and antecedent rainfall —
        <b>not</b> an official CWC/IMD Flash Flood Guidance value, which would require a full
        calibrated hydrological model this project doesn't have. The state hazard atlas only
        covers about 9.7% of Uttarakhand's area; zones outside it are marked <i>modelled</i>
        and their class comes from the <b>Flash Flood Potential Index</b> (FFPI) — a 1-10
        susceptibility index computed from real terrain (Copernicus GLO-30 DEM), soil
        (SoilGrids) and land cover (ESA WorldCover). FFPI is a physical susceptibility score,
        not a probability or a forecast, and a surveyed atlas class always takes precedence
        over it.
    </div>

    <div id="ffgsAlert"></div>

    <div class="panel">
        <h2 data-i18n="myLocationHeading">Check guidance at my location</h2>
        <div class="mylocation-body">
            <button id="ffgsMyLocationBtn" data-i18n="myLocationBtn">Use my current location</button>
            <div id="ffgsMyLocationResult" hidden></div>
        </div>
    </div>

    <div class="panel panel--picker">
        <h2 data-i18n="zoneMapHeading">Zone map — live status</h2>
        <p class="zone-picker-hint" data-i18n="zonePickerHint">Search for a locality or town to see its live flash-flood status.</p>
        <div class="zone-picker">
            <div class="combo">
                <input id="zoneSearch" type="text" autocomplete="off" spellcheck="false"
                       role="combobox" aria-expanded="false" aria-controls="zoneSuggestions"
                       aria-autocomplete="list" aria-haspopup="listbox"
                       data-i18n-placeholder="zoneSearchPlaceholder"
                       placeholder="Search a zone…">
                <button type="button" id="zoneClearBtn" hidden aria-label="Clear">&times;</button>
                <ul id="zoneSuggestions" role="listbox" hidden></ul>
            </div>
            <button type="button" id="zoneShowAllBtn" data-i18n="zoneShowAll">Show all zones</button>
        </div>
        <div id="zoneDetail" hidden></div>
        <div id="mapWrap" hidden>
        <div id="map"></div>
        <div class="legend">
            <span><span class="dot" style="background:#4c8c4a"></span><span data-i18n="hazardLow">LOW hazard</span></span>
            <span><span class="dot" style="background:#c99a2e"></span><span data-i18n="hazardModerate">MODERATE hazard</span></span>
            <span><span class="dot" style="background:#cf7a2a"></span><span data-i18n="hazardSignificant">SIGNIFICANT hazard</span></span>
            <span><span class="dot" style="background:#7a1f1f"></span><span data-i18n="hazardExtreme">EXTREME hazard</span></span>
            <span><span style="display:inline-block; width:14px; height:0; border-top:2px dashed #0b3558; margin-right:6px; vertical-align:middle;"></span><span data-i18n="watershedLegend">Watershed boundary (HydroBASINS)</span></span>
            <span style="margin-left:auto;" data-i18n="markerNote">Marker color = current worst status across all three windows</span>
        </div>
        </div>
    </div>

    <div class="panel">
        <h2><span data-i18n="allZonesHeading">All monitored zones</span> <span id="ffgsUpdated" style="font-weight:normal; color:var(--faint); font-size:12px;"></span></h2>
        <div class="table-scroll">
            <table class="ffgs-table">
                <thead>
                    <tr>
                        <th data-i18n="colLocation">Location</th>
                        <th data-i18n="colHazardZone">Hazard zone</th>
                        <th class="num" data-i18n="colFfpi">FFPI</th>
                        <th class="num" data-i18n="colEventRisk">Event model</th>
                        <th data-i18n="colSoil">Soil</th>
                        <th class="num" data-i18n="colCatchment">Catchment</th>
                        <th class="num" data-i18n="col1h">1h rain</th>
                        <th class="num" data-i18n="col3h">3h rain</th>
                        <th class="num" data-i18n="col24h">24h rain</th>
                        <th data-i18n="colStatus">Status</th>
                    </tr>
                </thead>
                <tbody id="ffgsTableBody">
                    <tr><td colspan="10" data-i18n="loading">Loading…</td></tr>
                </tbody>
            </table>
        </div>
    </div>

</div>

<footer data-i18n="footerText">
    Hazard classification: georeferenced state flash-flood hazard atlas. Rainfall: Open-Meteo
    forecast API, fetched directly by your browser. Refreshes automatically every 60 seconds.
</footer>

<script>

const FFGS_STATUS_ORDER = { SAFE: 0, WATCH: 1, CRITICAL: 2 };
const HAZARD_COLORS = { LOW: "#4c8c4a", MODERATE: "#c99a2e", SIGNIFICANT: "#cf7a2a", EXTREME: "#7a1f1f" };

// Declared up here (rather than next to the render functions that use
// them) so applyLanguage()'s re-render call below never hits a
// temporal-dead-zone ReferenceError from a `let` that hasn't executed
// yet — same reasoning as guidanceRows in the landing page's script.
let ffgsZones = [];
let ffgsDurations = ["1h", "3h", "24h"];
let ffgsLoadFailed = false;
let zoneMarkerLayer = null;

// ---- i18n (mirrors the landing page's translations/applyLanguage
// pattern, kept local to this page since it's a standalone template
// with no shared JS file) ----

const translations = {
  en: {
    textSizeLabel: "Text size:",
    pageTitle: "Flash Flood Guidance System",
    pageSubtitle: "Duration-based rainfall guidance for Uttarakhand",
    navMapTool: "Map tool →",
    navBackDashboard: "← Back to dashboard",
    noticeHtml: "This page pairs each zone's static classification with live rainfall over three windows (1h / 3h / 24h) to show whether it is SAFE, in WATCH, or in CRITICAL status right now. Thresholds are adjusted using real watershed data (upstream catchment area, from HydroSHEDS/HydroBASINS) and soil data (texture-based drainage class, from SoilGrids) on top of the hazard class and antecedent rainfall — <b>not</b> an official CWC/IMD Flash Flood Guidance value, which would require a full calibrated hydrological model this project doesn't have. The state hazard atlas only covers about 9.7% of Uttarakhand's area; zones outside it are marked <i>modelled</i> and their class comes from the <b>Flash Flood Potential Index</b> (FFPI) — a 1-10 susceptibility index computed from real terrain (Copernicus GLO-30 DEM), soil (SoilGrids) and land cover (ESA WorldCover). FFPI is a physical susceptibility score, not a probability or a forecast, and a surveyed atlas class always takes precedence over it.",
    myLocationHeading: "Check guidance at my location",
    myLocationBtn: "Use my current location",
    zoneMapHeading: "Zone map — live status",
    zonePickerHint: "Search for a locality or town to see its live flash-flood status.",
    zoneSearchPlaceholder: "Search a zone…",
    zoneShowAll: "Show all zones",
    zoneNoMatch: "No zone matches that name",
    zoneDetailClass: "Hazard zone",
    zoneDetailFfpi: "FFPI",
    zoneDetailEvent: "Event model",
    zoneDetailStatus: "Current status",
    hazardLow: "LOW hazard",
    hazardModerate: "MODERATE hazard",
    hazardSignificant: "SIGNIFICANT hazard",
    hazardExtreme: "EXTREME hazard",
    hclsLOW: "LOW", hclsMODERATE: "MODERATE", hclsSIGNIFICANT: "SIGNIFICANT", hclsEXTREME: "EXTREME",
    markerNote: "Marker color = current worst status across all three windows",
    watershedLegend: "Watershed boundary (HydroBASINS)",
    allZonesHeading: "All monitored zones",
    colLocation: "Location",
    colHazardZone: "Hazard zone",
    colFfpi: "FFPI",
    colEventRisk: "Event model",
    eventRiskTitle: "Probability of rainfall-triggered mass movement, from a model trained on 206 real recorded disasters (NASA Global Landslide Catalog, 1970-2019). Spatially blocked cross-validation ROC-AUC 0.82. Not a forecast.",
    ffpiModelledNote: "modelled",
    ffpiTitleAtlas: "Hazard class from the state flash-flood hazard atlas (surveyed).",
    ffpiTitleModelled: "No atlas coverage here. Class derived from the Flash Flood Potential Index — a 1-10 terrain/soil/land-cover susceptibility index, not a surveyed class.",
    colSoil: "Soil",
    colCatchment: "Catchment",
    col1h: "1h rain",
    col3h: "3h rain",
    col24h: "24h rain",
    colStatus: "Status",
    soilLabel: "Soil group:",
    soilSand: "sand",
    soilClay: "clay",
    catchmentLabel: "Catchment:",
    loading: "Loading…",
    updatedLabel: "Updated ",
    noZones: "No mapped zones with live data right now.",
    guidanceUnavailable: "Guidance data unavailable right now.",
    footerText: "Hazard classification: georeferenced state flash-flood hazard atlas. Rainfall: Open-Meteo forecast API, fetched directly by your browser. Refreshes automatically every 60 seconds.",
    statusSAFE: "SAFE", statusWATCH: "WATCH", statusCRITICAL: "CRITICAL", statusUNMAPPED: "NO RAIN DATA",
    hazardZoneSuffix: " hazard zone",
    popupWindow: "Window", popupRain: "Rain", popupCriticalAt: "Critical at", popupStatus: "Status",
    alertCriticalPrefix: "CRITICAL: ",
    alertCriticalSuffix: " — live rainfall has crossed the critical threshold for at least one window.",
    alertWatchPrefix: "WATCH: ",
    alertWatchSuffix: " — live rainfall is approaching the critical threshold.",
    locating: "Locating…",
    checkingGuidance: "Checking guidance…",
    noGeoSupport: "Geolocation is not supported by this browser.",
    noMappedZoneNear: "No mapped hazard zone within range of your location (nearest is {km} km away).",
    noMappedZone: "No mapped hazard zone at your location.",
    nearestZoneNote: " (nearest mapped zone, {km} km away)",
    locationError: "Could not check guidance for your location right now.",
    locationDenied: "Location access denied or unavailable.",
    townDehradun: "Dehradun", townRishikesh: "Rishikesh", townHaridwar: "Haridwar",
    townMussoorie: "Mussoorie", townNainital: "Nainital", townHaldwani: "Haldwani",
    townAlmora: "Almora", townPithoragarh: "Pithoragarh", townJoshimath: "Joshimath"
  },
  hi: {
    textSizeLabel: "टेक्स्ट आकार:",
    pageTitle: "फ्लैश फ्लड गाइडेंस सिस्टम",
    pageSubtitle: "उत्तराखंड के लिए अवधि-आधारित वर्षा मार्गदर्शन",
    navMapTool: "मानचित्र टूल →",
    navBackDashboard: "← डैशबोर्ड पर वापस जाएं",
    noticeHtml: "यह पृष्ठ प्रत्येक मैप किए गए खतरा क्षेत्र के स्थिर वर्गीकरण को तीन अवधियों (1 घंटा / 3 घंटा / 24 घंटा) की लाइव वर्षा के साथ जोड़ता है, ताकि यह दिखाया जा सके कि वह अभी सुरक्षित (SAFE), सतर्क (WATCH) या गंभीर (CRITICAL) स्थिति में है। सीमाएँ वास्तविक जलग्रहण डेटा (अपस्ट्रीम कैचमेंट क्षेत्र, HydroSHEDS/HydroBASINS से) और मिट्टी डेटा (बनावट-आधारित जल निकासी वर्ग, SoilGrids से) का उपयोग करके, खतरा एटलस और पूर्ववर्ती वर्षा के साथ, समायोजित की जाती हैं — <b>न कि</b> कोई आधिकारिक CWC/IMD फ्लैश फ्लड गाइडेंस मान, जिसके लिए एक पूर्ण जल-विज्ञान मॉडल चाहिए जो इस प्रोजेक्ट के पास नहीं है। खतरा एटलस केवल उत्तराखंड के विशिष्ट खतरा-प्रवण क्षेत्रों को कवर करता है, पूरे राज्य को नहीं, और मिट्टी डेटा केवल उन्हीं बिंदुओं पर उपलब्ध है जहाँ इसे वास्तव में मापा गया था।",
    myLocationHeading: "मेरे स्थान पर मार्गदर्शन जांचें",
    myLocationBtn: "मेरा वर्तमान स्थान उपयोग करें",
    zoneMapHeading: "क्षेत्र मानचित्र — लाइव स्थिति",
    zonePickerHint: "अपने क्षेत्र की लाइव स्थिति देखने के लिए मोहल्ला या शहर खोजें।",
    zoneSearchPlaceholder: "क्षेत्र खोजें…",
    zoneShowAll: "सभी क्षेत्र दिखाएँ",
    zoneNoMatch: "इस नाम से कोई क्षेत्र नहीं मिला",
    zoneDetailClass: "खतरा क्षेत्र",
    zoneDetailFfpi: "FFPI",
    zoneDetailEvent: "घटना मॉडल",
    zoneDetailStatus: "वर्तमान स्थिति",
    hazardLow: "कम खतरा",
    hazardModerate: "मध्यम खतरा",
    hazardSignificant: "उच्च खतरा",
    hazardExtreme: "अत्यधिक खतरा",
    hclsLOW: "कम", hclsMODERATE: "मध्यम", hclsSIGNIFICANT: "उच्च", hclsEXTREME: "अत्यधिक",
    markerNote: "मार्कर का रंग = तीनों अवधियों में सबसे खराब वर्तमान स्थिति",
    watershedLegend: "जलग्रहण सीमा (HydroBASINS)",
    allZonesHeading: "सभी निगरानी क्षेत्र",
    colLocation: "स्थान",
    colHazardZone: "खतरा क्षेत्र",
    colFfpi: "FFPI",
    colEventRisk: "घटना मॉडल",
    eventRiskTitle: "वर्षा-जनित भूस्खलन की संभावना — 206 वास्तविक दर्ज आपदाओं (NASA Global Landslide Catalog, 1970-2019) पर प्रशिक्षित मॉडल से। स्थानिक रूप से विभाजित क्रॉस-वैलिडेशन ROC-AUC 0.82। यह पूर्वानुमान नहीं है।",
    ffpiModelledNote: "अनुमानित",
    ffpiTitleAtlas: "खतरा श्रेणी राज्य फ्लैश फ्लड हैज़र्ड एटलस से (सर्वेक्षित)।",
    ffpiTitleModelled: "यहाँ एटलस कवरेज नहीं है। श्रेणी फ्लैश फ्लड पोटेंशियल इंडेक्स से ली गई है — भूभाग/मिट्टी/भू-आवरण पर आधारित 1-10 संवेदनशीलता सूचकांक, सर्वेक्षित श्रेणी नहीं।",
    colSoil: "मिट्टी",
    colCatchment: "जलग्रहण क्षेत्र",
    col1h: "1 घंटे की वर्षा",
    col3h: "3 घंटे की वर्षा",
    col24h: "24 घंटे की वर्षा",
    colStatus: "स्थिति",
    soilLabel: "मिट्टी समूह:",
    soilSand: "रेत",
    soilClay: "चिकनी मिट्टी",
    catchmentLabel: "जलग्रहण क्षेत्र:",
    loading: "लोड हो रहा है…",
    updatedLabel: "अद्यतन ",
    noZones: "अभी कोई मैप किया गया क्षेत्र लाइव डेटा के साथ उपलब्ध नहीं है।",
    guidanceUnavailable: "मार्गदर्शन डेटा अभी उपलब्ध नहीं है।",
    footerText: "खतरा वर्गीकरण: राज्य का जियोरेफ़रेंस्ड फ्लैश फ्लड खतरा एटलस। वर्षा: Open-Meteo पूर्वानुमान API, आपके ब्राउज़र द्वारा सीधे प्राप्त। हर 60 सेकंड में स्वतः अद्यतन होता है।",
    statusSAFE: "सुरक्षित", statusWATCH: "सतर्क", statusCRITICAL: "गंभीर", statusUNMAPPED: "वर्षा डेटा नहीं",
    hazardZoneSuffix: " खतरा क्षेत्र",
    popupWindow: "अवधि", popupRain: "वर्षा", popupCriticalAt: "गंभीर स्तर", popupStatus: "स्थिति",
    alertCriticalPrefix: "गंभीर: ",
    alertCriticalSuffix: " — लाइव वर्षा ने कम से कम एक अवधि में गंभीर सीमा पार कर ली है।",
    alertWatchPrefix: "सतर्क: ",
    alertWatchSuffix: " — लाइव वर्षा गंभीर सीमा के करीब पहुंच रही है।",
    locating: "स्थान प्राप्त किया जा रहा है…",
    checkingGuidance: "मार्गदर्शन जांचा जा रहा है…",
    noGeoSupport: "आपका ब्राउज़र जियोलोकेशन का समर्थन नहीं करता।",
    noMappedZoneNear: "आपके स्थान के आसपास कोई मैप किया गया खतरा क्षेत्र नहीं है (निकटतम {km} किमी दूर है)।",
    noMappedZone: "आपके स्थान पर कोई मैप किया गया खतरा क्षेत्र नहीं है।",
    nearestZoneNote: " (निकटतम मैप किया गया क्षेत्र, {km} किमी दूर)",
    locationError: "अभी आपके स्थान के लिए मार्गदर्शन जांचा नहीं जा सका।",
    locationDenied: "स्थान की अनुमति अस्वीकृत या अनुपलब्ध।",
    townDehradun: "देहरादून", townRishikesh: "ऋषिकेश", townHaridwar: "हरिद्वार",
    townMussoorie: "मसूरी", townNainital: "नैनीताल", townHaldwani: "हल्द्वानी",
    townAlmora: "अल्मोड़ा", townPithoragarh: "पिथौरागढ़", townJoshimath: "जोशीमठ"
  }
};

let currentLang = "en";

function t(key) {
    const dict = translations[currentLang] || translations.en;
    return dict[key] !== undefined ? dict[key] : key;
}

function ffgsTownName(name) {
    if (!name) return "";
    const key = "town" + name;
    const val = t(key);
    return val !== key ? val : name;
}

function hazardClassLabel(cls) {
    if (!cls) return "";
    const key = "hcls" + cls;
    const val = t(key);
    return val !== key ? val : cls;
}

function statusLabel(status) {
    // Every listed zone now has a class (atlas or FFPI), so a null
    // status means the rainfall reading is missing, not that the zone
    // is unmapped -- saying "UNMAPPED" here would contradict the
    // hazard class and FFPI value shown in the same row.
    if (!status) return t("statusUNMAPPED");
    const key = "status" + status;
    const val = t(key);
    return val !== key ? val : status;
}

function applyLanguage(lang) {
    currentLang = translations[lang] ? lang : "en";
    document.documentElement.lang = currentLang;

    const dict = translations[currentLang];

    document.querySelectorAll("[data-i18n]").forEach(function(el) {
        const key = el.getAttribute("data-i18n");
        if (dict[key] !== undefined) el.textContent = dict[key];
    });

    document.querySelectorAll("[data-i18n-html]").forEach(function(el) {
        const key = el.getAttribute("data-i18n-html");
        if (dict[key] !== undefined) el.innerHTML = dict[key];
    });

    document.querySelectorAll("[data-i18n-placeholder]").forEach(function(el) {
        const key = el.getAttribute("data-i18n-placeholder");
        if (dict[key] !== undefined) el.placeholder = dict[key];
    });

    // The zone picker's rendered contents are language-dependent too,
    // and are not driven by data-i18n attributes.
    if (typeof refreshZoneUi === "function") refreshZoneUi();

    document.querySelectorAll(".lang-toggle button").forEach(function(btn) {
        btn.classList.toggle("active", btn.getAttribute("data-lang") === currentLang);
    });

    try {
        localStorage.setItem("floodsafeLang", currentLang);
    } catch (error) {
        // Private browsing / storage disabled -- just skip remembering it.
    }

    // Re-render the fetched-data parts too, since they're built with
    // innerHTML/textContent in JS rather than scanned from data-i18n.
    if (ffgsZones.length || ffgsLoadFailed) {
        renderMarkers();
        renderFfgsTable();
        renderAlertBanner();
    }
}

document.querySelectorAll(".lang-toggle button").forEach(function(btn) {
    btn.addEventListener("click", function() {
        applyLanguage(btn.getAttribute("data-lang"));
    });
});

(function initLanguage() {
    let saved = "en";
    try {
        saved = localStorage.getItem("floodsafeLang") || "en";
    } catch (error) {
        saved = "en";
    }
    applyLanguage(saved);
})();

// ---- Text size control (same steps as the landing page) ----

let fontStep = 0;

function applyFontStep() {
    document.documentElement.style.fontSize = (100 + fontStep * 12.5) + "%";
}

document.getElementById("textSmaller").addEventListener("click", function() {
    fontStep = Math.max(fontStep - 1, -2);
    applyFontStep();
});
document.getElementById("textLarger").addEventListener("click", function() {
    fontStep = Math.min(fontStep + 1, 3);
    applyFontStep();
});
document.getElementById("textReset").addEventListener("click", function() {
    fontStep = 0;
    applyFontStep();
});

const map = L.map("map").setView([30.0668, 79.0193], 8);

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap contributors",
    maxZoom: 19
}).addTo(map);

// The map starts hidden behind the zone picker, and Leaflet cannot
// project onto a display:none container -- every layer added while it
// is hidden throws "Invalid LatLng object: (NaN, NaN)". So the overlays
// are fetched only once the map is first revealed, and only once.
let overlaysLoaded = false;

function loadMapOverlays() {
    if (overlaysLoaded) return;
    overlaysLoaded = true;

    fetch("/ffgs/hazard-atlas.geojson")
        .then(function(r) { return r.json(); })
        .then(function(geojson) {
            L.geoJSON(geojson, {
                style: function(feature) {
                    const cls = feature.properties && feature.properties.hazard;
                    const color = HAZARD_COLORS[cls] || "#6b7680";
                    return { color: color, weight: 1, fillColor: color, fillOpacity: 0.25 };
                }
            }).addTo(map);
        })
        .catch(function() {});

    // Watershed sub-basin boundaries (HydroSHEDS/HydroBASINS) -- outline
    // only, no fill, so it reads as terrain context under the hazard
    // shading and zone markers rather than competing with them.
    fetch("/ffgs/watersheds.geojson")
        .then(function(r) { return r.json(); })
        .then(function(geojson) {
            L.geoJSON(geojson, {
                style: { color: "#0b3558", weight: 1, fillOpacity: 0, dashArray: "3,3", opacity: 0.5 }
            }).addTo(map);
        })
        .catch(function() {});
}

function worseStatus(a, b) {
    if (!a) return b;
    if (!b) return a;
    return FFGS_STATUS_ORDER[a] >= FFGS_STATUS_ORDER[b] ? a : b;
}

function statusForDuration(rainMm, thresholds) {
    if (rainMm == null || !thresholds) return null;
    if (rainMm >= thresholds.critical) return "CRITICAL";
    if (rainMm >= thresholds.watch) return "WATCH";
    return "SAFE";
}

function statusColor(status) {
    if (status === "CRITICAL") return "#7a1f1f";
    if (status === "WATCH") return "#b5860f";
    if (status === "SAFE") return "#14532d";
    return "#6b7680";
}

function formatCatchmentKm2(km2) {
    if (km2 == null) return null;
    return (km2 >= 1000 ? (km2 / 1000).toFixed(1) + "k" : km2.toFixed(0)) + " km²";
}

function soilCellText(z) {
    return (z.soil && z.soil.hydrologic_soil_group) ? z.soil.hydrologic_soil_group : "—";
}

function catchmentCellText(z) {
    const text = z.watershed ? formatCatchmentKm2(z.watershed.up_area_km2) : null;
    return text || "—";
}

function physicalContextLine(zoneOrPoint) {
    const parts = [];

    if (zoneOrPoint.soil && zoneOrPoint.soil.hydrologic_soil_group) {
        parts.push(t("soilLabel") + " " + zoneOrPoint.soil.hydrologic_soil_group +
            " (" + zoneOrPoint.soil.sand_pct + "% " + t("soilSand") + ", " +
            zoneOrPoint.soil.clay_pct + "% " + t("soilClay") + ")");
    }

    const catchmentText = zoneOrPoint.watershed ? formatCatchmentKm2(zoneOrPoint.watershed.up_area_km2) : null;
    if (catchmentText) {
        parts.push(t("catchmentLabel") + " " + catchmentText);
    }

    return parts.join(" · ");
}

// Fetched directly from the browser (own IP), never proxied through
// this server — same reasoning as fetchLiveConditions in map_app.py:
// all visitors sharing Render's one outbound IP against Open-Meteo
// trips its rate limit.
async function fetchDurationRainfall(lat, lon) {

    const url = "https://api.open-meteo.com/v1/forecast?latitude=" + lat +
        "&longitude=" + lon +
        "&current=precipitation&hourly=precipitation&past_days=2&forecast_days=1&timezone=auto";

    const payload = await (await fetch(url)).json();

    return window.FloodSafeRainfall.parseDurations(payload);
}

function alertZoneLabel(z) {
    return z.parent_town ? z.name + " (" + ffgsTownName(z.parent_town) + ")" : ffgsTownName(z.name);
}

function renderAlertBanner() {
    const el = document.getElementById("ffgsAlert");
    const critical = ffgsZones.filter(function(z) { return z.overall === "CRITICAL"; });
    const watch = ffgsZones.filter(function(z) { return z.overall === "WATCH"; });

    if (critical.length) {
        el.style.display = "block";
        el.className = "ffgs-alert-critical";
        el.textContent = t("alertCriticalPrefix") + critical.map(alertZoneLabel).join(", ") + t("alertCriticalSuffix");
    } else if (watch.length) {
        el.style.display = "block";
        el.className = "ffgs-alert-watch";
        el.textContent = t("alertWatchPrefix") + watch.map(alertZoneLabel).join(", ") + t("alertWatchSuffix");
    } else {
        el.style.display = "none";
    }
}

// A zone's class comes either from the surveyed hazard atlas or, where
// the atlas has no coverage, from FFPI. The two are never shown the
// same way -- a modelled class always carries a visible marker so it
// can't be mistaken for a surveyed one.
function hazardCellText(z) {
    const label = hazardClassLabel(z.effective_class);
    if (z.hazard_source === "ffpi") {
        return '<span title="' + t("ffpiTitleModelled") + '">' + label +
            '<span class="ffpi-modelled"> · ' + t("ffpiModelledNote") + "</span></span>";
    }
    return '<span title="' + t("ffpiTitleAtlas") + '">' + label + "</span>";
}

function ffpiCellText(z) {
    if (z.ffpi == null) return "—";
    return z.ffpi.toFixed(1);
}

// Probability from the model trained on real recorded disasters, shown
// as a percentage. Distinct from FFPI beside it: FFPI is a physical
// index with no labels, this is a supervised estimate.
function eventRiskCellText(z) {
    if (z.event_prob == null) return "—";
    return '<span title="' + t("eventRiskTitle") + '">' +
        Math.round(z.event_prob * 100) + "%</span>";
}

function renderFfgsTable() {
    const tbody = document.getElementById("ffgsTableBody");

    if (ffgsLoadFailed) {
        tbody.innerHTML = '<tr><td colspan="10">' + t("guidanceUnavailable") + "</td></tr>";
        return;
    }

    if (ffgsZones.length === 0) {
        tbody.innerHTML = '<tr><td colspan="10">' + t("noZones") + "</td></tr>";
        return;
    }

    const sorted = ffgsZones.slice().sort(function(a, b) {
        return (FFGS_STATUS_ORDER[b.overall] || 0) - (FFGS_STATUS_ORDER[a.overall] || 0);
    });

    tbody.innerHTML = sorted.map(function(z) {
        function cell(d) {
            const info = z.perDuration[d];
            if (!info || info.rainMm == null) return "—";
            return info.rainMm.toFixed(1) + " mm";
        }
        const badgeClass = "ffgs-badge ffgs-" + (z.overall || "unmapped").toLowerCase();
        const locationCell = z.parent_town
            ? z.name + '<span class="parent-town"> — ' + ffgsTownName(z.parent_town) + "</span>"
            : ffgsTownName(z.name);
        return "<tr><td>" + locationCell + "</td><td>" + hazardCellText(z) + "</td>" +
            '<td class="num">' + ffpiCellText(z) + "</td>" +
            '<td class="num">' + eventRiskCellText(z) + "</td>" +
            "<td>" + soilCellText(z) + "</td>" +
            '<td class="num">' + catchmentCellText(z) + "</td>" +
            '<td class="num">' + cell("1h") + "</td>" +
            '<td class="num">' + cell("3h") + "</td>" +
            '<td class="num">' + cell("24h") + "</td>" +
            "<td><span class=\\"" + badgeClass + "\\">" + statusLabel(z.overall) + "</span></td></tr>";
    }).join("");

    const updatedEl = document.getElementById("ffgsUpdated");
    if (updatedEl) {
        updatedEl.textContent = "(" + t("updatedLabel") + new Date().toLocaleTimeString() + ")";
    }
}

// ============================================================
// ZONE PICKER
//
// 127 markers on one map is a haystack -- the useful question is
// "what is the status where I am?", so the map stays hidden until a
// zone is chosen and the search box answers that question directly.
// The full overview is still one button away.
// ============================================================

// The FFGS page has no escaping helper of its own -- escapeHtml lives
// in the landing-page template, which is a separate document. Zone and
// town names come from OpenStreetMap, so they are untrusted input and
// are escaped before they reach innerHTML.
function escapeAttr(text) {
    return String(text == null ? "" : text)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

let zoneIndex = [];
let markerByKey = {};
let selectedZoneKey = null;
let activeSuggestion = -1;
let mapRevealed = false;

function zoneKey(z) {
    return z.lat.toFixed(5) + "," + z.lon.toFixed(5);
}

// Locality names carry macrons (Bahadrabad is stored as Bahādrābād)
// but people type ASCII, so both sides are stripped to bare letters
// before matching.
function foldText(value) {
    return (value || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
}

function zoneLabel(z) {
    return z.parent_town
        ? z.name + " — " + ffgsTownName(z.parent_town)
        : ffgsTownName(z.name);
}

function buildZoneIndex() {
    zoneIndex = ffgsZones.map(function(z) {
        return {
            zone: z,
            label: zoneLabel(z),
            haystack: foldText(z.name + " " + (z.parent_town || ""))
        };
    }).sort(function(a, b) {
        const aTown = a.zone.kind === "town";
        const bTown = b.zone.kind === "town";
        if (aTown !== bTown) return aTown ? -1 : 1;
        return a.label.localeCompare(b.label);
    });
}

function closeSuggestions() {
    const list = document.getElementById("zoneSuggestions");
    const input = document.getElementById("zoneSearch");
    if (!list || !input) return;
    list.hidden = true;
    list.innerHTML = "";
    input.setAttribute("aria-expanded", "false");
    activeSuggestion = -1;
}

function renderSuggestions(query) {
    const list = document.getElementById("zoneSuggestions");
    const input = document.getElementById("zoneSearch");
    const q = foldText(query);

    const matches = zoneIndex.filter(function(entry) {
        return !q || entry.haystack.indexOf(q) >= 0;
    });

    if (matches.length === 0) {
        list.innerHTML = '<li class="sug-empty" role="presentation">' + t("zoneNoMatch") + "</li>";
        list.hidden = false;
        input.setAttribute("aria-expanded", "true");
        activeSuggestion = -1;
        list._matches = [];
        return;
    }

    list.innerHTML = matches.map(function(entry, i) {
        const z = entry.zone;
        const color = statusColor(z.overall);
        const town = z.parent_town
            ? '<span class="sug-town">' + escapeAttr(ffgsTownName(z.parent_town)) + "</span>"
            : "";
        return '<li role="option" id="zone-opt-' + i + '" data-index="' + i +
            '" aria-selected="false">' +
            '<span class="dot" style="background:' + color + '"></span>' +
            "<span>" + escapeAttr(z.name) + "</span>" + town +
            '<span class="sug-status">' + escapeAttr(statusLabel(z.overall)) + "</span></li>";
    }).join("");

    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    activeSuggestion = -1;
    list._matches = matches;

    Array.prototype.forEach.call(list.querySelectorAll("li[data-index]"), function(li) {
        li.addEventListener("mousedown", function(ev) {
            // mousedown, not click: blur would close the list first.
            ev.preventDefault();
            selectZone(matches[parseInt(li.getAttribute("data-index"), 10)]);
        });
    });
}

function moveActive(delta) {
    const list = document.getElementById("zoneSuggestions");
    const options = list.querySelectorAll("li[data-index]");
    if (!options.length) return;

    if (activeSuggestion >= 0 && options[activeSuggestion]) {
        options[activeSuggestion].setAttribute("aria-selected", "false");
    }
    activeSuggestion = (activeSuggestion + delta + options.length) % options.length;
    const el = options[activeSuggestion];
    el.setAttribute("aria-selected", "true");
    el.scrollIntoView({ block: "nearest" });
    document.getElementById("zoneSearch").setAttribute("aria-activedescendant", el.id);
}

function revealMap() {
    const wrap = document.getElementById("mapWrap");
    if (!wrap || !wrap.hidden) return;
    wrap.hidden = false;
    mapRevealed = true;
    // Leaflet measured this container while it was display:none, so it
    // still believes it has zero size. Anything projected before it is
    // re-measured comes out NaN and throws. Re-measure synchronously,
    // then load the overlays and markers that were held back.
    map.invalidateSize();
    loadMapOverlays();
    renderMarkers();
}

function renderZoneDetail(z) {
    const el = document.getElementById("zoneDetail");
    const rows = ffgsDurations.map(function(d) {
        const info = z.perDuration[d];
        const rainText = (info && info.rainMm != null) ? info.rainMm.toFixed(1) + " mm" : "—";
        const critical = z.thresholds_mm && z.thresholds_mm[d]
            ? z.thresholds_mm[d].critical.toFixed(0) + " mm" : "—";
        const badge = "ffgs-badge ffgs-" + ((info && info.status) || "unmapped").toLowerCase();
        return "<tr><td>" + d + "</td><td>" + rainText + "</td><td>" + critical +
            '</td><td><span class="' + badge + '">' + statusLabel(info && info.status) +
            "</span></td></tr>";
    }).join("");

    el.innerHTML =
        "<h3>" + escapeAttr(zoneLabel(z)) + "</h3>" +
        '<div class="zd-meta">' + t("zoneDetailClass") + ": " + hazardCellText(z) +
        " · " + t("zoneDetailFfpi") + " " + (z.ffpi != null ? z.ffpi.toFixed(1) : "—") +
        " · " + t("zoneDetailEvent") + " " +
        (z.event_prob != null ? Math.round(z.event_prob * 100) + "%" : "—") + "</div>" +
        '<table class="popup-table"><thead><tr><th>' + t("popupWindow") + "</th><th>" +
        t("popupRain") + "</th><th>" + t("popupCriticalAt") + "</th><th>" +
        t("popupStatus") + "</th></tr></thead><tbody>" + rows + "</tbody></table>";
    el.hidden = false;
}

function selectZone(entry) {
    if (!entry) return;
    const z = entry.zone;
    selectedZoneKey = zoneKey(z);

    const input = document.getElementById("zoneSearch");
    input.value = entry.label;
    document.getElementById("zoneClearBtn").hidden = false;
    closeSuggestions();

    revealMap();
    renderZoneDetail(z);

    map.flyTo([z.lat, z.lon], 12, { duration: 0.7 });
    const marker = markerByKey[selectedZoneKey];
    if (marker) {
        // Wait out the flight, otherwise the popup opens mid-pan.
        setTimeout(function() { marker.openPopup(); }, 750);
    }
}

function clearZoneSelection() {
    selectedZoneKey = null;
    const input = document.getElementById("zoneSearch");
    input.value = "";
    document.getElementById("zoneClearBtn").hidden = true;
    document.getElementById("zoneDetail").hidden = true;
    closeSuggestions();
    input.focus();
}

// Re-render whatever the picker is showing, after the language changes
// or a rainfall refresh lands.
function refreshZoneUi() {
    // ffgsZones is a module-scope `let`, so it is not a window
    // property -- guarding on window.ffgsZones silently disabled the
    // whole picker.
    if (!ffgsZones.length) return;
    buildZoneIndex();

    if (selectedZoneKey) {
        const entry = zoneIndex.filter(function(e) {
            return zoneKey(e.zone) === selectedZoneKey;
        })[0];
        if (entry) {
            const input = document.getElementById("zoneSearch");
            if (input) input.value = entry.label;
            renderZoneDetail(entry.zone);
        }
    }
}

function initZonePicker() {
    const input = document.getElementById("zoneSearch");
    if (!input) return;

    input.addEventListener("focus", function() { renderSuggestions(input.value); });
    input.addEventListener("click", function() { renderSuggestions(input.value); });
    input.addEventListener("input", function() {
        document.getElementById("zoneClearBtn").hidden = !input.value;
        renderSuggestions(input.value);
    });

    input.addEventListener("keydown", function(ev) {
        const list = document.getElementById("zoneSuggestions");
        if (ev.key === "ArrowDown") {
            ev.preventDefault();
            if (list.hidden) renderSuggestions(input.value); else moveActive(1);
        } else if (ev.key === "ArrowUp") {
            ev.preventDefault();
            moveActive(-1);
        } else if (ev.key === "Enter") {
            const matches = list._matches || [];
            if (!list.hidden && matches.length) {
                ev.preventDefault();
                selectZone(matches[activeSuggestion >= 0 ? activeSuggestion : 0]);
            }
        } else if (ev.key === "Escape") {
            closeSuggestions();
        }
    });

    // Delay so a mousedown on a suggestion still registers.
    input.addEventListener("blur", function() { setTimeout(closeSuggestions, 150); });

    document.getElementById("zoneClearBtn").addEventListener("click", clearZoneSelection);

    document.getElementById("zoneShowAllBtn").addEventListener("click", function() {
        revealMap();
        selectedZoneKey = null;
        document.getElementById("zoneSearch").value = "";
        document.getElementById("zoneClearBtn").hidden = true;
        document.getElementById("zoneDetail").hidden = true;
        map.flyTo([30.0668, 79.0193], 8, { duration: 0.7 });
    });
}

function renderMarkers() {
    // Nothing may be projected onto the map until it has been shown and
    // re-measured -- see revealMap.
    if (!mapRevealed) return;

    if (!zoneMarkerLayer) {
        zoneMarkerLayer = L.layerGroup().addTo(map);
    }
    zoneMarkerLayer.clearLayers();
    markerByKey = {};

    ffgsZones.forEach(function(z) {
        const color = statusColor(z.overall);

        const marker = L.circleMarker([z.lat, z.lon], {
            radius: 9, color: color, fillColor: color, fillOpacity: 0.85, weight: 2
        }).addTo(zoneMarkerLayer);

        markerByKey[zoneKey(z)] = marker;

        const rows = ffgsDurations.map(function(d) {
            const info = z.perDuration[d];
            const rainText = (info && info.rainMm != null) ? info.rainMm.toFixed(1) + " mm" : "—";
            const critical = z.thresholds_mm && z.thresholds_mm[d] ? z.thresholds_mm[d].critical.toFixed(0) + " mm" : "—";
            const statusText = statusLabel(info && info.status);
            return "<tr><td>" + d + "</td><td>" + rainText + "</td><td>" + critical + "</td><td>" + statusText + "</td></tr>";
        }).join("");

        const popupTitle = z.parent_town
            ? "<b>" + z.name + "</b> (" + ffgsTownName(z.parent_town) + ")"
            : "<b>" + ffgsTownName(z.name) + "</b>";

        const contextLine = physicalContextLine(z);

        marker.bindPopup(
            popupTitle + " — " + hazardCellText(z) + t("hazardZoneSuffix") +
            (z.ffpi != null ? " · FFPI " + z.ffpi.toFixed(1) : "") + "<br>" +
            (contextLine ? "<span style='color:#6b7680; font-size:12px;'>" + contextLine + "</span><br>" : "") +
            '<table class="popup-table"><thead><tr><th>' + t("popupWindow") + "</th><th>" + t("popupRain") + "</th><th>" + t("popupCriticalAt") + "</th><th>" + t("popupStatus") + "</th></tr></thead><tbody>" +
            rows + "</tbody></table>"
        );
    });
}

async function loadFfgsZones() {
    const tbody = document.getElementById("ffgsTableBody");

    let data;
    try {
        data = await (await fetch("/ffgs/zones")).json();
    } catch (error) {
        ffgsLoadFailed = true;
        renderFfgsTable();
        return;
    }

    if (!data.available) {
        ffgsLoadFailed = true;
        tbody.innerHTML = '<tr><td colspan="10">' + (data.error || t("guidanceUnavailable")) + "</td></tr>";
        return;
    }

    ffgsLoadFailed = false;
    ffgsDurations = data.durations || ffgsDurations;

    // No-op unless the server had no rainfall for any zone; then this
    // browser fetches the same cells itself (see /rainfall-fallback.js).
    if (window.FloodSafeRainfall) {
        await window.FloodSafeRainfall.fillZones(data);
    }

    // effective_class, not hazard_class: FFPI now supplies a class for
    // zones the hazard atlas never covered, and those are exactly the
    // ones that used to be dropped here.
    const mappedZones = data.zones.filter(function(z) { return z.effective_class; });

    ffgsZones = mappedZones.map(function(zone) {
        const rain = zone.live_rainfall || null;
        const perDuration = {};
        let overall = null;

        ffgsDurations.forEach(function(duration) {
            const rainMm = rain ? rain[duration] : null;
            const thresholds = zone.thresholds_mm ? zone.thresholds_mm[duration] : null;
            const status = statusForDuration(rainMm, thresholds);
            perDuration[duration] = { rainMm: rainMm, status: status };
            overall = worseStatus(overall, status);
        });

        return {
            name: zone.name,
            lat: zone.lat,
            lon: zone.lon,
            hazard_class: zone.hazard_class,
            effective_class: zone.effective_class,
            hazard_source: zone.hazard_source,
            ffpi: zone.ffpi,
            ffpi_band: zone.ffpi_band,
            event_prob: zone.event_prob,
            // Needed by the picker to list the nine guidance towns
            // ahead of the localities; without it every entry sorts as
            // a locality and the towns scatter alphabetically.
            kind: zone.kind,
            thresholds_mm: zone.thresholds_mm,
            parent_town: zone.parent_town || null,
            soil: zone.soil || null,
            watershed: zone.watershed || null,
            perDuration: perDuration,
            overall: overall
        };
    });

    renderMarkers();
    renderFfgsTable();
    renderAlertBanner();
    // After renderMarkers, so markerByKey is populated before a
    // selection can try to open a popup.
    refreshZoneUi();
}

initZonePicker();
loadFfgsZones();

// Rainfall itself comes from /ffgs/zones (server-cached, see
// _fetch_ffgs_live_rainfall in server.py), so this 60s poll is
// normally just a same-origin request. When the server has no reading,
// the browser fallback fetches at most once per ten minutes, however
// often this polls.
setInterval(loadFfgsZones, 60000);

document.getElementById("ffgsMyLocationBtn").addEventListener("click", function() {
    const resultEl = document.getElementById("ffgsMyLocationResult");
    resultEl.hidden = false;
    resultEl.textContent = t("locating");

    if (!navigator.geolocation) {
        resultEl.textContent = t("noGeoSupport");
        return;
    }

    navigator.geolocation.getCurrentPosition(async function(pos) {
        const lat = pos.coords.latitude;
        const lon = pos.coords.longitude;

        resultEl.textContent = t("checkingGuidance");

        try {
            const rain = await fetchDurationRainfall(lat, lon);
            const antecedentParam = rain.antecedent_48h != null ? rain.antecedent_48h : "";
            const point = await (await fetch(
                "/ffgs/point?lat=" + lat + "&lon=" + lon + "&antecedent_48h_mm=" + antecedentParam
            )).json();

            if (!point.effective_class || !point.thresholds_mm) {
                resultEl.innerHTML = point.exact_match === false && point.distance_km != null
                    ? t("noMappedZoneNear").replace("{km}", point.distance_km)
                    : t("noMappedZone");
                return;
            }

            const rows = ffgsDurations.map(function(d) {
                const rainMm = rain[d];
                const thresholds = point.thresholds_mm[d];
                const status = statusForDuration(rainMm, thresholds);
                const rainText = rainMm == null ? "—" : rainMm.toFixed(1) + " mm";
                const critText = thresholds ? thresholds.critical.toFixed(0) + " mm" : "—";
                const badgeClass = "ffgs-badge ffgs-" + (status || "unmapped").toLowerCase();
                return "<tr><td>" + d + "</td><td>" + rainText + "</td><td>" + critText + "</td>" +
                    "<td><span class=\\"" + badgeClass + "\\">" + statusLabel(status) + "</span></td></tr>";
            }).join("");

            // The "nearest mapped zone, Xkm away" note only makes sense
            // for an atlas class. An FFPI class is computed at this exact
            // point, so there is no distance to disclose.
            const approxNote = (point.hazard_source === "atlas" && !point.exact_match && point.distance_km != null)
                ? t("nearestZoneNote").replace("{km}", point.distance_km)
                : "";

            const contextLine = physicalContextLine(point);

            resultEl.innerHTML = "<b>" + hazardCellText(point) + t("hazardZoneSuffix") + "</b>" +
                (point.ffpi != null ? " · FFPI " + point.ffpi.toFixed(1) : "") + approxNote +
                (contextLine ? "<br><span style='color:#6b7680; font-size:12px;'>" + contextLine + "</span>" : "") +
                '<table class="popup-table" style="margin-top:8px;"><thead><tr><th>' + t("popupWindow") + "</th><th>" + t("popupRain") + "</th><th>" + t("popupCriticalAt") + "</th><th>" + t("popupStatus") + "</th></tr></thead><tbody>" +
                rows + "</tbody></table>";
        } catch (error) {
            resultEl.textContent = t("locationError");
        }
    }, function() {
        resultEl.textContent = t("locationDenied");
    });
});

</script>

</body>
</html>
"""


@app.route("/ffgs")
def ffgs_page():

    return FFGS_PAGE_HTML


@app.route("/ffgs/hazard-atlas.geojson")
def ffgs_hazard_atlas():

    geojson_path = os.path.join(DATA_DIR, "uttarakhand_flash_flood_hazard_clean.geojson")

    if not os.path.exists(geojson_path):
        return jsonify({"type": "FeatureCollection", "features": []})

    return send_file(geojson_path, mimetype="application/geo+json")


@app.route("/ffgs/watersheds.geojson")
def ffgs_watersheds():

    geojson_path = os.path.join(DATA_DIR, "uttarakhand_watersheds.geojson")

    if not os.path.exists(geojson_path):
        return jsonify({"type": "FeatureCollection", "features": []})

    return send_file(geojson_path, mimetype="application/geo+json")


# ============================================================
# FFGS LIVE RAINFALL — server-side, cached
#
# Originally fetched by each visitor's own browser directly from
# Open-Meteo (one batched call per page load), to avoid Render's
# shared outbound IP getting rate-limited the way it did for the old /weather proxy
# and Nominatim before (see their own comments). In practice, though,
# every visitor's FFGS page wants the exact same data — the same
# fixed zone list — so client-side fetching means N visitors each
# independently re-fetch identical data instead of sharing one
# answer, and a handful of people testing from the same network (a
# hackathon venue's WiFi, for instance) can exhaust Open-Meteo's
# free-tier daily quota for that shared IP within minutes — which is
# exactly what happened during development here. A short server-side
# cache fixes both: one outbound call per
# FFGS_RAINFALL_CACHE_TTL_SECONDS serves every visitor, and a failed
# refresh falls back to the last good reading instead of leaving the
# whole page blank — same resilience pattern as /town-rainfall above.
# "Check my location" stays a genuine client-side fetch (see
# fetchDurationRainfall in FFGS_PAGE_HTML) — that's a one-off,
# per-visitor, arbitrary point, not worth caching.
# ============================================================

FFGS_RAINFALL_CACHE_TTL_SECONDS = 10 * 60
# last_error / last_attempt are diagnostics, surfaced on /ffgs/zones.
# Without them a failed upstream fetch is indistinguishable from genuinely
# dry weather: both render an empty rainfall column, and the only way to
# tell them apart was reading the host's logs.
_ffgs_rainfall_cache = {
    "timestamp": 0.0,
    "data": {},
    "last_error": None,
    "last_attempt": 0.0,
}

# Zones are collapsed onto a grid this coarse before fetching, and every
# zone in a cell shares that cell's reading.
#
# Open-Meteo counts each *location* in a multi-location request as its
# own API call against a 10,000/day free-tier limit. At a 10-minute
# refresh that is 144 refreshes a day, so 127 zones cost 18,288
# calls/day -- 83% over budget, which is exactly what silently emptied
# the live site's rainfall column after the FFPI work grew the zone list
# from 74 to 127.
#
# Raising the TTL would work but costs freshness on a flash-flood
# product. Deduplicating costs nothing real instead: Open-Meteo's
# underlying models are ~11 km, so the twenty Rishikesh localities all
# sit inside one model cell and were being asked the same question
# twenty times. 0.1 degrees is ~11 km here, matching that resolution and
# taking 127 zones down to 34 fetches -- 4,896 calls/day, comfortably
# inside the limit with the 10-minute refresh kept intact.
FFGS_RAINFALL_GRID_DEG = 0.1


def _group_ffgs_zones_by_rainfall_cell():
    cells = {}

    for zone in FFGS_ZONES:
        if not zone.get("effective_class"):
            continue
        key = (
            round(zone["lat"] / FFGS_RAINFALL_GRID_DEG),
            round(zone["lon"] / FFGS_RAINFALL_GRID_DEG),
        )
        cells.setdefault(key, []).append(zone)

    return list(cells.values())


# The zone list is fixed at import, so the cells are too. Each cell is
# fetched at its first zone's coordinates. /ffgs/zones publishes the
# same list (and each zone's index into it) so the browser fallback
# asks Open-Meteo for exactly the points the server would have.
FFGS_RAINFALL_CELLS = _group_ffgs_zones_by_rainfall_cell()
FFGS_RAIN_CELL_BY_POINT = {
    (zone["lat"], zone["lon"]): i
    for i, zones in enumerate(FFGS_RAINFALL_CELLS)
    for zone in zones
}


def _parse_open_meteo_durations(payload):

    hourly = payload.get("hourly") or {}
    hourly_times = hourly.get("time") or []
    hourly_precip = hourly.get("precipitation") or []
    current_time = (payload.get("current") or {}).get("time")

    try:
        idx = hourly_times.index(current_time) if current_time else len(hourly_times) - 1
    except ValueError:
        idx = len(hourly_times) - 1

    def sum_last(n):
        if idx < 0:
            return None
        start = max(0, idx - n + 1)
        return round(sum(float(v or 0.0) for v in hourly_precip[start:idx + 1]), 2)

    return {
        "1h": sum_last(1),
        "3h": sum_last(3),
        "24h": sum_last(24),
        "antecedent_48h": sum_last(48),
    }


def _fetch_ffgs_live_rainfall():
    """
    One batched Open-Meteo call covering every FFGS zone that has a
    class to compare rainfall against, cached for
    FFGS_RAINFALL_CACHE_TTL_SECONDS. Returns
    {(lat, lon): {"1h", "3h", "24h", "antecedent_48h"}} — the last
    successful reading if this refresh fails, or {} if there's never
    been a successful fetch.

    Keyed on effective_class, not hazard_class: zones whose class comes
    from FFPI rather than the hazard atlas have thresholds to breach
    just the same, and filtering on the atlas class would have left all
    53 of them permanently without a rainfall reading.
    """

    now = time.time()
    cache = _ffgs_rainfall_cache

    if cache["data"] and (now - cache["timestamp"]) < FFGS_RAINFALL_CACHE_TTL_SECONDS:
        return cache["data"]

    if _rainfall_backoff_active(cache, now):
        return cache["data"]

    if not FFGS_RAINFALL_CELLS:
        return cache["data"]

    # Collapsed onto the weather model's own resolution — see
    # FFGS_RAINFALL_GRID_DEG. Every zone in a cell reads that cell's
    # result.
    representatives = [zones[0] for zones in FFGS_RAINFALL_CELLS]
    response = None

    try:

        response = requests.get(
            "https://api.open-meteo.com/v1/forecast",

            params={
                "latitude": ",".join(str(z["lat"]) for z in representatives),
                "longitude": ",".join(str(z["lon"]) for z in representatives),
                "current": "precipitation",
                "hourly": "precipitation",
                "past_days": 2,
                "forecast_days": 1,
                "timezone": "auto",
            },

            timeout=30
        )

        response.raise_for_status()
        payload = response.json()

        # Open-Meteo returns a bare object (not a list) for a single
        # location, and a list of one object per location otherwise.
        per_location = payload if isinstance(payload, list) else [payload]

        fresh = {}

        for (cell_zones, loc) in zip(FFGS_RAINFALL_CELLS, per_location):

            reading = _parse_open_meteo_durations(loc)

            for zone in cell_zones:
                fresh[(zone["lat"], zone["lon"])] = reading

        _ffgs_rainfall_cache["timestamp"] = now
        _ffgs_rainfall_cache["data"] = fresh
        _ffgs_rainfall_cache["last_error"] = None
        _ffgs_rainfall_cache["last_attempt"] = now

        print(
            f"FFGS rainfall refreshed: {len(FFGS_RAIN_CELL_BY_POINT)} zones "
            f"served by {len(representatives)} fetches",
            flush=True
        )

        return fresh

    except Exception as e:

        # A quota block is the failure this system actually hits, and it
        # reads as an ordinary HTTP error unless the body is inspected.
        _ffgs_rainfall_cache["last_error"] = _describe_open_meteo_failure(response, e)
        _ffgs_rainfall_cache["last_attempt"] = now

        print("WARNING: FFGS live rainfall refresh failed:",
              _ffgs_rainfall_cache["last_error"], flush=True)
        return cache["data"]


# ============================================================
# FLASH FLOOD GUIDANCE SYSTEM (FFGS) — endpoints
# ============================================================

@app.route("/ffgs/zones")
def ffgs_zones():

    rainfall_by_point = _fetch_ffgs_live_rainfall()

    zones_with_rainfall = [
        dict(
            zone,
            live_rainfall=rainfall_by_point.get((zone["lat"], zone["lon"])),
            rain_cell=FFGS_RAIN_CELL_BY_POINT.get((zone["lat"], zone["lon"])),
        )
        for zone in FFGS_ZONES
    ]

    cache = _ffgs_rainfall_cache

    return jsonify({
        "available": GUIDANCE_AVAILABLE,
        "error": None if GUIDANCE_AVAILABLE else GUIDANCE_ERROR,
        "durations": list(FFGS_DURATIONS),
        "rainfall": {
            "zones_with_data": sum(1 for z in zones_with_rainfall if z.get("live_rainfall")),
            "age_seconds": (round(time.time() - cache["timestamp"], 1)
                            if cache["timestamp"] else None),
            "last_error": cache["last_error"],
            # For the browser fallback (/rainfall-fallback.js): the
            # points to fetch when this server has no reading at all.
            "cells": [[zones[0]["lat"], zones[0]["lon"]] for zones in FFGS_RAINFALL_CELLS],
        },
        "zones": zones_with_rainfall,
    })


@app.route("/ffgs/point")
def ffgs_point():

    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
    except (TypeError, ValueError):
        return jsonify({
            "error": "lat and lon query parameters are required numbers."
        }), 400

    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return jsonify({
            "error": "lat/lon out of range."
        }), 400

    antecedent_48h_mm = request.args.get("antecedent_48h_mm", type=float)

    return jsonify(ffgs_guidance_for_point(lat, lon, antecedent_48h_mm))


# ============================================================
# SHELTERS
# ============================================================

@app.route("/shelters")
def get_shelters():

    return jsonify(SHELTERS_WITH_LOCALITY)


# ============================================================
# EVACUATE — route to the nearest reachable shelter
# ============================================================

@app.route("/evacuate", methods=["POST"])
def evacuate():

    if not ROUTING_ENGINE_AVAILABLE:

        return jsonify({
            "status": "error",
            "error": "Routing is temporarily unavailable.",
            "details": ROUTING_ENGINE_ERROR or "Routing engine failed to load."
        }), 503

    try:

        data = request.get_json(force=True, silent=True)

        if not isinstance(data, dict):

            return jsonify({
                "status": "error",
                "error": "Request body must be valid JSON with lat, lon."
            }), 400

        try:

            lat = float(data.get("lat"))
            lon = float(data.get("lon"))

        except (TypeError, ValueError):

            return jsonify({
                "status": "error",
                "error": "lat and lon must be numbers."
            }), 400

        if not (-90.0 <= lat <= 90.0):

            return jsonify({
                "status": "error",
                "error": "lat must be between -90 and 90."
            }), 400

        if not (-180.0 <= lon <= 180.0):

            return jsonify({
                "status": "error",
                "error": "lon must be between -180 and 180."
            }), 400

        mode = str(data.get("mode", "SAFEST")).upper()

        if mode not in ["FASTEST", "SAFEST"]:
            mode = "SAFEST"

        reports_list = [
            [report["lon"], report["lat"]]
            for report in _active_reports()
        ]

        live_rain_mm = data.get("live_rain_mm")

        try:
            live_rain_mm = (
                float(live_rain_mm) if live_rain_mm is not None else None
            )
        except (TypeError, ValueError):
            live_rain_mm = None

        print()
        print("================================")
        print("EVACUATE")
        print("================================")
        print("From:", lat, lon, "mode:", mode)

        outcome = find_nearest_shelter(
            start_lon=lon,
            start_lat=lat,
            mode=mode,
            report_points=reports_list,
            live_rain_mm=live_rain_mm
        )

        if outcome is None:

            return jsonify({
                "status": "error",
                "error": "No reachable shelter found.",
                "details":
                    "Either no shelters are loaded for this region, or "
                    "none could be reached from this location."
            }), 422

        response = _shape_route_result(outcome["route"], mode)
        response["status"] = "ok"
        response["shelter"] = {
            "name": outcome["shelter"]["name"],
            "lat": outcome["shelter"]["lat"],
            "lon": outcome["shelter"]["lon"]
        }

        print(
            "Nearest reachable shelter:",
            outcome["shelter"]["name"],
            f"({response['distance_km']:.2f} km)"
        )

        return jsonify(response)

    except Exception as e:

        print()
        print("EVACUATE ERROR:", repr(e))

        return jsonify({
            "status": "error",
            "error": "Evacuation routing failed",
            "details": str(e)
        }), 500


# ============================================================
# NEAREST HOSPITAL — route to the nearest reachable hospital
#
# Same shortlist-then-route logic as /evacuate, but against the
# hospital target list instead of shelters — for "I need a
# hospital" rather than "I need to flee a flood."
# ============================================================

@app.route("/nearest-hospital", methods=["POST"])
def nearest_hospital():

    if not ROUTING_ENGINE_AVAILABLE:

        return jsonify({
            "status": "error",
            "error": "Routing is temporarily unavailable.",
            "details": ROUTING_ENGINE_ERROR or "Routing engine failed to load."
        }), 503

    try:

        data = request.get_json(force=True, silent=True)

        if not isinstance(data, dict):

            return jsonify({
                "status": "error",
                "error": "Request body must be valid JSON with lat, lon."
            }), 400

        try:

            lat = float(data.get("lat"))
            lon = float(data.get("lon"))

        except (TypeError, ValueError):

            return jsonify({
                "status": "error",
                "error": "lat and lon must be numbers."
            }), 400

        if not (-90.0 <= lat <= 90.0):

            return jsonify({
                "status": "error",
                "error": "lat must be between -90 and 90."
            }), 400

        if not (-180.0 <= lon <= 180.0):

            return jsonify({
                "status": "error",
                "error": "lon must be between -180 and 180."
            }), 400

        mode = str(data.get("mode", "SAFEST")).upper()

        if mode not in ["FASTEST", "SAFEST"]:
            mode = "SAFEST"

        reports_list = [
            [report["lon"], report["lat"]]
            for report in _active_reports()
        ]

        live_rain_mm = data.get("live_rain_mm")

        try:
            live_rain_mm = (
                float(live_rain_mm) if live_rain_mm is not None else None
            )
        except (TypeError, ValueError):
            live_rain_mm = None

        print()
        print("================================")
        print("NEAREST HOSPITAL")
        print("================================")
        print("From:", lat, lon, "mode:", mode)

        outcome = find_nearest_hospital(
            start_lon=lon,
            start_lat=lat,
            mode=mode,
            report_points=reports_list,
            live_rain_mm=live_rain_mm
        )

        if outcome is None:

            return jsonify({
                "status": "error",
                "error": "No reachable hospital found.",
                "details":
                    "Either no hospitals are loaded for this region, or "
                    "none could be reached from this location."
            }), 422

        response = _shape_route_result(outcome["route"], mode)
        response["status"] = "ok"
        response["hospital"] = {
            "name": outcome["hospital"]["name"],
            "lat": outcome["hospital"]["lat"],
            "lon": outcome["hospital"]["lon"]
        }

        print(
            "Nearest reachable hospital:",
            outcome["hospital"]["name"],
            f"({response['distance_km']:.2f} km)"
        )

        return jsonify(response)

    except Exception as e:

        print()
        print("NEAREST HOSPITAL ERROR:", repr(e))

        return jsonify({
            "status": "error",
            "error": "Hospital routing failed",
            "details": str(e)
        }), 500


# ============================================================
# ROUTE
# ============================================================

@app.route("/route", methods=["POST"])
def route():

    # --------------------------------------------------------
    # ROUTING ENGINE UNAVAILABLE
    # --------------------------------------------------------

    if not ROUTING_ENGINE_AVAILABLE:

        return jsonify({

            "status": "error",

            "error":
                "Routing is temporarily unavailable.",

            "details":
                ROUTING_ENGINE_ERROR
                or "Routing engine failed to load."

        }), 503

    try:

        # ----------------------------------------------------
        # GET REQUEST DATA
        # ----------------------------------------------------

        data = request.get_json(force=True, silent=True)

        if not isinstance(data, dict):

            return jsonify({

                "status": "error",

                "error":
                    "Request body must be valid JSON with "
                    "start_lat, start_lon, end_lat, end_lon."

            }), 400

        required_fields = [
            "start_lat", "start_lon", "end_lat", "end_lon"
        ]

        missing_fields = [
            field for field in required_fields
            if field not in data or data[field] in (None, "")
        ]

        if missing_fields:

            return jsonify({

                "status": "error",

                "error":
                    "Missing required field(s): "
                    + ", ".join(missing_fields)

            }), 400

        try:

            start_lat = float(data["start_lat"])
            start_lon = float(data["start_lon"])

            end_lat = float(data["end_lat"])
            end_lon = float(data["end_lon"])

        except (TypeError, ValueError):

            return jsonify({

                "status": "error",

                "error":
                    "start_lat, start_lon, end_lat and end_lon "
                    "must all be numbers."

            }), 400

        # ----------------------------------------------------
        # VALIDATE COORDINATE RANGES
        # ----------------------------------------------------

        for label, lat_value in (
            ("start_lat", start_lat),
            ("end_lat", end_lat)
        ):

            if not (-90.0 <= lat_value <= 90.0):

                return jsonify({

                    "status": "error",

                    "error":
                        f"{label} must be between -90 and 90."

                }), 400

        for label, lon_value in (
            ("start_lon", start_lon),
            ("end_lon", end_lon)
        ):

            if not (-180.0 <= lon_value <= 180.0):

                return jsonify({

                    "status": "error",

                    "error":
                        f"{label} must be between -180 and 180."

                }), 400

        if (
            start_lat == end_lat
            and start_lon == end_lon
        ):

            return jsonify({

                "status": "error",

                "error":
                    "Start and destination are the same location."

            }), 400

        mode = str(
            data.get("mode", "SAFEST")
        ).upper()

        if mode not in [
            "FASTEST",
            "SAFEST"
        ]:

            mode = "SAFEST"

        # ----------------------------------------------------
        # PRINT REQUEST
        # ----------------------------------------------------

        print()
        print("================================")
        print("ROUTING")
        print("================================")

        print("Mode:", mode)

        print(
            "Start:",
            start_lat,
            start_lon
        )

        print(
            "Destination:",
            end_lat,
            end_lon
        )

        # ----------------------------------------------------
        # CALCULATE ROUTE
        # ----------------------------------------------------

        reports_list = [
            [report["lon"], report["lat"]]
            for report in _active_reports()
        ]

        live_rain_mm = data.get("live_rain_mm")

        try:
            live_rain_mm = (
                float(live_rain_mm)
                if live_rain_mm is not None
                else None
            )
        except (TypeError, ValueError):
            live_rain_mm = None

        result = calculate_route(
            start_lat=start_lat,
            start_lon=start_lon,
            end_lat=end_lat,
            end_lon=end_lon,
            mode=mode,
            report_points=reports_list,
            live_rain_mm=live_rain_mm
        )

        # ----------------------------------------------------
        # NO ROUTE FOUND
        #
        # calculate_route() returns None (not an exception) when
        # the two points aren't connected in the directed road
        # graph. Handle that explicitly instead of letting it
        # fall through to a subscript error.
        # ----------------------------------------------------

        if result is None:

            return jsonify({

                "status": "error",

                "error":
                    "No route found between these locations.",

                "details":
                    "The start and destination are not connected "
                    "in the road network graph."

            }), 422

        # ----------------------------------------------------
        # PATH
        # ----------------------------------------------------

        path = result["path"]

        if path is None or len(path) == 0:

            raise ValueError(
                "Routing engine returned an empty route."
            )

        # ----------------------------------------------------
        # CONVERT NODE PATH TO COORDINATES
        #
        # coordinates.npy format:
        # [longitude, latitude]
        #
        # Leaflet format:
        # [latitude, longitude]
        # ----------------------------------------------------

        route_coordinates = []

        for node in path:

            lon = float(
                coordinates[node][0]
            )

            lat = float(
                coordinates[node][1]
            )

            route_coordinates.append([
                lon,
                lat
            ])

        # ----------------------------------------------------
        # DISTANCE
        # ----------------------------------------------------

        distance_km = result.get(
            "distance_km"
        )

        distance_m = result.get(
            "distance_m"
        )

        if distance_km is None:

            if distance_m is None:

                raise KeyError(
                    "Routing engine returned "
                    "neither distance_km nor distance_m."
                )

            distance_m = float(distance_m)

            distance_km = (
                distance_m / 1000.0
            )

        else:

            distance_km = float(
                distance_km
            )

        if distance_m is None:

            distance_m = (
                distance_km * 1000.0
            )

        else:

            distance_m = float(
                distance_m
            )

        # ----------------------------------------------------
        # RISK COUNTS
        # ----------------------------------------------------

        raw_risk_counts = result.get(
            "risk_counts",
            {}
        )

        risk_counts = {}

        for key, value in raw_risk_counts.items():

            try:

                risk_counts[str(key)] = int(
                    value
                )

            except (ValueError, TypeError):

                pass

        # ----------------------------------------------------
        # SEGMENT RISKS
        #
        # One risk value per hop between consecutive route
        # coordinates (len(coordinates) - 1 entries), so the
        # frontend can color each stretch of the drawn route by
        # its own risk instead of drawing it as one flat color.
        # ----------------------------------------------------

        raw_segment_risks = result.get(
            "segment_risks",
            []
        )

        segment_risks = []

        for value in raw_segment_risks:

            try:

                segment_risks.append(
                    float(value)
                )

            except (ValueError, TypeError):

                segment_risks.append(1.0)

        # ----------------------------------------------------
        # RESPONSE
        # ----------------------------------------------------

        response = {

            "status": "ok",

            "mode": mode,

            "distance_km":
                distance_km,

            "distance_m":
                distance_m,

            "nodes":
                int(len(path)),

            "road_nodes":
                int(len(path)),

            "coordinates":
                route_coordinates,

            "risk_counts":
                risk_counts,

            "segment_risks":
                segment_risks
        }

        # ----------------------------------------------------
        # SNAP DISTANCES
        # ----------------------------------------------------

        if "snap_start_m" in result:

            response["snap_start_m"] = float(
                result["snap_start_m"]
            )

        if "snap_end_m" in result:

            response["snap_end_m"] = float(
                result["snap_end_m"]
            )

        # ----------------------------------------------------
        # EXTREME-RISK UNAVOIDABLE FLAG
        #
        # True only when SAFEST's fully extreme-avoiding search
        # still had to cross an EXTREME segment — i.e. no
        # extreme-free path exists at all between these two
        # points. If a safe-but-long path existed and was instead
        # swapped for a shorter one that crosses EXTREME (see
        # "safest_capped" below), this stays False: that's a
        # distance/safety tradeoff, not unavoidability.
        # ----------------------------------------------------

        response["extreme_unavoidable"] = bool(
            result.get("extreme_unavoidable", False)
        )

        response["live_escalation_applied"] = bool(
            result.get("live_escalation_applied", False)
        )

        response["reported_hazards_on_route"] = int(
            result.get("reported_hazard_count", 0)
        )

        response["safest_capped"] = bool(
            result.get("safest_capped", False)
        )

        # ----------------------------------------------------
        # PRINT RESULT
        # ----------------------------------------------------

        print()
        print("========== ROUTE RESULT ==========")

        print(
            "Distance:",
            round(distance_km, 2),
            "km"
        )

        print(
            "Road nodes:",
            len(path)
        )

        print()
        print("Flood-risk edges:")

        print(
            "Normal:",
            risk_counts.get("1.0", 0)
        )

        print(
            "Low:",
            risk_counts.get("2.0", 0)
        )

        print(
            "Significant:",
            risk_counts.get("4.0", 0)
        )

        print(
            "Extreme:",
            risk_counts.get("8.0", 0)
        )

        print()

        return jsonify(response)

    # ========================================================
    # ERRORS
    # ========================================================

    except KeyError as e:

        print()
        print("ROUTE KEY ERROR:", repr(e))

        return jsonify({

            "status": "error",

            "error":
                "Missing routing parameter or result field",

            "details":
                str(e)

        }), 400

    except ValueError as e:

        print()
        print("ROUTE VALUE ERROR:", repr(e))

        return jsonify({

            "status": "error",

            "error":
                "Invalid routing input",

            "details":
                str(e)

        }), 400

    except Exception as e:

        print()
        print("ROUTE ERROR:", repr(e))

        return jsonify({

            "status": "error",

            "error":
                "Route calculation failed",

            "details":
                str(e)

        }), 500


# ============================================================
# COMPARE ROUTES
#
# Runs FASTEST and SAFEST together so the frontend can draw
# both at once instead of two separate /route round-trips.
# Reuses the same coordinate/risk shaping as /route, kept as a
# small self-contained helper here rather than refactoring the
# larger, already-working /route handler above.
# ============================================================

def _shape_route_result(result, mode):

    path = result["path"]

    route_coordinates = [
        [float(coordinates[node][0]), float(coordinates[node][1])]
        for node in path
    ]

    risk_counts = {}

    for key, value in result.get("risk_counts", {}).items():

        try:
            risk_counts[str(key)] = int(value)
        except (ValueError, TypeError):
            pass

    segment_risks = []

    for value in result.get("segment_risks", []):

        try:
            segment_risks.append(float(value))
        except (ValueError, TypeError):
            segment_risks.append(1.0)

    return {
        "mode": mode,
        "distance_km": float(result["distance_m"]) / 1000.0,
        "coordinates": route_coordinates,
        "risk_counts": risk_counts,
        "segment_risks": segment_risks,
        "extreme_unavoidable": bool(result.get("extreme_unavoidable", False)),
        "reported_hazards_on_route": int(result.get("reported_hazard_count", 0)),
        "safest_capped": bool(result.get("safest_capped", False))
    }


@app.route("/compare", methods=["POST"])
def compare():

    if not ROUTING_ENGINE_AVAILABLE:

        return jsonify({
            "status": "error",
            "error": "Routing is temporarily unavailable.",
            "details": ROUTING_ENGINE_ERROR or "Routing engine failed to load."
        }), 503

    try:

        data = request.get_json(force=True, silent=True)

        if not isinstance(data, dict):

            return jsonify({
                "status": "error",
                "error":
                    "Request body must be valid JSON with "
                    "start_lat, start_lon, end_lat, end_lon."
            }), 400

        required_fields = ["start_lat", "start_lon", "end_lat", "end_lon"]

        missing_fields = [
            field for field in required_fields
            if field not in data or data[field] in (None, "")
        ]

        if missing_fields:

            return jsonify({
                "status": "error",
                "error": "Missing required field(s): " + ", ".join(missing_fields)
            }), 400

        try:

            start_lat = float(data["start_lat"])
            start_lon = float(data["start_lon"])
            end_lat = float(data["end_lat"])
            end_lon = float(data["end_lon"])

        except (TypeError, ValueError):

            return jsonify({
                "status": "error",
                "error":
                    "start_lat, start_lon, end_lat and end_lon "
                    "must all be numbers."
            }), 400

        for label, value in (
            ("start_lat", start_lat), ("end_lat", end_lat)
        ):
            if not (-90.0 <= value <= 90.0):
                return jsonify({
                    "status": "error",
                    "error": f"{label} must be between -90 and 90."
                }), 400

        for label, value in (
            ("start_lon", start_lon), ("end_lon", end_lon)
        ):
            if not (-180.0 <= value <= 180.0):
                return jsonify({
                    "status": "error",
                    "error": f"{label} must be between -180 and 180."
                }), 400

        if start_lat == end_lat and start_lon == end_lon:
            return jsonify({
                "status": "error",
                "error": "Start and destination are the same location."
            }), 400

        reports_list = [
            [report["lon"], report["lat"]]
            for report in _active_reports()
        ]

        live_rain_mm = data.get("live_rain_mm")

        try:
            live_rain_mm = (
                float(live_rain_mm) if live_rain_mm is not None else None
            )
        except (TypeError, ValueError):
            live_rain_mm = None

        results = {}

        for mode in ("FASTEST", "SAFEST"):

            result = calculate_route(
                start_lat=start_lat,
                start_lon=start_lon,
                end_lat=end_lat,
                end_lon=end_lon,
                mode=mode,
                report_points=reports_list,
                live_rain_mm=live_rain_mm
            )

            if result is None:

                return jsonify({
                    "status": "error",
                    "error": "No route found between these locations.",
                    "details":
                        "The start and destination are not connected "
                        "in the road network graph."
                }), 422

            results[mode] = _shape_route_result(result, mode)

        return jsonify({
            "status": "ok",
            "fastest": results["FASTEST"],
            "safest": results["SAFEST"]
        })

    except Exception as e:

        print()
        print("COMPARE ERROR:", repr(e))

        return jsonify({
            "status": "error",
            "error": "Route comparison failed",
            "details": str(e)
        }), 500


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":

    import socket

    def get_local_ip():

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    local_ip = get_local_ip()

    print()
    print("========================================")
    print("        FLOODSAFE SERVER")
    print("========================================")
    print()

    print("On this computer:")
    print("http://127.0.0.1:5000/")
    print()

    print("On your phone (same WiFi network):")
    print(f"http://{local_ip}:5000/")
    print()

    print("Status:")
    print(f"http://{local_ip}:5000/status")
    print()

    print("Search:")
    print(f"http://{local_ip}:5000/search?q=Rishikesh")
    print()

    print("========================================")
    print()

    try:

        app.run(
            host="0.0.0.0",
            port=5000,
            debug=False,
            threaded=True
        )

    except OSError as e:

        print()
        print("========================================")
        print("SERVER FAILED TO START")
        print("========================================")
        print(
            "Port 5000 may already be in use, or another "
            "network error occurred."
        )
        print("Details:", e)
        print()