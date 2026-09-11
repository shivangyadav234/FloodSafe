import os
import json
import time
import uuid
import threading
import requests
from difflib import get_close_matches

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

from flask import Flask, jsonify, request, send_file
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
        calculate_route, coordinates, shelters, find_nearest_shelter
    )

    ROUTING_ENGINE_AVAILABLE = True

except Exception as e:

    calculate_route = None
    coordinates = None
    shelters = []
    find_nearest_shelter = None

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

    try:

        with open(REPORTS_FILE, "w", encoding="utf-8") as f:
            json.dump(reports, f)

    except OSError as e:

        print("WARNING: failed to save reports.json:", repr(e))


_reports = _load_reports()


def _active_reports():

    now = time.time()

    fresh = [
        report for report in _reports
        if now - report.get("timestamp", 0) < REPORT_EXPIRY_SECONDS
    ]

    if len(fresh) != len(_reports):

        _reports[:] = fresh
        _save_reports(_reports)

    return fresh


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

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
        "routing_error": None if ROUTING_ENGINE_AVAILABLE else ROUTING_ENGINE_ERROR
    })


# ============================================================
# SEARCH
#
# KNOWN_PLACES is a small gazetteer of well-known Uttarakhand
# towns/villages, used only to correct obvious typos before
# handing the query to Nominatim — Nominatim itself does the
# real geocoding, this just fixes spelling first so "Brinag"
# still finds "Berinag".
# ============================================================

KNOWN_PLACES = [
    "Dehradun", "Rishikesh", "Haridwar", "Mussoorie", "Nainital",
    "Almora", "Bageshwar", "Pithoragarh", "Berinag", "Kanda", "Khelkot",
    "Munsyari", "Chamoli", "Joshimath", "Uttarkashi", "Gopeshwar",
    "Rudraprayag", "Kausani", "Ranikhet", "Champawat", "Tehri",
    "Gairsain", "Didihat", "Dwarahat", "Bhimtal", "Lansdowne", "Pauri",
    "Karnaprayag", "Roorkee", "Kotdwar", "Ramnagar", "Haldwani",
    "Kashipur", "Rudrapur", "Vikasnagar", "Srinagar", "Devprayag",
    "Ukhimath", "Chakrata", "Barkot", "Purola", "Auli", "Chopta",
    "Sitarganj", "Bazpur", "Tanakpur", "Lohaghat", "Dharchula",
    "Gangotri", "Yamunotri", "Kedarnath", "Badrinath"
]

MIN_SPELLING_SIMILARITY = 0.6


def _correct_spelling(query):
    """
    Best-effort typo fix against KNOWN_PLACES. Only corrects the
    first comma-separated token (the actual place name — later
    tokens are usually "India"/district names typed by the user)
    and only when it's close enough to a known place but not an
    exact match already. Returns the corrected query, or the
    original if nothing close enough was found.
    """

    first_token = query.split(",")[0].strip()

    if not first_token:
        return query

    matches = get_close_matches(
        first_token, KNOWN_PLACES, n=1, cutoff=MIN_SPELLING_SIMILARITY
    )

    if not matches or matches[0].lower() == first_token.lower():
        return query

    return query.replace(first_token, matches[0], 1)


# ============================================================
# NOMINATIM THROTTLE + CACHE
#
# Every visitor's search/report goes through THIS server, so they
# all share one outbound IP when talking to Nominatim — which
# enforces a strict ~1 request/second limit per IP and returns 429
# once it's exceeded. A single busy demo (a few people searching
# and reporting around the same time) is enough to trip that.
#
# Two independent mitigations:
#   - a hard minimum spacing between outbound Nominatim calls
#     (queues bursts instead of firing them all at once)
#   - a short-lived cache, since demo traffic tends to repeat the
#     same well-known place names within minutes of each other
# ============================================================

NOMINATIM_MIN_INTERVAL_SECONDS = 1.1
GEOCODE_CACHE_TTL_SECONDS = 600

_nominatim_lock = threading.Lock()
_last_nominatim_call = 0.0

_search_cache = {}    # normalized query -> (expires_at, results)
_reverse_cache = {}   # (rounded_lat, rounded_lon) -> (expires_at, place_name)


def _throttle_nominatim():
    """Blocks just long enough to keep outbound Nominatim calls at
    least NOMINATIM_MIN_INTERVAL_SECONDS apart, across all requests
    this process handles."""

    global _last_nominatim_call

    with _nominatim_lock:

        wait = (
            NOMINATIM_MIN_INTERVAL_SECONDS
            - (time.time() - _last_nominatim_call)
        )

        if wait > 0:
            time.sleep(wait)

        _last_nominatim_call = time.time()


def _geocode(query):
    """Raw Nominatim lookup — returns a parsed list of {lat, lon, name}."""

    cache_key = query.strip().lower()

    cached = _search_cache.get(cache_key)

    if cached and cached[0] > time.time():
        return cached[1]

    _throttle_nominatim()

    response = requests.get(
        "https://nominatim.openstreetmap.org/search",

        params={
            "q": query + ", India",
            "format": "jsonv2",
            "limit": 5,
            "countrycodes": "in",
            "addressdetails": 1
        },

        headers={
            "User-Agent":
                "FloodSafe/1.0 "
                "(college disaster-navigation project)"
        },

        timeout=15
    )

    response.raise_for_status()

    results = response.json()

    output = []

    for place in results:

        try:

            output.append({
                "lat": float(place["lat"]),
                "lon": float(place["lon"]),
                "name": place.get(
                    "display_name",
                    place.get("name", query)
                )
            })

        except (KeyError, ValueError, TypeError):

            continue

    _search_cache[cache_key] = (
        time.time() + GEOCODE_CACHE_TTL_SECONDS,
        output
    )

    return output


def _reverse_geocode(lat, lon):
    """
    Best-effort place name for a coordinate, via Nominatim's reverse
    endpoint. Used only for display (hazard-report list) — never
    blocks or fails the caller if it errors out or times out.
    Returns None on any failure.
    """

    # ~11m precision — plenty for "did someone already report near
    # here", and lets nearby reports share one cached lookup.
    cache_key = (round(lat, 4), round(lon, 4))

    cached = _reverse_cache.get(cache_key)

    if cached and cached[0] > time.time():
        return cached[1]

    try:

        _throttle_nominatim()

        response = requests.get(
            "https://nominatim.openstreetmap.org/reverse",

            params={
                "lat": lat,
                "lon": lon,
                "format": "jsonv2",
                "zoom": 16
            },

            headers={
                "User-Agent":
                    "FloodSafe/1.0 "
                    "(college disaster-navigation project)"
            },

            timeout=8
        )

        response.raise_for_status()

        place = response.json()

        place_name = place.get("display_name")

        _reverse_cache[cache_key] = (
            time.time() + GEOCODE_CACHE_TTL_SECONDS,
            place_name
        )

        return place_name

    except Exception as e:

        print("WARNING: reverse geocode failed:", repr(e))
        return None


@app.route("/search")
def search():

    query = request.args.get("q", "").strip()

    if not query:

        return jsonify([])

    if len(query) > 200:

        return jsonify({
            "error": "Search query is too long."
        }), 400

    print()
    print("================================")
    print("SEARCH")
    print("================================")
    print("Query:", query)

    try:

        output = _geocode(query)

        corrected_from = None
        corrected_to = None

        # Nominatim found nothing — try a spelling-corrected retry
        # before giving up, so a typo in a well-known town name
        # still finds something instead of "no places found".
        if not output:

            corrected_query = _correct_spelling(query)

            if corrected_query != query:

                print("No results — retrying as:", corrected_query)

                retry_output = _geocode(corrected_query)

                if retry_output:

                    output = retry_output
                    corrected_from = query
                    corrected_to = corrected_query

        print("Results:", len(output))

        response_body = {"results": output}

        if corrected_to:

            response_body["corrected_from"] = corrected_from
            response_body["corrected_to"] = corrected_to

            print(f"Corrected '{corrected_from}' -> '{corrected_to}'")

        return jsonify(response_body)

    except requests.exceptions.Timeout:

        return jsonify({
            "error": "Search request timed out."
        }), 504

    except requests.exceptions.RequestException as e:

        return jsonify({
            "error": "Search request failed.",
            "details": str(e)
        }), 502

    except Exception as e:

        return jsonify({
            "error": "Search failed.",
            "details": str(e)
        }), 500


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

.report-description {
    font-size: 14px;
    color: #444;
    margin-bottom: 6px;
}

.report-meta {
    font-size: 12px;
    color: #888;
    display: flex;
    justify-content: space-between;
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
        <a class="back-link" href="/">← Back to map</a>
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

            const marker = L.marker([report.lat, report.lon]).addTo(map);

            marker.bindPopup(
                "<b>" + escapeHtml(label) + "</b><br>" +
                escapeHtml(report.description)
            );

            markers[report.id] = marker;

            const card = document.createElement("div");
            card.className = "report-card";
            card.onclick = function() { focusReport(report.id); };

            card.innerHTML =
                '<div class="report-place">📍 ' + escapeHtml(label) + '</div>' +
                '<div class="report-description">' +
                escapeHtml(report.description) + '</div>' +
                '<div class="report-meta">' +
                '<span>' + timeAgo(report.timestamp) + '</span>' +
                '<span>' + report.lat.toFixed(5) + ', ' +
                report.lon.toFixed(5) + '</span>' +
                '</div>';

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

    place_name = _reverse_geocode(lat, lon)

    report = {
        "id": uuid.uuid4().hex,
        "lat": lat,
        "lon": lon,
        "place_name": place_name,
        "description": description,
        "timestamp": time.time()
    }

    print()
    print("================================")
    print("HAZARD REPORT")
    print("================================")
    print("Location:", lat, lon, "(", place_name, ")")
    print("Description:", description)

    _reports.append(report)
    _save_reports(_reports)

    return jsonify(report), 201


# ============================================================
# LIVE WEATHER
#
# Proxies Open-Meteo (no API key required) so the frontend can
# show current conditions and the router can become more
# cautious when it's actually raining, without either of them
# calling a third-party API directly from the browser.
# ============================================================

RAIN_LOW_THRESHOLD_MM = 5.0
RAIN_HIGH_THRESHOLD_MM = 15.0


@app.route("/weather")
def weather():

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

    try:

        response = requests.get(
            "https://api.open-meteo.com/v1/forecast",

            params={
                "latitude": lat,
                "longitude": lon,
                "current": "precipitation",
                "hourly": "precipitation",
                "forecast_days": 1,
                "timezone": "auto"
            },

            timeout=10
        )

        response.raise_for_status()

        payload = response.json()

        current_mm = float(
            payload.get("current", {}).get("precipitation", 0.0) or 0.0
        )

        hourly = payload.get("hourly", {})
        hourly_times = hourly.get("time", [])
        hourly_precip = hourly.get("precipitation", [])
        current_time = payload.get("current", {}).get("time")

        next_3h_mm = 0.0

        if current_time and hourly_times:

            try:
                start_index = hourly_times.index(current_time)
            except ValueError:
                start_index = 0

            next_3h_mm = float(
                sum(hourly_precip[start_index:start_index + 3])
            )

        total_mm = current_mm + next_3h_mm

        if total_mm < RAIN_LOW_THRESHOLD_MM:
            risk_level = "LOW"
        elif total_mm < RAIN_HIGH_THRESHOLD_MM:
            risk_level = "MODERATE"
        else:
            risk_level = "HIGH"

        return jsonify({
            "status": "ok",
            "current_mm": current_mm,
            "next_3h_mm": next_3h_mm,
            "total_mm": total_mm,
            "risk_level": risk_level
        })

    except requests.exceptions.Timeout:

        return jsonify({
            "error": "Weather request timed out."
        }), 504

    except requests.exceptions.RequestException as e:

        return jsonify({
            "error": "Weather request failed.",
            "details": str(e)
        }), 502

    except Exception as e:

        return jsonify({
            "error": "Weather lookup failed.",
            "details": str(e)
        }), 500


# ============================================================
# SHELTERS
# ============================================================

@app.route("/shelters")
def get_shelters():

    return jsonify(shelters)


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
        # If mode is SAFEST but the route still crosses an
        # EXTREME-risk segment, it means no other path exists
        # between these two points — not that the router picked
        # it carelessly. Let the frontend say so explicitly.
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