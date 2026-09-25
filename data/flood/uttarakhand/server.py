import os
import json
import math
import time
import uuid
import hmac
import base64
import threading
from urllib.parse import urlparse

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
        find_nearest_shelter, find_nearest_hospital,
        road_snap_km, MAX_SNAP_KM
    )

    ROUTING_ENGINE_AVAILABLE = True

except Exception as e:

    calculate_route = None
    road_snap_km = None
    MAX_SNAP_KM = None
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
# ONE ROUTE AT A TIME
#
# The server runs several threads in one process (gunicorn.conf.py) so
# a route computation no longer freezes every other page: on the single
# sync worker, /status took 7.3 s while one evacuation ran, against
# 0.36 s idle. Route computations themselves still go one at a time --
# each briefly holds ~150 MB of arrays, and two at once could push the
# 512 MB instance over its limit. Others queue here; everything else
# carries on.
# ============================================================

_routing_slot = threading.Lock()


def _one_route_at_a_time(fn):

    if fn is None:
        return None

    def run(*args, **kwargs):
        with _routing_slot:
            return fn(*args, **kwargs)

    return run


def _single_flight(fallback):
    """
    At most one thread runs the wrapped refresh at a time; any other
    caller meanwhile gets fallback() -- the current cached value --
    instead of starting a second upstream request for the same data.
    """
    import functools

    def wrap(fn):
        lock = threading.Lock()

        @functools.wraps(fn)
        def run(*args, **kwargs):
            if not lock.acquire(blocking=False):
                return fallback()
            try:
                return fn(*args, **kwargs)
            finally:
                lock.release()

        return run

    return wrap


calculate_route = _one_route_at_a_time(calculate_route)
find_nearest_shelter = _one_route_at_a_time(find_nearest_shelter)
find_nearest_hospital = _one_route_at_a_time(find_nearest_hospital)

# ============================================================
# APP
# ============================================================

app = Flask(__name__)

# Nothing this app accepts comes near this -- the largest body is a
# rainfall relay post of about 0.2 MB. Without a cap, one oversized
# upload is read into memory whole on a 512 MB instance.
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

# Read-only, cross-origin: the public data (zones, rainfall, official
# warnings) may be read by other sites. This used to be CORS(app), which
# reflected any origin and allowed every method, so any web page could
# make its visitors' browsers post, confirm or resolve hazard reports.
CORS(app, methods=["GET", "HEAD", "OPTIONS"])


# ============================================================
# CROSS-SITE WRITES AND BROWSER HARDENING
#
# Browsers attach an Origin header (and Sec-Fetch-Site) to every
# cross-site POST, including a plain HTML form that needs no CORS
# preflight. So any state-changing request whose Origin isn't this
# site is refused: a hostile page can no longer resolve every report
# through its visitors' browsers, each with a fresh IP that walks past
# the per-IP limits. Requests with no Origin -- curl, the rainfall relay
# -- are unaffected and stay rate-limited as before.
# ============================================================

@app.before_request
def refuse_cross_site_writes():

    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None

    origin = request.headers.get("Origin")
    fetch_site = request.headers.get("Sec-Fetch-Site", "")

    cross_site = (
        (origin and urlparse(origin).netloc != request.host)
        or fetch_site == "cross-site"
    )

    if cross_site:
        return jsonify({
            "status": "error",
            "error": "Cross-site requests can't change FloodSafe data."
        }), 403

    return None


@app.after_request
def security_headers(response):

    # No other site may frame these pages (clickjacking a "Resolve"
    # button), browsers must not guess content types, and cross-site
    # navigations carry only the origin.
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return response


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

    # The exception text stays in the log. It used to be returned to
    # the caller too, which hands out internals (file paths, library
    # messages) to anyone who can trigger an error.
    print()
    print("UNHANDLED SERVER ERROR:", repr(e), flush=True)

    return jsonify({
        "status": "error",
        "error": "Unexpected server error"
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
# Each report expires on its own after REPORT_EXPIRY_SECONDS so
# stale reports don't silently keep rerouting traffic forever.
#
# Storage: Postgres when DATABASE_URL is set, else a JSON file next to
# the other data files. The file alone does not survive a deploy on
# Render, whose free-tier disk is wiped on every restart -- so every
# live hazard report vanished whenever the app redeployed or woke from
# sleep. The in-memory list stays the working copy either way; the
# database is written one change at a time (upsert one report, delete
# given ids), never replaced wholesale, so a failed load at startup can
# never turn into a later write that erases stored reports.
# ============================================================

REPORTS_FILE = os.path.join(DATA_DIR, "reports.json")
REPORT_EXPIRY_SECONDS = 6 * 60 * 60
REPORT_DESCRIPTION_MAX_LENGTH = 300
REPORT_NAME_MAX_LENGTH = 80

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

REPORTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS flask_hazard_reports (
    id         text PRIMARY KEY,
    report     jsonb NOT NULL,
    created_at double precision NOT NULL
)
"""

# Surfaced on /status, so a storage failure is visible from outside
# instead of only in the host's logs.
_report_storage_state = {"last_error": None, "loaded": False, "last_load_attempt": 0.0}
REPORT_DB_RETRY_SECONDS = 60


def _db_connect():
    # Imported here so the file-backed mode (and the test suite) never
    # needs the driver.
    import psycopg
    return psycopg.connect(DATABASE_URL, connect_timeout=10, autocommit=True)


def _db_run(action, fn):
    """Run fn(conn); on failure record the error and return None."""

    try:
        with _db_connect() as conn:
            result = fn(conn)
        _report_storage_state["last_error"] = None
        return result
    except Exception as e:
        _report_storage_state["last_error"] = f"{action} failed: {type(e).__name__}"
        print(f"WARNING: report database {action} failed:", repr(e), flush=True)
        return None


def _db_load_reports():

    def load(conn):
        conn.execute(REPORTS_TABLE_SQL)
        rows = conn.execute("SELECT report FROM flask_hazard_reports").fetchall()
        return [row[0] for row in rows]

    _report_storage_state["last_load_attempt"] = time.time()
    loaded = _db_run("load", load)

    if loaded is not None:
        _report_storage_state["loaded"] = True

    return loaded


def _persist_report(report):
    """Store a new or changed report."""

    if not DATABASE_URL:
        _save_reports(_reports)
        return

    from psycopg.types.json import Jsonb

    _db_run("save", lambda conn: conn.execute(
        "INSERT INTO flask_hazard_reports (id, report, created_at) "
        "VALUES (%s, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET report = EXCLUDED.report",
        (report["id"], Jsonb(report), float(report.get("timestamp", 0))),
    ))


def _persist_removals(report_ids):
    """Remove resolved or expired reports from storage."""

    if not DATABASE_URL:
        _save_reports(_reports)
        return

    ids = [i for i in report_ids if i]
    if ids:
        _db_run("delete", lambda conn: conn.execute(
            "DELETE FROM flask_hazard_reports WHERE id = ANY(%s)", (ids,)))


def _retry_report_load_if_needed():
    """
    If the database was unreachable at startup, keep trying (at most once
    a minute) and merge in whatever it holds. Until then the app serves
    only reports made since it started, rather than none at all.
    """

    if not DATABASE_URL or _report_storage_state["loaded"]:
        return

    if time.time() - _report_storage_state["last_load_attempt"] < REPORT_DB_RETRY_SECONDS:
        return

    stored = _db_load_reports()

    if stored is None:
        return

    stored_ids = {r.get("id") for r in stored}
    memory_only = [r for r in _reports if r.get("id") not in stored_ids]

    known = {r.get("id") for r in _reports}
    _reports.extend(r for r in stored if r.get("id") not in known)

    # Reports made while the database was down only exist in memory.
    for report in memory_only:
        _persist_report(report)


def _load_reports():

    if DATABASE_URL:
        stored = _db_load_reports()
        return stored if stored is not None else []

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
    # /route, /compare, /evacuate and /nearest-hospital share one
    # allowance. One route over the 2M-node graph takes seconds, and
    # evacuate/hospital try up to five, on the single worker -- so an
    # unmetered loop could stall the app for everyone. Generous because
    # a room of people behind one venue NAT shares an address.
    "routing": (30, 60),
    # subscribe / unsubscribe / test notification
    "push": (10, 300),
}

_rate_buckets = {}
_rate_lock = threading.Lock()


def _client_ip_and_source():
    """
    The caller's address, and which header it came from.

    This used to take the *first* X-Forwarded-For entry. That entry is
    whatever the client sent -- proxies append to the header, they don't
    replace it -- so rotating a fake X-Forwarded-For gave every request a
    fresh identity. Confirmed against the live site: 22 of 22 requests
    went through with a spoofed header, while a fixed one was blocked
    after 20. That bypassed the report rate limit and the one-confirm-
    per-visitor guard, and let anyone resolve (delete) every report.

    The live site sits behind Cloudflare, which sets CF-Connecting-IP
    itself and overwrites any value the client supplies, so it is
    trusted first. Otherwise the *last* X-Forwarded-For entry is the one
    added by the nearest proxy, not by the client.
    """

    cf_ip = request.headers.get("CF-Connecting-IP", "").strip()
    if cf_ip:
        return cf_ip, "cf-connecting-ip"

    forwarded = [h.strip() for h in request.headers.get("X-Forwarded-For", "").split(",") if h.strip()]
    if forwarded:
        return forwarded[-1], "x-forwarded-for-last"

    return request.remote_addr or "unknown", "socket"


def _client_ip():
    """
    The caller's identity for rate limiting and confirm de-duplication.

    An IPv6 address is reduced to its /64: providers hand each home or
    phone a whole /64, so a client can use a fresh address from it on
    every request, which otherwise walks straight past per-IP limits.
    """

    import ipaddress

    raw = _client_ip_and_source()[0]

    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return raw

    if address.version == 6:
        return str(ipaddress.ip_network(f"{address}/64", strict=False))

    return str(address)


def _rate_limited(label):
    """
    True if this caller has exhausted `label`'s allowance. Sliding
    window: timestamps older than the window are dropped on each call,
    which also keeps the bucket from growing without bound.
    """

    max_requests, window = RATE_LIMITS[label]
    key = (label, _client_ip())

    with _rate_lock:
        return _rate_check(key, max_requests, window)


def _rate_check(key, max_requests, window):

    now = time.time()
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
    minutes = window // 60
    per = "minute" if minutes == 1 else f"{minutes} minutes"

    return jsonify({
        "status": "error",
        "error": f"Too many requests. Limit is {max_requests} per {per}.",
    }), 429


_reports = _load_reports()

# The server runs several threads, and _reports is shared. Every change
# to it -- new report, resolve, confirm, expiry, the retried database
# load -- happens under this lock, so two simultaneous requests can't
# lose each other's update (an append landing while expiry rewrites the
# list, say).
_reports_lock = threading.RLock()


def _holding_reports_lock(fn):
    import functools

    @functools.wraps(fn)
    def run(*args, **kwargs):
        with _reports_lock:
            return fn(*args, **kwargs)

    return run

# Tracks which client IPs have already confirmed which report, purely
# to stop the same visitor inflating a count by clicking repeatedly.
# Deliberately in-memory only (not persisted) — losing this on a
# restart just means a handful of IPs could each confirm once more,
# which is a low-stakes trade-off for a soft, best-effort guard, not
# a real identity system.
_confirmed_ips_by_report = {}


@_holding_reports_lock
def _active_reports():

    _retry_report_load_if_needed()

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
        _persist_removals(expired_ids)

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

    # The same 1h/3h/24h thresholds /ffgs uses -- flood-calibrated
    # (see CALIBRATED THRESHOLDS), the hand-set formula only outside the
    # calibrated area. This panel used its own single hand-set pair per
    # class against a "right now" reading, so the landing page and /ffgs
    # could judge the same town differently.
    ffgs = ffgs_guidance_for_point(lat, lon)

    return {
        "lat": lat,
        "lon": lon,
        "hazard_class": hazard_class,
        "effective_class": effective_class,
        "hazard_source": hazard_source,
        "ffpi": ffpi["ffpi"] if ffpi else None,
        "exact_match": exact,
        "distance_km": round(distance_km, 1) if distance_km is not None else None,
        "thresholds_mm": ffgs["thresholds_mm"],
        "threshold_source": ffgs["threshold_source"],
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


# ============================================================
# CALIBRATED THRESHOLDS
#
# The hand-set, hazard-class thresholds above were tested against 24
# monsoons (2000-2023) of IMD-recorded floods from the India Flood
# Inventory, with hourly ERA5 rainfall at 81 grid points across the
# state (floodsafe/pipeline/calibrate_thresholds.py). They fired on about
# a quarter of all monsoon district-days. Rain measured against each
# place's own climate did better at every false-alarm rate, so each FFGS
# zone now takes the calibrated thresholds of its nearest grid point,
# converted to the live feed's units (build_calibrated_thresholds.py).
#
# Operating point, chosen by the project owner ("balanced"): CRITICAL on
# ~10% of dry monsoon district-days, WATCH on ~20%. Validated
# leave-one-year-out, that caught 43% (CRITICAL) and 57% (WATCH) of
# recorded floods -- the numbers the page publishes.
#
# The antecedent-rain and watershed/soil multipliers are not applied on
# top: the calibration measured the rainfall rule alone, and antecedent
# rain on its own barely separated flood days (AUC 0.57). The hand-set
# formula remains only as a fallback if the calibration file is missing.
# ============================================================

def _load_calibrated_thresholds():
    path = os.path.join(DATA_DIR, "calibrated_thresholds.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        print("WARNING: calibrated thresholds unavailable, using the hand-set ones:", repr(e), flush=True)
        return None


CALIBRATED_THRESHOLDS = _load_calibrated_thresholds()

# Nearest grid point further than this (~33 km) is outside the calibrated
# area -- outside Uttarakhand -- and gets the hand-set fallback instead.
CALIBRATED_MAX_DEG = 0.3


def _nearest_point_thresholds(table, lat, lon):
    """The thresholds_mm of the table's nearest grid point, or None."""

    if not table:
        return None

    lon_scale = math.cos(math.radians(lat))
    best, best_d2 = None, None

    for point in table["points"]:
        d2 = (point["lat"] - lat) ** 2 + ((point["lon"] - lon) * lon_scale) ** 2
        if best_d2 is None or d2 < best_d2:
            best, best_d2 = point, d2

    if best is None or best_d2 > CALIBRATED_MAX_DEG ** 2:
        return None

    return {window: dict(levels) for window, levels in best["thresholds_mm"].items()}


def calibrated_thresholds_for_point(lat, lon):

    return _nearest_point_thresholds(CALIBRATED_THRESHOLDS, lat, lon)


# ============================================================
# LANDSLIDE THRESHOLDS
#
# Rain over 1, 3 and 7 days against each place's own climate, calibrated
# on rain-triggered landslides from NASA's Global Landslide Catalog
# (floodsafe/pipeline/calibrate_landslide_thresholds.py and
# build_landslide_thresholds.py). Landslides follow soil saturation over
# days, which the flood rule's 1h/3h/24h peaks miss.
#
# The whole layer hangs on data/landslide_thresholds.json: the builder
# only writes it when the landslide rule catches more landslides than
# the flood rule at the same false-alarm rate. Without the file nothing
# changes -- no landslide status anywhere, and the rainfall feed keeps
# its 2-day history instead of the 7 days the 168h window needs.
# ============================================================

LANDSLIDE_DURATIONS = ("24h", "72h", "168h")


def _load_landslide_thresholds():
    path = os.path.join(DATA_DIR, "landslide_thresholds.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            table = json.load(f)
        for point in table["points"]:
            for window in LANDSLIDE_DURATIONS:
                pair = point["thresholds_mm"][window]
                if not 0 < pair["watch"] < pair["critical"]:
                    raise ValueError(f"bad {window} thresholds at {point['lat']},{point['lon']}")
        return table
    except (OSError, ValueError, KeyError, TypeError) as e:
        print("WARNING: landslide thresholds unreadable, landslide layer off:", repr(e), flush=True)
        return None


LANDSLIDE_THRESHOLDS = _load_landslide_thresholds()


def landslide_thresholds_for_point(lat, lon):

    return _nearest_point_thresholds(LANDSLIDE_THRESHOLDS, lat, lon)


def _landslide_summary():
    """What the page says about the layer, or {"available": False}."""

    if not LANDSLIDE_THRESHOLDS:
        return {"available": False}

    validation = LANDSLIDE_THRESHOLDS.get("validation", {})

    def pod(level):
        try:
            return validation[level]["POD"]
        except (KeyError, TypeError):
            return None

    return {
        "available": True,
        "durations": list(LANDSLIDE_DURATIONS),
        "episodes_scored": validation.get("landslide_episodes_scored"),
        "critical_pod": pod("critical"),
        "watch_pod": pod("watch"),
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

    # Calibrated thresholds where the calibration reaches (all of
    # Uttarakhand); the hand-set class formula only as a fallback.
    thresholds = calibrated_thresholds_for_point(lat, lon)
    threshold_source = "calibrated"

    if thresholds is None:
        thresholds = ffgs_thresholds_for_class(
            effective_class, antecedent_48h_mm, physical["static_multiplier"])
        threshold_source = "heuristic" if thresholds else None

    return {
        "lat": lat,
        "lon": lon,
        "hazard_class": hazard_class,
        "effective_class": effective_class,
        "hazard_source": hazard_source,
        "exact_match": exact,
        "distance_km": round(distance_km, 1) if distance_km is not None else None,
        "thresholds_mm": thresholds,
        "threshold_source": threshold_source,
        "landslide_thresholds_mm": landslide_thresholds_for_point(lat, lon),
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


# ============================================================
# DISTRICTS
#
# Official warnings name districts, so every zone needs one. Taken by
# location from the OpenStreetMap district polygons (admin_level 5)
# already in uttarakhand_boundary.geojson (see get_boundary.py), not
# from the zone's parent town: Rishikesh's localities, for one, fall in
# three districts -- Dehradun, Tehri Garhwal and Pauri Garhwal.
# ============================================================

def _load_districts():

    from shapely.geometry import shape as shapely_shape

    path = os.path.join(DATA_DIR, "uttarakhand_boundary.geojson")

    try:
        with open(path, "r", encoding="utf-8") as f:
            features = json.load(f)["features"]
    except (OSError, ValueError, KeyError) as e:
        print("WARNING: district boundaries unavailable:", repr(e), flush=True)
        return []

    districts = []

    for feature in features:
        props = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        if str(props.get("admin_level")) != "5" or not props.get("name"):
            continue
        if geometry.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        # OSM tags one of them "Pithoragarh district"; the rest are bare.
        name = props["name"].replace(" district", "").strip()
        districts.append((name, shapely_shape(geometry)))

    return districts


UTTARAKHAND_DISTRICTS = _load_districts()


def district_for_point(lat, lon):
    from shapely.geometry import Point as ShapelyPoint

    point = ShapelyPoint(lon, lat)

    for name, geometry in UTTARAKHAND_DISTRICTS:
        if geometry.contains(point):
            return name

    return None


for _zone in FFGS_ZONES:
    _zone["district"] = district_for_point(_zone["lat"], _zone["lon"])


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
# WEB APP MANIFEST
#
# Lets phones install FloodSafe to the home screen as an app. On an
# iPhone that is also the only way to get push alerts: Safari gives
# Web Push only to sites opened from a home-screen icon (iOS 16.4+),
# and the icon needs this manifest to open as an app rather than a
# browser tab. The icons in static/icons/ come from make_app_icons.py.
#
# Every page links it through APP_HEAD_TAGS; the map page gets the
# same tags from map_app.py.
# ============================================================

APP_THEME_COLOR = "#0b3558"

APP_HEAD_TAGS = f"""<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="{APP_THEME_COLOR}">
<link rel="icon" type="image/png" sizes="192x192" href="/static/icons/icon-192.png">
<link rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png">
<meta name="apple-mobile-web-app-title" content="FloodSafe">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
"""


def _with_app_head(html):
    return html.replace("</head>", APP_HEAD_TAGS + "</head>", 1)


WEB_APP_MANIFEST = {
    "id": "/",
    "name": "FloodSafe — Uttarakhand",
    "short_name": "FloodSafe",
    "description": "Flood-aware road routes, flash-flood guidance and push alerts for Uttarakhand.",
    "lang": "en",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#f3f5f6",
    "theme_color": APP_THEME_COLOR,
    "icons": [
        {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png",
         "purpose": "maskable"},
    ],
    # Long-press menu on Android's home-screen icon.
    "shortcuts": [
        {"name": "Road map", "short_name": "Map", "url": "/app"},
        {"name": "Flash flood guidance", "short_name": "Flood guidance", "url": "/ffgs"},
    ],
}


@app.route("/manifest.webmanifest")
def web_app_manifest():

    response = Response(json.dumps(WEB_APP_MANIFEST, ensure_ascii=False),
                        mimetype="application/manifest+json")
    response.headers["Cache-Control"] = "no-cache"
    return response


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
    <p class="guidance-intro" data-i18n="guidanceIntro">Live rain for each town against the same flood-calibrated 1h / 3h / 24h thresholds as the Flash Flood Guidance System, with the headroom left before the tightest window reaches CRITICAL.</p>
    <div style="margin-bottom:18px;">
      <a class="btn-official" href="/ffgs" data-i18n="guidanceOpenFfgs">Open full Flash Flood Guidance System →</a>
    </div>
    <table class="guidance-table">
      <thead>
        <tr>
          <th data-i18n="guidanceColTown">Location</th>
          <th data-i18n="guidanceColHazard">Hazard Zone</th>
          <th data-i18n="guidanceColRain">Rain 1h / 24h</th>
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
    guidanceIntro: "Live rain for each town against the same flood-calibrated 1h / 3h / 24h thresholds as the Flash Flood Guidance System, with the headroom left before the tightest window reaches CRITICAL.",
    guidanceColTown: "Location",
    guidanceColHazard: "Hazard Zone",
    guidanceColRain: "Rain 1h / 24h",
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
    guidanceIntro: "हर शहर की लाइव वर्षा की तुलना फ्लैश फ्लड गाइडेंस सिस्टम वाली उन्हीं बाढ़-कैलिब्रेटेड 1 घंटे / 3 घंटे / 24 घंटे की सीमाओं से, और सबसे नज़दीकी अवधि के गंभीर स्तर तक पहुँचने से पहले बची गुंजाइश।",
    guidanceColTown: "स्थान",
    guidanceColHazard: "खतरा क्षेत्र",
    guidanceColRain: "वर्षा 1 घंटा / 24 घंटे",
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
// Each town from /flood-guidance-zones carries the same flood-calibrated
// 1h/3h/24h thresholds and the same live rainfall as its zone on /ffgs,
// so the two pages always agree about a town. Status is the worst of
// the three windows (the /ffgs rule); headroom is how much more rain the
// tightest window can take before CRITICAL.
// (guidanceRows / guidanceLoading are declared earlier, alongside the
// other dynamic-text state — see the comment there.)

const GUIDANCE_WINDOWS = ['1h', '3h', '24h'];
const GUIDANCE_RANK = { SAFE: 0, WATCH: 1, CRITICAL: 2 };

function computeGuidanceLevel(zoneOrPoint, rain) {
    if (!zoneOrPoint || !zoneOrPoint.thresholds_mm || !rain) return null;
    let worst = null;
    GUIDANCE_WINDOWS.forEach(function(w) {
        const th = zoneOrPoint.thresholds_mm[w];
        const mm = rain[w];
        if (!th || mm == null) return;
        const level = mm >= th.critical ? 'CRITICAL' : (mm >= th.watch ? 'WATCH' : 'SAFE');
        if (worst === null || GUIDANCE_RANK[level] > GUIDANCE_RANK[worst]) worst = level;
    });
    return worst;
}

// { mm, window } for the window closest to its critical level.
function guidanceHeadroom(zoneOrPoint, rain) {
    if (!zoneOrPoint || !zoneOrPoint.thresholds_mm || !rain) return null;
    let best = null;
    GUIDANCE_WINDOWS.forEach(function(w) {
        const th = zoneOrPoint.thresholds_mm[w];
        const mm = rain[w];
        if (!th || mm == null) return;
        const left = Math.max(th.critical - mm, 0);
        if (best === null || left / th.critical < best.share) {
            best = { mm: left, window: w, share: left / th.critical };
        }
    });
    return best;
}

function guidanceRainText(rain) {
    if (!rain || rain['1h'] == null) return '—';
    return rain['1h'].toFixed(1) + ' / ' + (rain['24h'] == null ? '—' : rain['24h'].toFixed(1)) + ' mm';
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
        const rainText = guidanceRainText(rainMm);
        const level = computeGuidanceLevel(zone, rainMm);
        const headroom = guidanceHeadroom(zone, rainMm);
        const headroomText = headroom == null ? '—' : headroom.mm.toFixed(0) + ' mm (' + headroom.window + ')';
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
    let data;

    try {
        data = await (await fetch('/flood-guidance-zones')).json();
    } catch (error) {
        guidanceLoading = false;
        guidanceRows = [];
        renderGuidanceTable();
        return;
    }

    // Same browser fallback as /ffgs when the server has no rainfall.
    if (window.FloodSafeRainfall) {
        await window.FloodSafeRainfall.fillZones(data);
    }

    const zones = (data && Array.isArray(data.zones)) ? data.zones : [];

    guidanceLoading = false;
    guidanceRows = zones.map(function(zone) {
        return { zone: zone, rainMm: zone.live_rainfall || null };
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
    const rainText = guidanceRainText(rainMm);
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
                fetch('https://api.open-meteo.com/v1/forecast?latitude=' + lat + '&longitude=' + lon + '&current=precipitation&hourly=precipitation&past_days=2&forecast_days=1&timezone=auto').then(function(r) { return r.json(); })
            ]);
            const point = results[0];
            const weatherPayload = results[1];

            // 1h/3h/24h totals, parsed exactly as /ffgs and the server do.
            const rainMm = (weatherPayload && weatherPayload.hourly && window.FloodSafeRainfall)
                ? window.FloodSafeRainfall.parseDurations(weatherPayload) : null;
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
LANDING_PAGE_HTML = _with_app_head(LANDING_PAGE_HTML)


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
        "node_count": int(len(coordinates)) if ROUTING_ENGINE_AVAILABLE else None,
        # Which header rate limiting identifies callers by, so a deploy
        # can be checked for trusting the right one. The address itself
        # is not echoed.
        "client_ip_source": _client_ip_and_source()[1],
        # "file" on Render means reports are lost on every restart.
        "report_storage": "postgres" if DATABASE_URL else "file",
        "report_storage_error": _report_storage_state["last_error"],
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
REPORTS_VIEW_HTML = _with_app_head(REPORTS_VIEW_HTML)


@app.route("/reports-view")
def reports_view():

    return REPORTS_VIEW_HTML


@app.route("/report", methods=["POST"])
@_holding_reports_lock
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

    # A report only matters where it can block a road. Points far from
    # the road network -- anywhere else on Earth, or deep in the high
    # Himalaya -- block nothing and would only clutter everyone's map.
    if ROUTING_ENGINE_AVAILABLE and road_snap_km(lat, lon) > MAX_SNAP_KM:

        return jsonify({
            "status": "error",
            "error": (
                "Reports must be on or near a road FloodSafe covers "
                "(within 5 km of Uttarakhand's road network)."
            )
        }), 422

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
    _persist_report(report)

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
@_holding_reports_lock
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

    _persist_removals([report_id])

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
@_holding_reports_lock
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

    client_ip = _client_ip()
    already_confirmed_ips = _confirmed_ips_by_report.setdefault(report_id, set())

    already_confirmed = client_ip in already_confirmed_ips

    if not already_confirmed:

        already_confirmed_ips.add(client_ip)
        report["confirmations"] = int(report.get("confirmations", 0)) + 1
        _persist_report(report)

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
    # "server" (fetched here) or "relay" (see /rainfall/relay).
    "source": None,
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


# Query parameters for the town reading. Shared with the rainfall relay
# (see /rainfall/relay-points) so both ask Open-Meteo the same question.
TOWN_RAINFALL_QUERY = {"current": "precipitation", "timezone": "auto"}


def _town_readings_from_locations(per_location):
    fresh = {}

    for town, loc in zip(GUIDANCE_TOWNS, per_location):
        current = (loc or {}).get("current") or {}
        fresh[town["name"]] = round(float(current.get("precipitation") or 0.0), 2)

    return fresh


def _store_town_readings(fresh, now, source):
    cache = _town_rainfall_cache
    cache["timestamp"] = now
    cache["data"] = fresh
    cache["last_error"] = None
    cache["source"] = source
    print(f"Town rainfall refreshed from {source}: {len(fresh)} towns", flush=True)


@_single_flight(lambda: _town_rainfall_cache["data"])
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
                **TOWN_RAINFALL_QUERY,
            },

            timeout=20
        )

        response.raise_for_status()
        payload = response.json()

        per_location = payload if isinstance(payload, list) else [payload]

        fresh = _town_readings_from_locations(per_location)
        _store_town_readings(fresh, now, "server")

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
        "source": cache.get("source"),
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

        // Null, not a short total, when the feed doesn't reach back far
        // enough -- two days of rain must never read as a 7-day total.
        function sumLast(n) {
            if (idx < 0 || idx - n + 1 < 0) return null;
            var total = 0;
            for (var i = idx - n + 1; i <= idx; i++) total += Number(precip[i]) || 0;
            return Math.round(total * 100) / 100;
        }

        return {
            "1h": sumLast(1), "3h": sumLast(3), "24h": sumLast(24), antecedent_48h: sumLast(48),
            "72h": sumLast(72), "168h": sumLast(168)
        };
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

            // The server's own history length: 7 days with the landslide
            // layer on, 2 without.
            var pastDays = rainfall.past_days || 2;
            return once("zones:" + pastDays + JSON.stringify(rainfall.cells), function () {
                return forecast(rainfall.cells,
                    "&current=precipitation&hourly=precipitation&past_days=" + pastDays + "&forecast_days=1"
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

    # The same cached zone rainfall /ffgs/zones serves, so the landing
    # panel and /ffgs always agree about a town; and the same "rainfall"
    # block, so the page's browser fallback (/rainfall-fallback.js) can
    # fill it when the server has none.
    rainfall_by_point = _fetch_ffgs_live_rainfall()
    cache = _ffgs_rainfall_cache

    zones = [
        dict(zone,
             live_rainfall=rainfall_by_point.get((zone["lat"], zone["lon"])),
             rain_cell=FFGS_RAIN_CELL_BY_POINT.get((zone["lat"], zone["lon"])))
        for zone in GUIDANCE_ZONES
    ]

    return jsonify({
        "available": GUIDANCE_AVAILABLE,
        "error": None if GUIDANCE_AVAILABLE else GUIDANCE_ERROR,
        "durations": list(FFGS_DURATIONS),
        "rainfall": {
            "zones_with_data": sum(1 for z in zones if z.get("live_rainfall")),
            "last_error": cache["last_error"],
            "source": cache.get("source"),
            "cells": [[z[0]["lat"], z[0]["lon"]] for z in FFGS_RAINFALL_CELLS],
        },
        "zones": zones,
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

#ffgsLandslideAlert {
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
#zoneDetail .zd-landslide h4 { margin: 14px 0 6px; font-size: 14px; }
#zoneDetail .zd-note, .mylocation-body .zd-note { color: var(--faint); font-size: 12px; margin: 6px 0 0; }
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

.zone-cards-note { margin: 0; padding: 12px 18px 0; font-size: 12.5px; color: var(--muted); }

.zone-cards {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
    gap: 12px;
    padding: 14px 18px 18px;
}

.zone-card {
    display: flex;
    flex-direction: column;
    gap: 6px;
    text-align: left;
    font: inherit;
    color: inherit;
    background: var(--panel);
    border: 1px solid var(--border);
    border-left-width: 4px;
    border-radius: 8px;
    padding: 12px 14px;
    cursor: pointer;
}
.zone-card:hover, .zone-card:focus-visible { border-color: var(--muted); }
.zone-card.ffgs-card-critical { border-left-color: var(--risk); }
.zone-card.ffgs-card-watch { border-left-color: var(--watch); }
.zone-card.ffgs-card-safe { border-left-color: var(--safe); }
.zone-card.ffgs-card-unmapped { border-left-color: var(--faint); }
.zone-card-name { font-weight: 700; font-size: 14.5px; line-height: 1.3; }
.zone-card-meta { font-size: 12.5px; color: var(--muted); }
.zone-card-rain { font-size: 12.5px; color: var(--ink); font-variant-numeric: tabular-nums; }

.official-source { float: right; font-weight: normal; font-size: 12px; color: var(--faint); }
.official-note { margin: 0; padding: 10px 18px 0; font-size: 12.5px; color: var(--muted); }
.official-list { padding: 10px 18px 16px; }
.official-empty { font-size: 13px; color: var(--muted); }
.official-alert {
    border: 1px solid var(--border);
    border-left: 4px solid var(--watch);
    border-radius: 6px;
    padding: 10px 12px;
    margin-top: 8px;
}
.official-alert.sev-extreme, .official-alert.sev-severe { border-left-color: var(--risk); }
.official-alert.sev-minor { border-left-color: var(--faint); }
.official-alert-top { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: baseline; font-size: 12.5px; color: var(--muted); }
.official-alert-top b { color: var(--ink); font-size: 14px; }
.official-alert p { margin: 6px 0 0; font-size: 13.5px; line-height: 1.45; }
.official-alert a { font-size: 12.5px; }
.zone-official { margin-top: 12px; }

.zone-alerts { margin-top: 12px; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.za-btn {
    font: inherit;
    font-size: 13px;
    font-weight: 600;
    padding: 7px 12px;
    border-radius: 6px;
    border: 1px solid var(--navy);
    background: var(--navy);
    color: #fff;
    cursor: pointer;
}
.za-btn.za-secondary { background: transparent; color: var(--navy); }
.za-btn:disabled { opacity: 0.6; cursor: wait; }
.za-note { flex-basis: 100%; margin: 0; font-size: 12.5px; color: var(--muted); }
.za-message { color: var(--ink); }

.other-zones { border-top: 1px solid var(--border); }
.other-zones > summary {
    cursor: pointer;
    padding: 12px 18px;
    font-weight: 600;
    font-size: 13.5px;
    color: var(--muted);
}
.other-zones[open] > summary { border-bottom: 1px solid var(--border); }

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
    <div id="ffgsLandslideAlert" class="ffgs-alert-extra"></div>

    <div class="panel">
        <h2><span data-i18n="officialHeading">Official warnings for Uttarakhand</span>
            <span class="official-source" data-i18n="officialSource">NDMA SACHET</span></h2>
        <p class="official-note" data-i18n="officialNote">Issued by IMD, CWC and the Uttarakhand State Disaster Management Authority, shown as published. Separate from FloodSafe's own estimates below.</p>
        <div id="officialWarnings" class="official-list"></div>
    </div>

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
            <span id="pastLandslidesLegend" hidden><span class="dot" style="background:#8b5a2b"></span><span data-i18n="pastLandslidesLegend">Past rain-triggered landslide (NASA)</span></span>
            <span style="margin-left:auto;" data-i18n="markerNote">Marker color = current worst status across all three windows</span>
        </div>
        </div>
    </div>

    <div class="panel">
        <h2><span data-i18n="allZonesHeading">Monitored zones</span> <span id="ffgsUpdated" style="font-weight:normal; color:var(--faint); font-size:12px;"></span></h2>
        <p id="zoneCardsNote" class="zone-cards-note" hidden></p>
        <div id="zoneCards" class="zone-cards"></div>
        <details id="ffgsOtherZones" class="other-zones">
        <summary id="ffgsOtherZonesSummary"></summary>
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
        </details>
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
// Declared up here, not beside the official-warnings code: applyLanguage
// runs during startup and re-renders that panel, and reading a `let`
// before its line has executed throws and stops the whole script.
let officialData = null;
let officialFailed = false;
let ffgsDurations = ["1h", "3h", "24h"];
// From /ffgs/zones; {available: false} unless the server has landslide
// thresholds (see LANDSLIDE THRESHOLDS in server.py).
let landslideInfo = { available: false };
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
    noticeHtml: "This page compares live rainfall over three windows (1h / 3h / 24h) with each zone's thresholds to show whether it is SAFE, in WATCH, or CRITICAL right now. The thresholds are <b>calibrated against real floods</b>: 24 monsoons (2000-2023) of IMD-recorded flood events from the India Flood Inventory, matched with hourly ERA5 rainfall across the state, judging each place by how rare the rain is <i>for that place</i>. Tested on years it had not seen, CRITICAL caught __CRIT_POD__ of recorded floods while firing on about __CRIT_POFD__ of dry monsoon days per district, and WATCH caught __WATCH_POD__ at about __WATCH_POFD__. So many floods still come with no CRITICAL: rainfall data at ~25 km cannot see every cloudburst. Read SAFE as “no alert”, not “no risk”. This is <b>not</b> an official CWC/IMD warning; official warnings appear in their own panel above. Each zone's hazard class and <b>Flash Flood Potential Index</b> (FFPI) describe how susceptible its terrain is, from the state hazard atlas (about 9.7% of Uttarakhand) or, outside it, from real terrain, soil and land cover; they are context, and a surveyed atlas class takes precedence over FFPI.",
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
    allZonesHeading: "Monitored zones",
    otherZonesSummary: "Show the other {n} zones",
    zoneCardsFallbackNote: "Fewer zones are at Critical or Watch than there are cards, so the remaining cards show the zones closest to their thresholds, with their real status.",
    zoneCardTitle: "Show on the map",
    officialHeading: "Official warnings for Uttarakhand",
    officialSource: "NDMA SACHET",
    officialNote: "Issued by IMD, CWC and the Uttarakhand State Disaster Management Authority, shown as published. Separate from FloodSafe's own estimates below.",
    officialNone: "No current official warnings for Uttarakhand.",
    officialChecked: "Checked",
    officialError: "Couldn't load official warnings right now.",
    officialUntil: "Valid until",
    officialArea: "Area",
    officialStatewide: "All of Uttarakhand",
    officialLink: "Original alert (CAP)",
    officialForDistrict: "Official warning for {district} district",
    officialSeverity_Extreme: "Extreme",
    officialSeverity_Severe: "Severe",
    officialSeverity_Moderate: "Moderate",
    officialSeverity_Minor: "Minor",
    pushOn: "Alert me when this zone turns Critical",
    pushOff: "Stop alerts for this zone",
    pushTest: "Send a test notification",
    pushOnNote: "You'll get a notification if this zone reaches Critical, even with this page closed.",
    pushNoRainNote: "The server has no live rainfall right now, so alerts can't fire until it does.",
    pushUnsupported: "This browser doesn't support push notifications.",
    pushInstallIos: "On iPhone and iPad, alerts work only from the home screen: tap Share, then “Add to Home Screen”, and open FloodSafe from its icon. Needs iOS 16.4 or later.",
    pushDenied: "Notifications are blocked for this site. Allow them in your browser settings.",
    pushTestSent: "Test sent. It should appear in a few seconds.",
    pushError: "Couldn't update alerts: ",
    pushServiceError: "Your browser couldn't register with its push service. In Brave, turn on 'Use Google services for push messaging' (Settings > Privacy and security), then reload. Otherwise, a VPN, firewall or blocker may be stopping it; Chrome, Edge and Firefox work by default.",
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
    townAlmora: "Almora", townPithoragarh: "Pithoragarh", townJoshimath: "Joshimath",
    landslideHeading: "Landslide risk from rain",
    landslideLabel: "Landslide",
    landslideWin24h: "1 day", landslideWin72h: "3 days", landslideWin168h: "7 days",
    landslideNote: "Rain over 1, 3 and 7 days compared with this area’s usual monsoon rain. In testing on past monsoons, Critical caught {pod} of recorded rain-triggered landslides. Rain is only one cause: slope, road cutting and earthquakes are not included.",
    landslideNoRain: "Needs 7 days of rainfall, which isn’t available right now.",
    landslideAlertCritical: "Landslide risk from rain is Critical for: ",
    landslideAlertWatch: "Landslide risk from rain is at Watch for: ",
    pastLandslide: "Recorded landslide",
    pastLandslideDeaths: "deaths",
    pastLandslidesLegend: "Past rain-triggered landslide (NASA)"
  },
  hi: {
    textSizeLabel: "टेक्स्ट आकार:",
    pageTitle: "फ्लैश फ्लड गाइडेंस सिस्टम",
    pageSubtitle: "उत्तराखंड के लिए अवधि-आधारित वर्षा मार्गदर्शन",
    navMapTool: "मानचित्र टूल →",
    navBackDashboard: "← डैशबोर्ड पर वापस जाएं",
    noticeHtml: "यह पृष्ठ तीन अवधियों (1 घंटा / 3 घंटे / 24 घंटे) की लाइव वर्षा की तुलना हर क्षेत्र की सीमाओं से करके बताता है कि वह अभी सुरक्षित, सतर्क या गंभीर स्थिति में है। ये सीमाएँ <b>असली बाढ़ों पर कैलिब्रेट</b> की गई हैं: India Flood Inventory में IMD द्वारा दर्ज 24 मानसूनों (2000-2023) की बाढ़ की घटनाएँ, पूरे राज्य की घंटेवार ERA5 वर्षा के साथ, और हर जगह को इस आधार पर आँका गया है कि वहाँ के लिए वह वर्षा कितनी असामान्य है। जिन वर्षों को मॉडल ने नहीं देखा था, उन पर परखने पर गंभीर स्तर ने दर्ज बाढ़ों में से __CRIT_POD__ पकड़ीं और हर ज़िले में मानसून के लगभग __CRIT_POFD__ सूखे दिनों पर चेतावनी दी; सतर्क स्तर ने __WATCH_POD__ पकड़ीं, लगभग __WATCH_POFD__ दिनों पर। इसलिए कई बाढ़ें बिना गंभीर चेतावनी के भी आती हैं: ~25 किमी के वर्षा डेटा में हर बादल फटना दिखाई नहीं देता। सुरक्षित का अर्थ “कोई चेतावनी नहीं” है, “कोई खतरा नहीं” नहीं। यह CWC/IMD की आधिकारिक चेतावनी <b>नहीं</b> है; आधिकारिक चेतावनियाँ ऊपर अपने अलग पैनल में दिखती हैं। हर क्षेत्र का खतरा वर्ग और <b>Flash Flood Potential Index</b> (FFPI) बताते हैं कि उसका भूभाग कितना संवेदनशील है — राज्य खतरा एटलस (उत्तराखंड का लगभग 9.7%) से, या उसके बाहर असली भू-आकृति, मिट्टी और भूमि आवरण से; ये संदर्भ हैं, और सर्वेक्षित एटलस वर्ग FFPI से ऊपर रहता है।",
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
    allZonesHeading: "निगरानी क्षेत्र",
    otherZonesSummary: "अन्य {n} क्षेत्र दिखाएँ",
    zoneCardsFallbackNote: "कार्डों की तुलना में कम क्षेत्र गंभीर या सतर्क स्थिति में हैं, इसलिए शेष कार्ड अपनी सीमा के सबसे निकट के क्षेत्र उनकी वास्तविक स्थिति के साथ दिखाते हैं।",
    zoneCardTitle: "मानचित्र पर दिखाएँ",
    officialHeading: "उत्तराखंड के लिए आधिकारिक चेतावनियाँ",
    officialSource: "NDMA SACHET",
    officialNote: "IMD, CWC और उत्तराखंड राज्य आपदा प्रबंधन प्राधिकरण द्वारा जारी, जैसी प्रकाशित हुईं। नीचे दिए FloodSafe के अपने अनुमानों से अलग।",
    officialNone: "उत्तराखंड के लिए अभी कोई आधिकारिक चेतावनी नहीं है।",
    officialChecked: "जाँचा गया",
    officialError: "आधिकारिक चेतावनियाँ अभी लोड नहीं हो सकीं।",
    officialUntil: "मान्य",
    officialArea: "क्षेत्र",
    officialStatewide: "पूरा उत्तराखंड",
    officialLink: "मूल चेतावनी (CAP)",
    officialForDistrict: "{district} ज़िले के लिए आधिकारिक चेतावनी",
    officialSeverity_Extreme: "अत्यधिक",
    officialSeverity_Severe: "गंभीर",
    officialSeverity_Moderate: "मध्यम",
    officialSeverity_Minor: "मामूली",
    pushOn: "यह क्षेत्र गंभीर होने पर मुझे सूचित करें",
    pushOff: "इस क्षेत्र की सूचनाएँ बंद करें",
    pushTest: "परीक्षण सूचना भेजें",
    pushOnNote: "यह क्षेत्र गंभीर स्तर पर पहुँचने पर आपको सूचना मिलेगी, भले ही यह पेज बंद हो।",
    pushNoRainNote: "सर्वर के पास अभी लाइव वर्षा डेटा नहीं है, इसलिए डेटा मिलने तक सूचनाएँ नहीं भेजी जा सकतीं।",
    pushUnsupported: "यह ब्राउज़र पुश सूचनाओं का समर्थन नहीं करता।",
    pushInstallIos: "iPhone और iPad पर सूचनाएँ केवल होम स्क्रीन से काम करती हैं: Share पर टैप करें, फिर “Add to Home Screen” चुनें, और FloodSafe को उसके आइकन से खोलें। iOS 16.4 या नया आवश्यक है।",
    pushDenied: "इस साइट के लिए सूचनाएँ अवरुद्ध हैं। ब्राउज़र सेटिंग्स में इन्हें अनुमति दें।",
    pushTestSent: "परीक्षण भेजा गया। कुछ सेकंड में दिखना चाहिए।",
    pushError: "सूचनाएँ अपडेट नहीं हो सकीं: ",
    pushServiceError: "आपका ब्राउज़र अपनी पुश सेवा से पंजीकरण नहीं कर सका। Brave में 'Use Google services for push messaging' (Settings > Privacy and security) चालू करें और पेज फिर से लोड करें। अन्यथा कोई VPN, फ़ायरवॉल या ब्लॉकर इसे रोक रहा हो सकता है; Chrome, Edge और Firefox डिफ़ॉल्ट रूप से काम करते हैं।",
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
    townAlmora: "अल्मोड़ा", townPithoragarh: "पिथौरागढ़", townJoshimath: "जोशीमठ",
    landslideHeading: "वर्षा से भूस्खलन जोखिम",
    landslideLabel: "भूस्खलन",
    landslideWin24h: "1 दिन", landslideWin72h: "3 दिन", landslideWin168h: "7 दिन",
    landslideNote: "1, 3 और 7 दिनों की वर्षा की तुलना इस क्षेत्र की सामान्य मानसूनी वर्षा से। पिछले मानसूनों पर परीक्षण में, गंभीर स्तर ने दर्ज वर्षा-जनित भूस्खलनों में से {pod} पकड़े। वर्षा केवल एक कारण है: ढलान, सड़क कटान और भूकंप शामिल नहीं हैं।",
    landslideNoRain: "इसके लिए 7 दिनों का वर्षा डेटा चाहिए, जो अभी उपलब्ध नहीं है।",
    landslideAlertCritical: "इन स्थानों पर वर्षा से भूस्खलन जोखिम गंभीर है: ",
    landslideAlertWatch: "इन स्थानों पर वर्षा से भूस्खलन जोखिम निगरानी स्तर पर है: ",
    pastLandslide: "दर्ज भूस्खलन",
    pastLandslideDeaths: "मौतें",
    pastLandslidesLegend: "पिछला वर्षा-जनित भूस्खलन (NASA)"
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
    if (typeof renderOfficialWarnings === "function") renderOfficialWarnings();
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

    loadPastLandslides();
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

// ---- Landslide layer ----------------------------------------------

// {perDuration, overall} for rain against landslide thresholds, or null
// when the layer is off or the place has none.
function landslideStatus(rain, thresholds) {
    if (!landslideInfo.available || !thresholds) return null;
    const perDuration = {};
    let overall = null;
    landslideInfo.durations.forEach(function(d) {
        const rainMm = rain ? rain[d] : null;
        const status = statusForDuration(rainMm, thresholds[d]);
        perDuration[d] = { rainMm: rainMm, status: status };
        overall = worseStatus(overall, status);
    });
    return { perDuration: perDuration, overall: overall, thresholds: thresholds };
}

function landslideSectionHtml(landslide) {
    if (!landslide) return "";
    const rows = landslideInfo.durations.map(function(d) {
        const info = landslide.perDuration[d];
        const rainText = info.rainMm != null ? info.rainMm.toFixed(1) + " mm" : "—";
        const critical = landslide.thresholds[d] ? landslide.thresholds[d].critical.toFixed(0) + " mm" : "—";
        const badge = "ffgs-badge ffgs-" + (info.status || "unmapped").toLowerCase();
        return "<tr><td>" + t("landslideWin" + d) + "</td><td>" + rainText + "</td><td>" + critical +
            '</td><td><span class="' + badge + '">' + statusLabel(info.status) + "</span></td></tr>";
    }).join("");
    const pod = landslideInfo.critical_pod != null ? Math.round(landslideInfo.critical_pod * 100) + "%" : "—";
    return '<div class="zd-landslide"><h4>' + t("landslideHeading") + "</h4>" +
        '<table class="popup-table"><thead><tr><th>' + t("popupWindow") + "</th><th>" +
        t("popupRain") + "</th><th>" + t("popupCriticalAt") + "</th><th>" +
        t("popupStatus") + "</th></tr></thead><tbody>" + rows + "</tbody></table>" +
        (landslide.overall ? "" : '<p class="zd-note">' + t("landslideNoRain") + "</p>") +
        '<p class="zd-note">' + t("landslideNote").replace("{pod}", pod) + "</p></div>";
}

function landslideLineHtml(landslide) {
    if (!landslide) return "";
    const status = (landslide.overall || "unmapped").toLowerCase();
    return t("landslideLabel") + ': <span class="ffgs-badge ffgs-' + status + '">' +
        statusLabel(landslide.overall) + "</span>";
}

function loadPastLandslides() {
    fetch("/ffgs/landslides.geojson")
        .then(function(r) { return r.json(); })
        .then(function(geojson) {
            if (!geojson.features || !geojson.features.length) return;
            L.geoJSON(geojson, {
                pointToLayer: function(feature, latlng) {
                    return L.circleMarker(latlng, {
                        radius: 4, color: "#6b4226", weight: 1, fillColor: "#8b5a2b", fillOpacity: 0.8
                    });
                },
                onEachFeature: function(feature, layer) {
                    const p = feature.properties || {};
                    layer.bindPopup("<b>" + t("pastLandslide") + "</b> · " + escapeAttr(p.date) +
                        (p.district ? " · " + escapeAttr(p.district) : "") +
                        (p.title ? "<br>" + escapeAttr(p.title) : "") +
                        (p.fatalities ? "<br>" + p.fatalities + " " + t("pastLandslideDeaths") : ""));
                }
            }).addTo(map);
            document.getElementById("pastLandslidesLegend").hidden = false;
        })
        .catch(function() {});
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
        "&current=precipitation&hourly=precipitation&past_days=" + (landslideInfo.available ? 7 : 2) +
        "&forecast_days=1&timezone=auto";

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

    // Landslide status gets its own line, never folded into the flood one.
    const slideEl = document.getElementById("ffgsLandslideAlert");
    const slideCritical = ffgsZones.filter(function(z) { return z.landslide && z.landslide.overall === "CRITICAL"; });
    const slideWatch = ffgsZones.filter(function(z) { return z.landslide && z.landslide.overall === "WATCH"; });

    if (slideCritical.length) {
        slideEl.style.display = "block";
        slideEl.className = "ffgs-alert-critical";
        slideEl.textContent = t("landslideAlertCritical") + slideCritical.map(alertZoneLabel).join(", ");
    } else if (slideWatch.length) {
        slideEl.style.display = "block";
        slideEl.className = "ffgs-alert-watch";
        slideEl.textContent = t("landslideAlertWatch") + slideWatch.map(alertZoneLabel).join(", ");
    } else {
        slideEl.style.display = "none";
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

// ============================================================
// ZONE CARDS
//
// Listing all 127 zones made the page scroll forever. Five cards give
// the overview -- two Critical, two Watch, one Safe -- and everything
// else sits in a collapsed table below.
//
// The cards never misreport a status. When fewer zones are Critical
// or Watch than there are slots (in dry weather, none are), those
// slots go to the zones closest to their thresholds, each showing its
// real badge, and a note above the cards says so.
// ============================================================

const ZONE_CARD_SLOTS = [
    { status: "CRITICAL", count: 2 },
    { status: "WATCH", count: 2 },
    { status: "SAFE", count: 1 }
];
const HAZARD_CLASS_RANK = { LOW: 0, MODERATE: 1, SIGNIFICANT: 2, EXTREME: 3 };

// How far rain has gone toward the critical threshold, in the worst of
// the three windows -- 1.0 means at critical.
function criticalFraction(z) {
    let worst = 0;
    ffgsDurations.forEach(function(d) {
        const info = z.perDuration[d];
        const th = z.thresholds_mm && z.thresholds_mm[d];
        if (info && info.rainMm != null && th && th.critical) {
            worst = Math.max(worst, info.rainMm / th.critical);
        }
    });
    return worst;
}

// Most at-risk first: status, then closeness to critical, then the
// zone's hazard class, then the recorded-disaster model's probability.
function compareRisk(a, b) {
    return ((FFGS_STATUS_ORDER[b.overall] || 0) - (FFGS_STATUS_ORDER[a.overall] || 0)) ||
        (criticalFraction(b) - criticalFraction(a)) ||
        ((HAZARD_CLASS_RANK[b.effective_class] || 0) - (HAZARD_CLASS_RANK[a.effective_class] || 0)) ||
        ((b.event_prob || 0) - (a.event_prob || 0));
}

// Neighbouring localities share one rainfall cell and usually one
// hazard polygon, so without this the cards were four villages of the
// same district with identical numbers. At most one card per town or
// district, unless there aren't enough distinct ones to fill the slots.
function zoneArea(z) {
    return z.parent_town || z.name;
}

function takeSpread(candidates, count, picked) {
    const taken = [];
    const areaUsed = function(z) {
        return picked.concat(taken).some(function(p) { return zoneArea(p) === zoneArea(z); });
    };
    candidates.forEach(function(z) {
        if (taken.length < count && picked.indexOf(z) === -1 && !areaUsed(z)) taken.push(z);
    });
    candidates.forEach(function(z) {
        if (taken.length < count && picked.indexOf(z) === -1 && taken.indexOf(z) === -1) taken.push(z);
    });
    return taken;
}

function pickZoneCards() {
    const byRisk = ffgsZones.slice().sort(compareRisk);
    const picked = [];
    let shortfall = 0;

    ZONE_CARD_SLOTS.forEach(function(slot) {
        let matches = byRisk.filter(function(z) { return z.overall === slot.status; });
        // The Safe card shows the safest zone, as the contrast to the rest.
        if (slot.status === "SAFE") matches = matches.reverse();
        const taken = takeSpread(matches, slot.count, picked);
        picked.push.apply(picked, taken);
        shortfall += slot.count - taken.length;
    });

    const fillers = takeSpread(byRisk, shortfall, picked);
    const cards = picked.concat(fillers).sort(compareRisk);

    return { cards: cards, usedFallback: fillers.length > 0 };
}

function zoneCardHtml(z, index) {
    const status = (z.overall || "unmapped").toLowerCase();
    const name = z.parent_town
        ? escapeAttr(z.name) + '<span class="parent-town"> — ' + escapeAttr(ffgsTownName(z.parent_town)) + "</span>"
        : escapeAttr(ffgsTownName(z.name));

    const rain = ffgsDurations.map(function(d) {
        const info = z.perDuration[d];
        const th = z.thresholds_mm && z.thresholds_mm[d];
        const mm = (info && info.rainMm != null) ? info.rainMm.toFixed(1) : "—";
        return d + " " + mm + (th ? " / " + th.critical.toFixed(0) : "") + " mm";
    }).join(" · ");

    return '<button type="button" class="zone-card ffgs-card-' + status + '" data-card="' + index +
        '" title="' + t("zoneCardTitle") + '">' +
        '<span class="zone-card-name">' + name + "</span>" +
        '<span><span class="ffgs-badge ffgs-' + status + '">' + statusLabel(z.overall) + "</span></span>" +
        '<span class="zone-card-meta">' + hazardCellText(z) + t("hazardZoneSuffix") + "</span>" +
        '<span class="zone-card-rain">' + t("popupRain") + " / " + t("popupCriticalAt") + ": " + rain + "</span>" +
        (z.landslide ? '<span class="zone-card-meta">' + landslideLineHtml(z.landslide) + "</span>" : "") +
        "</button>";
}

function renderZoneCards(cards, usedFallback) {
    const container = document.getElementById("zoneCards");
    container.innerHTML = cards.map(zoneCardHtml).join("");
    document.getElementById("zoneCardsNote").hidden = !usedFallback;
    document.getElementById("zoneCardsNote").textContent = usedFallback ? t("zoneCardsFallbackNote") : "";

    container.querySelectorAll(".zone-card").forEach(function(button) {
        button.addEventListener("click", function() {
            const z = cards[Number(button.getAttribute("data-card"))];
            if (!zoneIndex.length) buildZoneIndex();
            const entry = zoneIndex.filter(function(e) { return zoneKey(e.zone) === zoneKey(z); })[0];
            selectZone(entry);
            document.getElementById("zoneSearch").scrollIntoView({ behavior: "smooth", block: "start" });
        });
    });
}

function renderFfgsTable() {
    const tbody = document.getElementById("ffgsTableBody");
    const cardsEl = document.getElementById("zoneCards");
    const summaryEl = document.getElementById("ffgsOtherZonesSummary");

    if (ffgsLoadFailed || ffgsZones.length === 0) {
        const message = ffgsLoadFailed ? t("guidanceUnavailable") : t("noZones");
        tbody.innerHTML = '<tr><td colspan="10">' + message + "</td></tr>";
        cardsEl.textContent = message;
        document.getElementById("zoneCardsNote").hidden = true;
        summaryEl.textContent = "";
        return;
    }

    const picked = pickZoneCards();
    renderZoneCards(picked.cards, picked.usedFallback);

    const sorted = ffgsZones
        .filter(function(z) { return picked.cards.indexOf(z) === -1; })
        .sort(compareRisk);

    summaryEl.textContent = t("otherZonesSummary").replace("{n}", sorted.length);

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
        t("popupStatus") + "</th></tr></thead><tbody>" + rows + "</tbody></table>" +
        landslideSectionHtml(z.landslide) +
        '<div id="zoneOfficial" class="zone-official"></div>' +
        '<div id="zoneAlerts" class="zone-alerts"></div>';
    el.hidden = false;
    renderZoneOfficial(z);
    renderZoneAlerts();
}

// ============================================================
// OFFICIAL WARNINGS (NDMA SACHET)
//
// Shown exactly as issued, in their own panel and labelled with the
// issuing office -- never mixed into FloodSafe's heuristic status.
// ============================================================

function officialText(alert, field) {
    // Hindi text when the page is in Hindi and the alert has it.
    return (currentLang === "hi" && alert[field + "_hi"]) ? alert[field + "_hi"] : alert[field];
}

function formatIst(iso) {
    const date = new Date(iso);
    if (isNaN(date)) return iso || "";
    return date.toLocaleString(currentLang === "hi" ? "hi-IN" : "en-IN", {
        timeZone: "Asia/Kolkata", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"
    }) + " IST";
}

function officialAlertHtml(alert) {
    const severityKey = "officialSeverity_" + alert.severity;
    const severity = t(severityKey) !== severityKey ? t(severityKey) : alert.severity;
    const area = alert.statewide ? t("officialStatewide") : alert.area;
    const description = officialText(alert, "description");

    return '<div class="official-alert sev-' + escapeAttr((alert.severity || "").toLowerCase()) + '">' +
        '<div class="official-alert-top"><b>' + escapeAttr(alert.event) + "</b>" +
        '<span class="ffgs-badge ffgs-' + (alert.severity === "Extreme" || alert.severity === "Severe" ? "critical" : "watch") + '">' +
        escapeAttr(severity) + "</span>" +
        "<span>" + escapeAttr(alert.office || alert.sender) + "</span></div>" +
        "<p>" + escapeAttr(officialText(alert, "headline")) + "</p>" +
        (description ? "<p>" + escapeAttr(description) + "</p>" : "") +
        '<div class="official-alert-top" style="margin-top:6px">' +
        "<span>" + t("officialArea") + ": " + escapeAttr(area) + "</span>" +
        "<span>" + t("officialUntil") + ": " + escapeAttr(formatIst(alert.expires)) + "</span>" +
        '<a href="' + escapeAttr(alert.link) + '" target="_blank" rel="noopener">' + t("officialLink") + "</a>" +
        "</div></div>";
}

function renderOfficialWarnings() {
    const el = document.getElementById("officialWarnings");
    if (!el) return;

    if (!officialData) {
        el.innerHTML = '<p class="official-empty">' + (officialFailed ? t("officialError") : t("loading")) + "</p>";
        return;
    }

    const checked = officialData.checked_at
        ? " " + t("officialChecked") + " " + formatIst(new Date(officialData.checked_at * 1000).toISOString())
        : "";

    if (officialData.last_error && !officialData.alerts.length) {
        el.innerHTML = '<p class="official-empty">' + t("officialError") + checked + "</p>";
        return;
    }

    el.innerHTML = officialData.alerts.length
        ? officialData.alerts.map(officialAlertHtml).join("") +
          '<p class="official-empty" style="margin-top:8px">' + checked.trim() + "</p>"
        : '<p class="official-empty">' + t("officialNone") + checked + "</p>";
}

function renderZoneOfficial(z) {
    const el = document.getElementById("zoneOfficial");
    if (!el) return;

    const alerts = (officialData && z && z.district)
        ? officialData.alerts.filter(function(a) { return a.districts.indexOf(z.district) !== -1; })
        : [];

    el.innerHTML = alerts.length
        ? "<b>⚠ " + escapeAttr(t("officialForDistrict").replace("{district}", z.district)) + "</b>" +
          alerts.map(officialAlertHtml).join("")
        : "";
}

async function loadOfficialWarnings() {
    try {
        officialData = await (await fetch("/official-warnings")).json();
        officialFailed = false;
    } catch (error) {
        officialFailed = true;
    }
    renderOfficialWarnings();
    const selected = ffgsZones.filter(function(z) { return zoneKey(z) === selectedZoneKey; })[0];
    if (selected) renderZoneOfficial(selected);
}

// ============================================================
// PUSH ALERTS
//
// "Alert me" subscribes this browser (via /sw.js) to Web Push for the
// selected zone; the server sends the notification when fresh rainfall
// puts the zone at Critical -- see PUSH ALERTS in server.py.
// ============================================================

let pushConfig = null;
let pushSubscription = null;
let pushZones = [];
let pushMessage = "";

function pushSupported() {
    return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

// Safari on iOS only offers push to a site opened from its home-screen
// icon, so in a normal tab it looks like no push support at all.
// iPadOS reports itself as a Mac, hence the touch check.
function iosOutsideHomeScreen() {
    const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
        (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    const standalone = navigator.standalone === true ||
        window.matchMedia("(display-mode: standalone)").matches;
    return ios && !standalone;
}

function urlBase64ToUint8Array(base64) {
    const padded = (base64 + "===".slice((base64.length + 3) % 4)).replace(/-/g, "+").replace(/_/g, "/");
    const raw = atob(padded);
    return Uint8Array.from(raw, function(c) { return c.charCodeAt(0); });
}

async function postJson(url, body) {
    const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
    });
    const data = await response.json().catch(function() { return {}; });
    if (!response.ok) throw new Error(data.error || ("HTTP " + response.status));
    return data;
}

async function initPush() {
    if (!pushSupported()) return renderZoneAlerts();
    try {
        pushConfig = await (await fetch("/push/config")).json();
    } catch (error) {
        pushConfig = null;
        return;
    }
    if (!pushConfig.available) return renderZoneAlerts();

    try {
        const registration = await navigator.serviceWorker.register("/sw.js");
        pushSubscription = await registration.pushManager.getSubscription();
        if (pushSubscription) {
            const data = await postJson("/push/subscriptions", { endpoint: pushSubscription.endpoint });
            pushZones = data.zones || [];
        }
    } catch (error) {
        console.warn("Push setup failed:", error);
    }
    renderZoneAlerts();
}

async function ensurePushSubscription() {
    const permission = await Notification.requestPermission();
    if (permission !== "granted") throw new Error(t("pushDenied"));

    const registration = await navigator.serviceWorker.ready;
    let subscription = await registration.pushManager.getSubscription();
    if (!subscription) {
        subscription = await registration.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: urlBase64ToUint8Array(pushConfig.public_key)
        });
    }
    pushSubscription = subscription;
    return subscription;
}

async function pushAction(action) {
    const zone = selectedZoneKey;
    pushMessage = "";
    try {
        if (action === "on") {
            const subscription = await ensurePushSubscription();
            await postJson("/push/subscribe", { zone: zone, subscription: subscription.toJSON() });
            if (pushZones.indexOf(zone) === -1) pushZones.push(zone);
        } else if (action === "off") {
            await postJson("/push/unsubscribe", { zone: zone, endpoint: pushSubscription.endpoint });
            pushZones = pushZones.filter(function(k) { return k !== zone; });
        } else if (action === "test") {
            await postJson("/push/test", { zone: zone, endpoint: pushSubscription.endpoint });
            pushMessage = t("pushTestSent");
        }
    } catch (error) {
        // AbortError is the browser failing to reach its own push
        // service (Brave's default, or a blocked connection) -- its
        // message, "push service error", says nothing actionable.
        pushMessage = error.name === "AbortError"
            ? t("pushServiceError")
            : t("pushError") + error.message;
    }
    renderZoneAlerts();
}

function renderZoneAlerts() {
    const el = document.getElementById("zoneAlerts");
    if (!el || !selectedZoneKey) return;

    if (!pushSupported()) {
        el.innerHTML = '<p class="za-note">' + t(iosOutsideHomeScreen() ? "pushInstallIos" : "pushUnsupported") + "</p>";
        return;
    }
    if (!pushConfig) {
        el.innerHTML = "";
        return;
    }
    if (!pushConfig.available) {
        el.innerHTML = '<p class="za-note">' + escapeAttr(pushConfig.reason || "") + "</p>";
        return;
    }

    const on = pushZones.indexOf(selectedZoneKey) !== -1;
    let html = on
        ? '<button type="button" class="za-btn" data-push="off">🔕 ' + t("pushOff") + "</button>" +
          '<button type="button" class="za-btn za-secondary" data-push="test">' + t("pushTest") + "</button>" +
          '<p class="za-note">' + t("pushOnNote") + "</p>"
        : '<button type="button" class="za-btn" data-push="on">🔔 ' + t("pushOn") + "</button>";

    if (!pushConfig.rainfall_live) html += '<p class="za-note">' + t("pushNoRainNote") + "</p>";
    if (pushMessage) html += '<p class="za-note za-message">' + escapeAttr(pushMessage) + "</p>";

    el.innerHTML = html;
    el.querySelectorAll("[data-push]").forEach(function(button) {
        button.addEventListener("click", function() {
            button.disabled = true;
            pushAction(button.getAttribute("data-push"));
        });
    });
}

// A notification opens /ffgs?zone=<key>; select that zone once the
// zone list has loaded.
let zoneFromUrlHandled = false;

function openZoneFromUrl() {
    if (zoneFromUrlHandled) return;
    zoneFromUrlHandled = true;

    const key = new URLSearchParams(location.search).get("zone");
    if (!key) return;

    if (!zoneIndex.length) buildZoneIndex();
    const entry = zoneIndex.filter(function(e) { return zoneKey(e.zone) === key; })[0];
    if (entry) selectZone(entry);
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
            rows + "</tbody></table>" +
            (z.landslide ? "<div style='margin-top:6px;'>" + landslideLineHtml(z.landslide) + "</div>" : "")
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
    landslideInfo = data.landslide || { available: false };

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
            district: zone.district || null,
            perDuration: perDuration,
            overall: overall,
            landslide: landslideStatus(rain, zone.landslide_thresholds_mm)
        };
    });

    renderMarkers();
    renderFfgsTable();
    renderAlertBanner();
    // After renderMarkers, so markerByKey is populated before a
    // selection can try to open a popup.
    refreshZoneUi();
    openZoneFromUrl();
}

initZonePicker();
loadFfgsZones();
initPush();
loadOfficialWarnings();
// The server re-reads SACHET every ten minutes; polling faster only
// re-fetches the same cached list.
setInterval(loadOfficialWarnings, 10 * 60 * 1000);

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
                rows + "</tbody></table>" +
                landslideSectionHtml(landslideStatus(rain, point.landslide_thresholds_mm));
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
FFGS_PAGE_HTML = _with_app_head(FFGS_PAGE_HTML)


@app.route("/ffgs")
def ffgs_page():

    return _ffgs_page_html()


def _validation_percent(level, key):
    try:
        return f"{round(CALIBRATED_THRESHOLDS['validation'][level][key] * 100)}%"
    except (TypeError, KeyError):
        return "n/a"


_ffgs_page_cache = []


def _ffgs_page_html():
    """
    The methods note quotes the validation scores; they're filled in from
    the calibration file itself, so the page can never drift from what
    the thresholds actually achieved.
    """
    if not _ffgs_page_cache:
        html = FFGS_PAGE_HTML
        for placeholder, level, key in (
            ("__CRIT_POD__", "critical", "POD"), ("__CRIT_POFD__", "critical", "POFD"),
            ("__WATCH_POD__", "watch", "POD"), ("__WATCH_POFD__", "watch", "POFD"),
        ):
            html = html.replace(placeholder, _validation_percent(level, key))
        _ffgs_page_cache.append(html)
    return _ffgs_page_cache[0]


@app.route("/ffgs/hazard-atlas.geojson")
def ffgs_hazard_atlas():

    geojson_path = os.path.join(DATA_DIR, "uttarakhand_flash_flood_hazard_clean.geojson")

    if not os.path.exists(geojson_path):
        return jsonify({"type": "FeatureCollection", "features": []})

    return send_file(geojson_path, mimetype="application/geo+json")


@app.route("/ffgs/landslides.geojson")
def ffgs_landslides():

    # Past rain-triggered landslides (fetch_landslide_catalog.py), shown
    # for context whether or not the threshold layer is on.
    path = os.path.join(DATA_DIR, "landslides_uttarakhand.geojson")

    if not os.path.exists(path):
        return jsonify({"type": "FeatureCollection", "features": []})

    return send_file(path, mimetype="application/geo+json")


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
    # "server" (fetched here) or "relay" (see /rainfall/relay).
    "source": None,
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
        # None rather than a short total when the feed doesn't reach back
        # far enough: two days of rain must never read as a 7-day total.
        if idx < 0 or idx - n + 1 < 0:
            return None
        return round(sum(float(v or 0.0) for v in hourly_precip[idx - n + 1:idx + 1]), 2)

    return {
        "1h": sum_last(1),
        "3h": sum_last(3),
        "24h": sum_last(24),
        "antecedent_48h": sum_last(48),
        "72h": sum_last(72),
        "168h": sum_last(168),
    }


# Query parameters for the zone reading, shared with the rainfall relay
# and the browser fallback. Seven past days only when the landslide
# layer is on (its 168h window); Open-Meteo bills a location as one call
# up to two weeks of data, so this costs no extra quota.
FFGS_RAINFALL_QUERY = {
    "current": "precipitation",
    "hourly": "precipitation",
    "past_days": 7 if LANDSLIDE_THRESHOLDS else 2,
    "forecast_days": 1,
    "timezone": "auto",
}


def _ffgs_readings_from_locations(per_location):
    fresh = {}

    for (cell_zones, loc) in zip(FFGS_RAINFALL_CELLS, per_location):

        reading = _parse_open_meteo_durations(loc)

        for zone in cell_zones:
            fresh[(zone["lat"], zone["lon"])] = reading

    return fresh


def _store_ffgs_readings(fresh, now, source):
    cache = _ffgs_rainfall_cache
    cache["timestamp"] = now
    cache["data"] = fresh
    cache["last_error"] = None
    cache["last_attempt"] = now
    cache["source"] = source

    print(
        f"FFGS rainfall refreshed from {source}: {len(FFGS_RAIN_CELL_BY_POINT)} zones "
        f"served by {len(FFGS_RAINFALL_CELLS)} fetches",
        flush=True
    )

    # Fresh readings are the only moment a zone can newly turn
    # Critical, so this is where push alerts are checked.
    _schedule_push_alert_check(fresh)


@_single_flight(lambda: _ffgs_rainfall_cache["data"])
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
                **FFGS_RAINFALL_QUERY,
            },

            timeout=30
        )

        response.raise_for_status()
        payload = response.json()

        # Open-Meteo returns a bare object (not a list) for a single
        # location, and a list of one object per location otherwise.
        per_location = payload if isinstance(payload, list) else [payload]

        fresh = _ffgs_readings_from_locations(per_location)
        _store_ffgs_readings(fresh, now, "server")

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
            "source": cache.get("source"),
            # For the browser fallback (/rainfall-fallback.js): the
            # points to fetch when this server has no reading at all.
            "cells": [[zones[0]["lat"], zones[0]["lon"]] for zones in FFGS_RAINFALL_CELLS],
            "past_days": FFGS_RAINFALL_QUERY["past_days"],
        },
        "landslide": _landslide_summary(),
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
# RAINFALL RELAY
#
# Open-Meteo refuses this server most days: its free quota is per IP,
# and Render's outbound IP is shared with other customers who spend it
# first. The browser fallback fills the pages, but only while someone
# has one open -- and push alerts have to be decided here, with nobody
# watching.
#
# So a scheduled GitHub Actions job (.github/workflows/rainfall-relay.yml)
# asks Open-Meteo for exactly the points and parameters listed at
# /rainfall/relay-points, from GitHub's IPs, and posts the raw responses
# to /rainfall/relay. The data is the same Open-Meteo reading the server
# would have fetched; only the network path differs. The server parses
# it with the same functions it uses for its own fetches, and the post
# is authenticated with a shared secret so nobody else can feed the
# app rainfall figures.
# ============================================================

RAINFALL_RELAY_SECRET = os.environ.get("RAINFALL_RELAY_SECRET", "").strip()

# Above the wettest 48 hours ever recorded anywhere (~2,500 mm), so it
# only rejects malformed values, never real extremes.
RAINFALL_MAX_PLAUSIBLE_MM = 3000.0


@app.route("/rainfall/relay-points")
def rainfall_relay_points():

    now = time.time()

    def needed(cache, ttl):
        return not cache["data"] or (now - cache["timestamp"]) >= ttl

    return jsonify({
        "configured": bool(RAINFALL_RELAY_SECRET),
        "ffgs": {
            "needed": needed(_ffgs_rainfall_cache, FFGS_RAINFALL_CACHE_TTL_SECONDS),
            "points": [[zones[0]["lat"], zones[0]["lon"]] for zones in FFGS_RAINFALL_CELLS],
            "query": FFGS_RAINFALL_QUERY,
        },
        "towns": {
            "needed": needed(_town_rainfall_cache, TOWN_RAINFALL_CACHE_TTL_SECONDS),
            "points": [[t["lat"], t["lon"]] for t in GUIDANCE_TOWNS],
            "query": TOWN_RAINFALL_QUERY,
        },
    })


def _plausible_mm(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and 0.0 <= number <= RAINFALL_MAX_PLAUSIBLE_MM


# The zone query's days, plus a day's slack.
FFGS_RELAY_MAX_HOURS = 24 * (FFGS_RAINFALL_QUERY["past_days"] + FFGS_RAINFALL_QUERY["forecast_days"] + 1)


def _validated_relay_locations(locations, points, hourly):
    """
    The relayed Open-Meteo responses, checked to be one per requested
    point, in order, with sane precipitation values. Raises ValueError.
    """

    if not isinstance(locations, list) or len(locations) != len(points):
        raise ValueError(f"expected {len(points)} locations")

    for loc, (lat, lon) in zip(locations, points):

        if not isinstance(loc, dict):
            raise ValueError("each location must be an object")

        # Open-Meteo snaps to its own grid, so allow a cell's width; this
        # catches responses in the wrong order, not grid rounding.
        try:
            if abs(float(loc["latitude"]) - lat) > 0.2 or abs(float(loc["longitude"]) - lon) > 0.2:
                raise ValueError("location out of order")
        except (KeyError, TypeError):
            raise ValueError("location missing coordinates")

        current = loc.get("current") or {}
        if not _plausible_mm(current.get("precipitation", 0.0)):
            raise ValueError("implausible current precipitation")

        if hourly:
            values = (loc.get("hourly") or {}).get("precipitation")
            if not isinstance(values, list) or len(values) > FFGS_RELAY_MAX_HOURS:
                raise ValueError("missing or oversized hourly series")
            if not all(v is None or _plausible_mm(v) for v in values):
                raise ValueError("implausible hourly precipitation")

    return locations


@app.route("/rainfall/relay", methods=["POST"])
def rainfall_relay():

    if not RAINFALL_RELAY_SECRET:
        return jsonify({"status": "error", "error": "Rainfall relay is not configured."}), 503

    supplied = request.headers.get("Authorization", "")
    expected = f"Bearer {RAINFALL_RELAY_SECRET}"

    if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        return jsonify({"status": "error", "error": "Unauthorized."}), 401

    data = request.get_json(force=True, silent=True)

    if not isinstance(data, dict):
        return jsonify({"status": "error", "error": "Body must be a JSON object."}), 400

    now = time.time()
    accepted = []

    try:
        # Validate both before storing either, so a bad half never
        # leaves the other half half-applied.
        ffgs = towns = None

        if data.get("ffgs") is not None:
            ffgs = _validated_relay_locations(
                data["ffgs"], [[z[0]["lat"], z[0]["lon"]] for z in FFGS_RAINFALL_CELLS], hourly=True)

        if data.get("towns") is not None:
            towns = _validated_relay_locations(
                data["towns"], [[t["lat"], t["lon"]] for t in GUIDANCE_TOWNS], hourly=False)

    except ValueError as e:
        return jsonify({"status": "error", "error": f"Rejected relay data: {e}"}), 400

    if ffgs is not None:
        _store_ffgs_readings(_ffgs_readings_from_locations(ffgs), now, "relay")
        accepted.append("ffgs")

    if towns is not None:
        _store_town_readings(_town_readings_from_locations(towns), now, "relay")
        accepted.append("towns")

    return jsonify({"status": "ok", "accepted": accepted})


# ============================================================
# PUSH ALERTS
#
# A visitor picks a zone on /ffgs and asks to be alerted; when fresh
# rainfall puts that zone at CRITICAL, this server sends a Web Push
# notification that arrives even with the tab closed.
#
# Needs DATABASE_URL: subscriptions live in Postgres so they survive
# restarts, and so does the VAPID signing key, which is generated on
# first use rather than configured -- there is no key to set up.
#
# Sent with py_vapid + http_ece directly rather than pywebpush, which
# also imports aiohttp (+7 MB) on a server near Render's memory cap.
#
# The server posts to the endpoint a browser hands it, so endpoints are
# restricted to the browser vendors' push services; otherwise anyone
# could make this server send requests to arbitrary URLs.
#
# The notification says plainly that the thresholds are this app's
# heuristic, not an official IMD/CWC warning -- same convention as the
# thresholds everywhere else in the app.
# ============================================================

# The VAPID "sub" claim tells push services who is sending. It must be
# a mailto: or a bare https:// origin (py_vapid rejects paths); the
# site's own origin avoids publishing anyone's email address.
PUSH_CONTACT = os.environ.get("PUSH_CONTACT", "https://floodsafe-u207.onrender.com")

PUSH_ALLOWED_HOST_SUFFIXES = (
    "fcm.googleapis.com",             # Chrome, Edge, Android, Opera
    "push.services.mozilla.com",      # Firefox
    "push.apple.com",                 # Safari
    "notify.windows.com",             # legacy Edge
)

# One alert per subscriber per zone per window, however long the zone
# stays critical -- repeating every ten minutes would train people to
# ignore it.
PUSH_ALERT_COOLDOWN_SECONDS = 6 * 60 * 60

PUSH_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS push_vapid_key (
    id          integer PRIMARY KEY CHECK (id = 1),
    private_pem text NOT NULL,
    public_key  text NOT NULL
);
CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint      text NOT NULL,
    zone_key      text NOT NULL,
    subscription  jsonb NOT NULL,
    created_at    double precision NOT NULL,
    last_alert_at double precision,
    PRIMARY KEY (endpoint, zone_key)
);
"""

_push_state = {
    "vapid": None,
    "public_key": None,
    "last_error": None,
    "last_check": None,
    "alerts_sent": 0,
}
_push_lock = threading.Lock()


def _zone_key(zone):
    # Must match zoneKey() in the FFGS page's JavaScript.
    return f"{zone['lat']:.5f},{zone['lon']:.5f}"


FFGS_ZONE_BY_KEY = {_zone_key(z): z for z in FFGS_ZONES if z.get("effective_class")}


def _b64url_decode(text):
    text = str(text)
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _push_db(action, fn):
    try:
        with _db_connect() as conn:
            conn.execute(PUSH_TABLES_SQL)
            result = fn(conn)
        return result
    except Exception as e:
        _push_state["last_error"] = f"{action} failed: {type(e).__name__}"
        print(f"WARNING: push {action} failed:", repr(e), flush=True)
        return None


def _push_vapid():
    """The server's VAPID key, created once and kept in Postgres."""

    if _push_state["vapid"] is not None:
        return _push_state["vapid"]

    # /push/config runs on every FFGS page load; without this an
    # unreachable database would stall each one on the connect timeout.
    if time.time() - _push_state.get("vapid_failed_at", 0.0) < 60:
        return None

    from py_vapid import Vapid02
    from cryptography.hazmat.primitives import serialization

    def load_or_create(conn):
        row = conn.execute("SELECT private_pem, public_key FROM push_vapid_key WHERE id = 1").fetchone()
        if row:
            return row

        key = Vapid02()
        key.generate_keys()
        public_raw = key.public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        conn.execute(
            "INSERT INTO push_vapid_key (id, private_pem, public_key) VALUES (1, %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (key.private_pem().decode("ascii"),
             base64.urlsafe_b64encode(public_raw).decode("ascii").rstrip("=")))
        return conn.execute("SELECT private_pem, public_key FROM push_vapid_key WHERE id = 1").fetchone()

    row = _push_db("key setup", load_or_create)

    if row is None:
        _push_state["vapid_failed_at"] = time.time()
        return None

    _push_state["vapid"] = Vapid02.from_pem(row[0].encode("ascii"))
    _push_state["public_key"] = row[1]
    return _push_state["vapid"]


def _validate_subscription(sub):
    """None if sub is a usable Web Push subscription, else the reason."""

    if not isinstance(sub, dict):
        return "subscription must be an object"

    endpoint = sub.get("endpoint")
    if not isinstance(endpoint, str) or len(endpoint) > 1000:
        return "invalid endpoint"

    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").lower()

    if parsed.scheme != "https" or not any(
            host == suffix or host.endswith("." + suffix) for suffix in PUSH_ALLOWED_HOST_SUFFIXES):
        return "endpoint is not a recognised browser push service"

    keys = sub.get("keys") or {}

    try:
        p256dh = _b64url_decode(keys.get("p256dh", ""))
        auth = _b64url_decode(keys.get("auth", ""))
    except (ValueError, TypeError):
        return "invalid subscription keys"

    if len(p256dh) != 65 or p256dh[0] != 4 or len(auth) != 16:
        return "invalid subscription keys"

    return None


def _send_push(subscription, message, ttl=PUSH_ALERT_COOLDOWN_SECONDS):
    """Encrypt and deliver one notification. Returns the HTTP status."""

    import http_ece
    from cryptography.hazmat.primitives.asymmetric import ec

    vapid = _push_vapid()
    if vapid is None:
        raise RuntimeError("push signing key unavailable")

    body = http_ece.encrypt(
        json.dumps(message).encode("utf-8"),
        private_key=ec.generate_private_key(ec.SECP256R1()),
        dh=_b64url_decode(subscription["keys"]["p256dh"]),
        auth_secret=_b64url_decode(subscription["keys"]["auth"]),
        version="aes128gcm",
    )

    endpoint = subscription["endpoint"]
    parsed = urlparse(endpoint)

    headers = vapid.sign({
        "aud": f"{parsed.scheme}://{parsed.netloc}",
        "sub": PUSH_CONTACT,
        "exp": int(time.time()) + 12 * 60 * 60,
    })
    headers.update({
        "TTL": str(int(ttl)),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "Urgency": "high",
    })

    return requests.post(endpoint, data=body, headers=headers, timeout=10).status_code


def _zone_label(zone):
    if zone.get("parent_town"):
        return f"{zone['name']} ({zone['parent_town']})"
    return zone["name"]


def _critical_windows(zone, reading):
    """[(window, rain_mm, critical_mm)] for every window at or over critical."""

    thresholds = zone.get("thresholds_mm") or {}
    hits = []

    for window in FFGS_DURATIONS:
        rain = (reading or {}).get(window)
        th = thresholds.get(window)
        if rain is not None and th and rain >= th["critical"]:
            hits.append((window, rain, th["critical"]))

    return hits


def _critical_alert_message(zone, hits):
    window, rain, critical = max(hits, key=lambda h: h[1] / h[2])
    return {
        "title": f"Flash-flood CRITICAL: {_zone_label(zone)}",
        "body": (
            f"{rain:.1f} mm of rain in the last {window}, at or above this "
            f"{zone['effective_class']} hazard zone's critical level of {critical:.0f} mm. "
            "FloodSafe threshold calibrated on past floods, not an official IMD/CWC warning."
        ),
        "url": f"/ffgs?zone={_zone_key(zone)}",
        "tag": f"critical-{_zone_key(zone)}",
    }


def _schedule_push_alert_check(readings):
    """
    Called with every fresh set of zone readings. Returns at once when
    no zone is critical -- the normal case, costing no database call --
    otherwise sends alerts on a background thread so the request that
    brought the rainfall isn't held up by push delivery.
    """

    if not DATABASE_URL:
        return

    critical = {}

    for key, zone in FFGS_ZONE_BY_KEY.items():
        hits = _critical_windows(zone, readings.get((zone["lat"], zone["lon"])))
        if hits:
            critical[key] = _critical_alert_message(zone, hits)

    _push_state["last_check"] = time.time()

    if critical:
        threading.Thread(target=_send_critical_alerts, args=(critical,), daemon=True).start()


def _send_critical_alerts(messages_by_zone):

    # One delivery pass at a time; a second refresh landing mid-pass
    # would otherwise double-send before last_alert_at is written.
    with _push_lock:

        now = time.time()

        rows = _push_db("alert lookup", lambda conn: conn.execute(
            "SELECT endpoint, zone_key, subscription FROM push_subscriptions "
            "WHERE zone_key = ANY(%s) AND (last_alert_at IS NULL OR last_alert_at < %s)",
            (list(messages_by_zone), now - PUSH_ALERT_COOLDOWN_SECONDS)).fetchall()) or []

        delivered, gone = [], []

        for endpoint, zone_key, subscription in rows:
            try:
                status = _send_push(subscription, messages_by_zone[zone_key])
            except Exception as e:
                _push_state["last_error"] = f"delivery failed: {type(e).__name__}"
                print("WARNING: push delivery failed:", repr(e), flush=True)
                continue

            if status in (404, 410):
                # The browser unsubscribed or the subscription expired.
                gone.append((endpoint, zone_key))
            elif status < 300:
                delivered.append((endpoint, zone_key))
            else:
                _push_state["last_error"] = f"push service returned HTTP {status}"

        def record(conn):
            for endpoint, zone_key in delivered:
                conn.execute(
                    "UPDATE push_subscriptions SET last_alert_at = %s "
                    "WHERE endpoint = %s AND zone_key = %s", (now, endpoint, zone_key))
            for endpoint, zone_key in gone:
                conn.execute(
                    "DELETE FROM push_subscriptions WHERE endpoint = %s AND zone_key = %s",
                    (endpoint, zone_key))

        if delivered or gone:
            _push_db("alert bookkeeping", record)

        _push_state["alerts_sent"] += len(delivered)

        if delivered:
            print(f"Push: sent {len(delivered)} critical alert(s)", flush=True)


def _push_unavailable():
    if not DATABASE_URL:
        return "Push alerts need the report database (DATABASE_URL), which isn't configured."
    if _push_vapid() is None:
        return "Push alerts are temporarily unavailable."
    return None


@app.route("/push/config")
def push_config():

    reason = _push_unavailable()

    return jsonify({
        "available": reason is None,
        "reason": reason,
        "public_key": _push_state["public_key"] if reason is None else None,
        # Whether the server currently has rainfall to judge zones by;
        # without it no alert can fire, and the page says so.
        "rainfall_live": bool(_ffgs_rainfall_cache["data"]),
        "last_error": _push_state["last_error"],
    })


def _push_request(require_zone=True):
    """Shared parsing for the subscription endpoints: (data, error_response)."""

    if _rate_limited("push"):
        return None, _rate_limit_response("push")

    reason = _push_unavailable()
    if reason:
        return None, (jsonify({"status": "error", "error": reason}), 503)

    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return None, (jsonify({"status": "error", "error": "Body must be a JSON object."}), 400)

    if require_zone and data.get("zone") not in FFGS_ZONE_BY_KEY:
        return None, (jsonify({"status": "error", "error": "Unknown zone."}), 400)

    return data, None


@app.route("/push/subscribe", methods=["POST"])
def push_subscribe():

    data, error = _push_request()
    if error:
        return error

    subscription = data.get("subscription")
    problem = _validate_subscription(subscription)
    if problem:
        return jsonify({"status": "error", "error": problem}), 400

    from psycopg.types.json import Jsonb

    stored = _push_db("subscribe", lambda conn: conn.execute(
        "INSERT INTO push_subscriptions (endpoint, zone_key, subscription, created_at) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (endpoint, zone_key) DO UPDATE SET subscription = EXCLUDED.subscription",
        (subscription["endpoint"], data["zone"], Jsonb(subscription), time.time())))

    if stored is None:
        return jsonify({"status": "error", "error": "Could not save the subscription."}), 503

    return jsonify({"status": "ok", "zone": data["zone"]}), 201


@app.route("/push/unsubscribe", methods=["POST"])
def push_unsubscribe():

    data, error = _push_request()
    if error:
        return error

    _push_db("unsubscribe", lambda conn: conn.execute(
        "DELETE FROM push_subscriptions WHERE endpoint = %s AND zone_key = %s",
        (str(data.get("endpoint", "")), data["zone"])))

    return jsonify({"status": "ok"})


@app.route("/push/subscriptions", methods=["POST"])
def push_subscriptions():
    """Which zones this browser is subscribed to (endpoint in the body,
    not the URL, since it identifies the browser)."""

    data, error = _push_request(require_zone=False)
    if error:
        return error

    rows = _push_db("lookup", lambda conn: conn.execute(
        "SELECT zone_key FROM push_subscriptions WHERE endpoint = %s",
        (str(data.get("endpoint", "")),)).fetchall()) or []

    return jsonify({"zones": [row[0] for row in rows]})


@app.route("/push/test", methods=["POST"])
def push_test():
    """Send a clearly-labelled test notification to one existing
    subscription, so a subscriber can see alerts work without waiting
    for real critical rainfall."""

    data, error = _push_request()
    if error:
        return error

    row = _push_db("test lookup", lambda conn: conn.execute(
        "SELECT subscription FROM push_subscriptions WHERE endpoint = %s AND zone_key = %s",
        (str(data.get("endpoint", "")), data["zone"])).fetchone())

    if not row:
        return jsonify({"status": "error", "error": "Not subscribed to this zone."}), 404

    zone = FFGS_ZONE_BY_KEY[data["zone"]]

    try:
        status = _send_push(row[0], {
            "title": f"Test alert: {_zone_label(zone)}",
            "body": "FloodSafe push alerts are working. This is a test, not a flood warning.",
            "url": f"/ffgs?zone={data['zone']}",
            "tag": f"test-{data['zone']}",
        }, ttl=60)
    except Exception as e:
        return jsonify({"status": "error", "error": f"Delivery failed: {type(e).__name__}"}), 502

    if status >= 300:
        return jsonify({"status": "error", "error": f"Push service returned HTTP {status}."}), 502

    return jsonify({"status": "ok"})


SERVICE_WORKER_JS = r"""
// ---- Offline copies ------------------------------------------------
//
// Mountain connectivity drops out exactly when a flood is on, so the
// map page, its libraries, the shelter list and the map tiles someone
// has already looked at are kept for use without a connection.
//
// Pages and /shelters are network-first: online visitors always get
// the live version, and the copy is only a fallback. Nothing that
// changes minute to minute (rainfall, reports, zone status) is cached,
// because showing it stale offline would read as current. Routes are
// POSTs and never pass through here; the map page keeps its own last
// results instead.

var CACHE = "floodsafe-offline-v1";
var TILE_CACHE = "floodsafe-tiles-v1";
var MAX_TILES = 400;
var NETWORK_TIMEOUT_MS = 8000;
var CDN_HOSTS = ["cdn.jsdelivr.net", "cdnjs.cloudflare.com", "code.jquery.com", "netdna.bootstrapcdn.com"];
var OFFLINE_PATHS = ["/app", "/ffgs", "/", "/shelters"];

self.addEventListener("install", function (event) {
    // Small and shared by every page; the map page itself is cached on
    // request (see "cache-urls") so /ffgs visitors never download it.
    event.waitUntil(caches.open(CACHE).then(function (cache) {
        return cache.add("/shelters");
    }).catch(function () {}));
    self.skipWaiting();
});

self.addEventListener("activate", function (event) {
    event.waitUntil(caches.keys().then(function (keys) {
        return Promise.all(keys.filter(function (key) {
            return key.indexOf("floodsafe-") === 0 && key !== CACHE && key !== TILE_CACHE;
        }).map(function (key) { return caches.delete(key); }));
    }).then(function () { return self.clients.claim(); }));
});

// Cross-origin files are refetched with CORS (the CDNs and OSM allow
// it): an opaque no-cors copy would be charged ~7 MB of storage quota
// each in Chrome.
function corsRequest(url) {
    return new Request(url, { mode: "cors", credentials: "omit" });
}

function store(cacheName, request, response) {
    if (response && response.ok) {
        var copy = response.clone();
        caches.open(cacheName).then(function (cache) { cache.put(request, copy); });
    }
    return response;
}

function networkFirst(request) {
    var network = fetch(request).then(function (response) {
        return store(CACHE, request, response);
    });
    var timeout = new Promise(function (resolve, reject) {
        setTimeout(function () { reject(new Error("timeout")); }, NETWORK_TIMEOUT_MS);
    });
    return Promise.race([network, timeout]).catch(function () {
        return caches.match(request, { ignoreSearch: request.mode === "navigate" }).then(function (cached) {
            return cached || network;
        });
    });
}

function cacheFirst(cacheName, url, original) {
    return caches.match(url).then(function (cached) {
        if (cached) return cached;
        return fetch(corsRequest(url)).catch(function () {
            return fetch(original);
        }).then(function (response) {
            store(cacheName, url, response);
            if (cacheName === TILE_CACHE) trimTiles();
            return response;
        });
    });
}

function trimTiles() {
    caches.open(TILE_CACHE).then(function (cache) {
        return cache.keys().then(function (keys) {
            // keys() is in insertion order, so the oldest go first.
            return Promise.all(keys.slice(0, Math.max(0, keys.length - MAX_TILES)).map(function (key) {
                return cache.delete(key);
            }));
        });
    });
}

self.addEventListener("fetch", function (event) {
    var request = event.request;
    if (request.method !== "GET") return;

    var url = new URL(request.url);

    if (url.origin === self.location.origin) {
        if (request.mode === "navigate" || OFFLINE_PATHS.indexOf(url.pathname) !== -1) {
            event.respondWith(networkFirst(request));
        }
        return;
    }

    if (CDN_HOSTS.indexOf(url.hostname) !== -1) {
        event.respondWith(cacheFirst(CACHE, url.href, request));
    } else if (/(^|\.)tile\.openstreetmap\.org$/.test(url.hostname)) {
        event.respondWith(cacheFirst(TILE_CACHE, url.href, request));
    }
});

// The first visit loads before this worker controls the page, so the
// page lists what it loaded and asks for those to be kept.
self.addEventListener("message", function (event) {
    var data = event.data || {};
    if (data.type !== "cache-urls" || !Array.isArray(data.urls)) return;

    event.waitUntil(caches.open(CACHE).then(function (cache) {
        return Promise.all(data.urls.map(function (href) {
            var url;
            try { url = new URL(href, self.location.origin); } catch (e) { return null; }
            var sameOrigin = url.origin === self.location.origin;
            if (!sameOrigin && CDN_HOSTS.indexOf(url.hostname) === -1) return null;
            return cache.match(url.href).then(function (hit) {
                if (hit) return null;
                return fetch(sameOrigin ? url.href : corsRequest(url.href)).then(function (response) {
                    if (response.ok) return cache.put(url.href, response);
                }).catch(function () {});
            });
        }));
    }));
});

// ---- Push alerts ---------------------------------------------------

self.addEventListener("push", function (event) {
    var message = {};
    try { message = event.data ? event.data.json() : {}; } catch (e) {}

    event.waitUntil(self.registration.showNotification(message.title || "FloodSafe alert", {
        body: message.body || "",
        tag: message.tag,
        renotify: true,
        requireInteraction: true,
        data: { url: message.url || "/ffgs" }
    }));
});

self.addEventListener("notificationclick", function (event) {
    event.notification.close();
    var url = (event.notification.data && event.notification.data.url) || "/ffgs";
    event.waitUntil(clients.openWindow(url));
});
"""


@app.route("/sw.js")
def service_worker():

    # Served from the root so its scope covers every page, and never
    # cached, so a fix to it reaches browsers on their next visit.
    response = Response(SERVICE_WORKER_JS, mimetype="application/javascript")
    response.headers["Cache-Control"] = "no-cache"
    return response


# ============================================================
# OFFICIAL WARNINGS (NDMA SACHET)
#
# FloodSafe's own CRITICAL/WATCH status is a heuristic on live
# rainfall. The official warnings for the same places come from NDMA's
# SACHET system -- the Common Alerting Protocol (CAP) feed through which
# IMD, CWC and the state disaster authorities publish their alerts. The
# feed is marked public domain.
#
# The all-India RSS feed lists recent alerts; each links to a CAP
# document with the event, severity, validity window, affected area
# and English and Hindi text. Only items plausibly about Uttarakhand
# (by issuing office, or a district or the state named in the title)
# are opened, and an alert is kept when its sender or area names
# Uttarakhand or one of its districts. Area polygons are published too,
# but SACHET refuses automated requests for them (HTTP 403), so areas
# are matched by the district names in the area description instead.
#
# Alerts are shown as issued, clearly apart from FloodSafe's estimates;
# nothing here is combined with or reinterpreted by the app's model.
# ============================================================

NDMA_FEED_URL = "https://sachet.ndma.gov.in/cap_public_website/rss/rss_india.xml"
NDMA_CACHE_TTL_SECONDS = 10 * 60
NDMA_MAX_DOCUMENT_BYTES = 1_000_000
NDMA_MAX_CAP_FETCHES = 30
NDMA_HEADERS = {"User-Agent": "FloodSafe/1.0 (+https://floodsafe-u207.onrender.com)"}

# District names as they appear in alert text, English and Hindi. Bare
# "Garhwal" is left out: it names a seven-district division, not Pauri.
DISTRICT_ALIASES = {
    "Almora": ["almora", "अल्मोड़ा"],
    "Bageshwar": ["bageshwar", "बागेश्वर"],
    "Chamoli": ["chamoli", "चमोली"],
    "Champawat": ["champawat", "चंपावत", "चम्पावत"],
    "Dehradun": ["dehradun", "dehra dun", "देहरादून"],
    "Haridwar": ["haridwar", "hardwar", "हरिद्वार"],
    "Nainital": ["nainital", "नैनीताल"],
    "Pauri Garhwal": ["pauri", "पौड़ी"],
    "Pithoragarh": ["pithoragarh", "पिथौरागढ़"],
    "Rudraprayag": ["rudraprayag", "रुद्रप्रयाग"],
    "Tehri Garhwal": ["tehri", "टिहरी"],
    "Udham Singh Nagar": ["udham singh nagar", "udhamsingh nagar", "u.s. nagar", "ऊधम सिंह नगर", "उधम सिंह नगर"],
    "Uttarkashi": ["uttarkashi", "उत्तरकाशी"],
}
STATE_ALIASES = ["uttarakhand", "uttaranchal", "उत्तराखंड", "उत्तराखण्ड"]
UTTARAKHAND_OFFICES = ["dehradun", "uttarakhand"]

CAP_NS = {"cap": "urn:oasis:names:tc:emergency:cap:1.2"}

_ndma_state = {
    "alerts": [],
    "checked_at": None,
    "last_error": None,
    "last_attempt": 0.0,
}
_ndma_cap_cache = {}   # guid -> parsed CAP (documents never change)


def _districts_named_in(text):
    lowered = (text or "").lower()
    return sorted(district for district, aliases in DISTRICT_ALIASES.items()
                  if any(alias in lowered for alias in aliases))


def _names_uttarakhand(text):
    lowered = (text or "").lower()
    return any(alias in lowered for alias in STATE_ALIASES)


def _ndma_get(url):
    response = requests.get(url, headers=NDMA_HEADERS, timeout=20)
    response.raise_for_status()
    if len(response.content) > NDMA_MAX_DOCUMENT_BYTES:
        raise ValueError("document too large")
    return response.content


def _parse_cap(xml_bytes, link):
    """The fields FloodSafe shows, from one CAP 1.2 document, or None."""

    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml_bytes)

    def text(node, tag):
        found = node.find(f"cap:{tag}", CAP_NS)
        return (found.text or "").strip() if found is not None and found.text else ""

    infos = root.findall("cap:info", CAP_NS)
    if not infos:
        return None

    def pick(prefix):
        for info in infos:
            if text(info, "language").lower().startswith(prefix):
                return info
        return None

    english = pick("en") or infos[0]
    hindi = pick("hi")
    area_desc = "; ".join(
        text(area, "areaDesc") for area in english.findall("cap:area", CAP_NS) if text(area, "areaDesc"))

    return {
        "identifier": text(root, "identifier"),
        "sender": text(root, "sender"),
        "sent": text(root, "sent"),
        "status": text(root, "status"),
        "msg_type": text(root, "msgType"),
        "references": text(root, "references"),
        "event": text(english, "event"),
        "severity": text(english, "severity"),
        "urgency": text(english, "urgency"),
        "certainty": text(english, "certainty"),
        "effective": text(english, "effective"),
        "expires": text(english, "expires"),
        "headline": text(english, "headline"),
        "description": text(english, "description"),
        "instruction": text(english, "instruction"),
        "headline_hi": text(hindi, "headline") if hindi is not None else "",
        "description_hi": text(hindi, "description") if hindi is not None else "",
        "area": area_desc,
        "link": link,
    }


def _still_valid(expires, now):
    from datetime import datetime

    if not expires:
        return True
    try:
        return datetime.fromisoformat(expires).timestamp() > now
    except ValueError:
        return True


@_single_flight(lambda: None)
def _refresh_ndma_alerts():
    """Re-read the feed and rebuild the current Uttarakhand alert list."""

    import xml.etree.ElementTree as ET

    now = time.time()
    _ndma_state["last_attempt"] = now

    try:
        feed = ET.fromstring(_ndma_get(NDMA_FEED_URL))
    except Exception as e:
        _ndma_state["last_error"] = f"Could not read the NDMA SACHET feed: {type(e).__name__}"
        print("WARNING: NDMA feed fetch failed:", repr(e), flush=True)
        return

    candidates = []

    for item in feed.findall("./channel/item"):
        author_raw = item.findtext("author") or ""
        author = author_raw.lower()
        title = item.findtext("title") or ""
        guid = (item.findtext("guid") or "").strip()
        link = (item.findtext("link") or "").strip()

        if not guid or not link.startswith("https://sachet.ndma.gov.in/"):
            continue

        # CWC's river alerts come from one national office, so their
        # state only shows up in the CAP area; open all of them.
        if (any(office in author for office in UTTARAKHAND_OFFICES) or "(cwc)" in author
                or _names_uttarakhand(title) or _districts_named_in(title)):
            candidates.append((guid, link, author_raw))

    alerts = []
    fetches = 0

    for guid, link, author in candidates:

        cap = _ndma_cap_cache.get(guid)

        if cap is None:
            if fetches >= NDMA_MAX_CAP_FETCHES:
                continue
            fetches += 1
            try:
                cap = _parse_cap(_ndma_get(link), link)
            except Exception as e:
                print("WARNING: NDMA CAP document skipped:", link, repr(e), flush=True)
                continue
            if cap is None:
                continue
            # "controlroom@ndma.gov.in (IMD Dehradun)" -> "IMD Dehradun"
            cap["office"] = author.split("(")[-1].rstrip(")").strip() if "(" in author else ""
            _ndma_cap_cache[guid] = cap

        districts = _districts_named_in(cap["area"] + " " + cap["headline"])
        statewide = _names_uttarakhand(cap["area"]) and not districts
        from_uttarakhand = "uttarakhand" in cap["sender"].lower() or "dehradun" in cap["office"].lower()

        # An alert from Uttarakhand's own offices whose area names no
        # district we recognise (a tehsil, say) is kept as statewide:
        # showing an official warning too broadly beats dropping it.
        if not (districts or statewide or from_uttarakhand):
            continue

        alerts.append(dict(cap, districts=districts or sorted(DISTRICT_ALIASES), statewide=not districts))

    # Drop cancelled alerts, the ones they cancel, and anything a newer
    # update in the feed supersedes (CAP "references" lists them as
    # "sender,identifier,sent" triples).
    superseded = set()
    for alert in alerts:
        for triple in alert["references"].split():
            parts = triple.split(",")
            if len(parts) >= 2:
                superseded.add(parts[1])

    current = [
        alert for alert in alerts
        if alert["status"] == "Actual"
        and alert["msg_type"] != "Cancel"
        and alert["identifier"] not in superseded
        and _still_valid(alert["expires"], now)
    ]

    severity_rank = {"Extreme": 0, "Severe": 1, "Moderate": 2, "Minor": 3}
    current.sort(key=lambda a: (severity_rank.get(a["severity"], 4), a["expires"]))

    # Keep the document cache bounded to what the feed still lists.
    live_guids = {guid for guid, _, _ in candidates}
    for guid in list(_ndma_cap_cache):
        if guid not in live_guids:
            del _ndma_cap_cache[guid]

    _ndma_state["alerts"] = current
    _ndma_state["checked_at"] = now
    _ndma_state["last_error"] = None

    print(f"NDMA: {len(current)} current Uttarakhand alert(s) "
          f"from {len(candidates)} candidate item(s)", flush=True)


def _official_alerts():
    now = time.time()
    # One attempt per TTL whether it succeeds or fails, so a SACHET
    # outage can't turn every page view into a request to it.
    if now - _ndma_state["last_attempt"] >= NDMA_CACHE_TTL_SECONDS:
        _refresh_ndma_alerts()

    alerts = [a for a in _ndma_state["alerts"] if _still_valid(a["expires"], now)]
    return alerts


@app.route("/official-warnings")
def official_warnings():

    alerts = _official_alerts()

    return jsonify({
        "source": "NDMA SACHET (Common Alerting Protocol feed)",
        "source_url": "https://sachet.ndma.gov.in/",
        "checked_at": _ndma_state["checked_at"],
        "last_error": _ndma_state["last_error"],
        "alerts": [
            {key: alert[key] for key in (
                "identifier", "office", "sender", "event", "severity", "urgency", "certainty",
                "effective", "expires", "headline", "headline_hi", "description",
                "description_hi", "instruction", "area", "districts", "statewide", "link")}
            for alert in alerts
        ],
    })


# ============================================================
# SHELTERS
# ============================================================

@app.route("/shelters")
def get_shelters():

    return jsonify(SHELTERS_WITH_LOCALITY)


# ============================================================
# EVACUATE — route to the nearest reachable shelter
# ============================================================

def _off_network_response(points):
    """
    A 422 response if any (label, lat, lon) is too far from the road
    network to route from, else None. See road_snap_km in
    routing_engine.py: without this, far-off points were silently
    snapped to the nearest Uttarakhand road and reported as "ok".
    """

    for label, lat, lon in points:

        km = road_snap_km(lat, lon)

        if km > MAX_SNAP_KM:
            return jsonify({
                "status": "error",
                "error": (
                    f"The {label} is about {km:.0f} km from the nearest road "
                    "FloodSafe covers. Routing only works on Uttarakhand's "
                    "road network."
                ),
            }), 422

    return None


@app.route("/evacuate", methods=["POST"])
def evacuate():

    if not ROUTING_ENGINE_AVAILABLE:

        return jsonify({
            "status": "error",
            "error": "Routing is temporarily unavailable.",
            "details": ROUTING_ENGINE_ERROR or "Routing engine failed to load."
        }), 503

    if _rate_limited("routing"):
        return _rate_limit_response("routing")

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

        off_network = _off_network_response([("starting point", lat, lon)])

        if off_network is not None:
            return off_network

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
            "error": "Evacuation routing failed"
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

    if _rate_limited("routing"):
        return _rate_limit_response("routing")

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

        off_network = _off_network_response([("starting point", lat, lon)])

        if off_network is not None:
            return off_network

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
            "error": "Hospital routing failed"
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

    if _rate_limited("routing"):
        return _rate_limit_response("routing")

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

        off_network = _off_network_response([("starting point", start_lat, start_lon), ("destination", end_lat, end_lon)])

        if off_network is not None:
            return off_network

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
                "Missing routing parameter or result field"

        }), 400

    except ValueError as e:

        print()
        print("ROUTE VALUE ERROR:", repr(e))

        return jsonify({

            "status": "error",

            "error":
                "Invalid routing input"

        }), 400

    except Exception as e:

        print()
        print("ROUTE ERROR:", repr(e))

        return jsonify({

            "status": "error",

            "error":
                "Route calculation failed"

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

    if _rate_limited("routing"):
        return _rate_limit_response("routing")

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

        off_network = _off_network_response([("starting point", start_lat, start_lon), ("destination", end_lat, end_lon)])

        if off_network is not None:
            return off_network

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
            "error": "Route comparison failed"
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