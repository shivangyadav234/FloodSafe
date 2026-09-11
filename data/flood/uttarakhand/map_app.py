import os

# =========================================================
# PROJ FIX
#
# Only override pyproj's bundled data dir if the caller
# explicitly points PROJ_DATA_OVERRIDE at a real folder.
# Otherwise use pyproj's own data — this is what makes the
# script runnable on any machine without editing paths.
# =========================================================

_PROJ_OVERRIDE = os.environ.get("PROJ_DATA_OVERRIDE")

if _PROJ_OVERRIDE and os.path.isdir(_PROJ_OVERRIDE):
    os.environ["PROJ_DATA"] = _PROJ_OVERRIDE
    os.environ["PROJ_LIB"] = _PROJ_OVERRIDE

import pyproj

if _PROJ_OVERRIDE and os.path.isdir(_PROJ_OVERRIDE):
    pyproj.datadir.set_data_dir(_PROJ_OVERRIDE)


# =========================================================
# IMPORTS
# =========================================================

import geopandas as gpd
import folium
from folium import GeoJson
from folium.plugins import Fullscreen


# =========================================================
# PATHS
#
# BASE defaults to a "data" folder next to this script, and
# can be overridden with FLOODSAFE_DATA_DIR — no hardcoded
# drive letters, so this runs unmodified on any machine.
# =========================================================

BASE = os.environ.get(
    "FLOODSAFE_DATA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
)

BOUNDARY_FILE = os.path.join(
    BASE,
    "uttarakhand_boundary.geojson"
)

HAZARD_FILE = os.path.join(
    BASE,
    "uttarakhand_flash_flood_hazard_clean.geojson"
)

ROAD_HAZARD_FILE = os.path.join(
    BASE,
    "uttarakhand_road_hazard_light.geojson"
)

OUTPUT_FILE = os.path.join(
    BASE,
    "uttarakhand_flood_map.html"
)


# =========================================================
# CHECK FILES
# =========================================================

print("Checking files...")

required_files = [
    BOUNDARY_FILE,
    HAZARD_FILE,
    ROAD_HAZARD_FILE
]

for file_path in required_files:

    if not os.path.exists(file_path):

        raise FileNotFoundError(
            f"Required file not found:\n{file_path}"
        )


# =========================================================
# LOAD DATA
# =========================================================

print()
print("Loading Uttarakhand boundary...")

boundary = gpd.read_file(BOUNDARY_FILE)

print(
    "Boundary:",
    len(boundary),
    "features"
)


print()
print("Loading flood hazard...")

hazard = gpd.read_file(HAZARD_FILE)

print(
    "Hazard polygons:",
    len(hazard)
)


print()
print("Loading hazardous roads...")

road_hazard = gpd.read_file(
    ROAD_HAZARD_FILE
)

print(
    "Hazardous roads:",
    len(road_hazard)
)


# =========================================================
# CRS
# =========================================================

if boundary.crs is None:

    boundary = boundary.set_crs(
        "EPSG:4326"
    )

else:

    boundary = boundary.to_crs(
        "EPSG:4326"
    )


if hazard.crs is None:

    hazard = hazard.set_crs(
        "EPSG:4326"
    )

else:

    hazard = hazard.to_crs(
        "EPSG:4326"
    )


if road_hazard.crs is None:

    road_hazard = road_hazard.set_crs(
        "EPSG:4326"
    )

else:

    road_hazard = road_hazard.to_crs(
        "EPSG:4326"
    )


# =========================================================
# DATA SUMMARY
# =========================================================

print()
print("========================================")
print("FloodSafe Data Summary")
print("========================================")
print()

for name in [
    "LOW",
    "MODERATE",
    "SIGNIFICANT",
    "EXTREME"
]:

    count = (
        hazard["hazard"] == name
    ).sum()

    print(
        f"{name:<12}: {count}"
    )


print()

for name in [
    "MODERATE",
    "SIGNIFICANT",
    "EXTREME"
]:

    count = (
        road_hazard["hazard"] == name
    ).sum()

    print(
        f"Road {name:<8}: {count}"
    )


# =========================================================
# CREATE MAP
# =========================================================

print()
print("Creating map...")

m = folium.Map(
    location=[
        30.0668,
        79.0193
    ],
    zoom_start=8,
    tiles="OpenStreetMap",
    control_scale=True
)

MAP_VAR = m.get_name()

print(
    "Map variable:",
    MAP_VAR
)


# =========================================================
# FULLSCREEN
# =========================================================

Fullscreen(
    position="topleft",
    title="Full Screen",
    title_cancel="Exit Full Screen",
    force_separate_button=True
).add_to(m)


# =========================================================
# UTTARAKHAND BOUNDARY
# =========================================================

boundary_layer = GeoJson(
    boundary,
    name="Uttarakhand Boundary",
    style_function=lambda feature: {

        "color": "#333333",

        "weight": 2,

        "fill": False

    }
)

boundary_layer.add_to(m)


# =========================================================
# FLOOD HAZARD COLORS
# =========================================================

HAZARD_COLORS = {

    "LOW": "limegreen",

    "MODERATE": "yellow",

    "SIGNIFICANT": "orange",

    "EXTREME": "red"

}


# =========================================================
# FLOOD HAZARD STYLE
# =========================================================

def hazard_style(feature):

    hazard_name = feature[
        "properties"
    ].get(
        "hazard",
        "LOW"
    )

    color = HAZARD_COLORS.get(
        hazard_name,
        "limegreen"
    )

    return {

        "color": color,

        "weight": 1,

        "fillColor": color,

        "fillOpacity": 0.30

    }


# =========================================================
# ALL FLOOD HAZARD
# =========================================================

hazard_layer = GeoJson(
    hazard,
    name="Flood Hazard — All Classes",
    style_function=hazard_style
)

hazard_layer.add_to(m)


# =========================================================
# INDIVIDUAL FLOOD LAYERS
# =========================================================

for hazard_name in HAZARD_COLORS:

    subset = hazard[
        hazard["hazard"] == hazard_name
    ].copy()

    if len(subset) == 0:

        continue

    color = HAZARD_COLORS[
        hazard_name
    ]

    layer = GeoJson(
        subset,
        name=f"Flood Hazard — {hazard_name}",
        style_function=lambda feature, c=color: {

            "color": c,

            "weight": 1,

            "fillColor": c,

            "fillOpacity": 0.30

        }
    )

    layer.add_to(m)


# =========================================================
# ROAD RISK COLORS
# =========================================================

ROAD_COLORS = {

    "MODERATE": "yellow",

    "SIGNIFICANT": "orange",

    "EXTREME": "red"

}


# =========================================================
# ROAD RISK STYLE
# =========================================================

def road_style(feature):

    hazard_name = feature[
        "properties"
    ].get(
        "hazard",
        "MODERATE"
    )

    color = ROAD_COLORS.get(
        hazard_name,
        "orange"
    )

    return {

        "color": color,

        "weight": 4,

        "opacity": 0.9

    }


# =========================================================
# ROAD RISK LAYER
# =========================================================

road_layer = GeoJson(
    road_hazard,
    name="Road Flood Risk",
    style_function=road_style
)

road_layer.add_to(m)


# =========================================================
# LEGEND
# =========================================================

legend_html = """
<div id="floodsafe-legend">

<div style="font-weight:700; margin-bottom:10px;">
Flood Hazard
</div>

<div>
<span class="legend-box low"></span>
LOW
</div>

<div>
<span class="legend-box moderate"></span>
MODERATE
</div>

<div>
<span class="legend-box significant"></span>
SIGNIFICANT
</div>

<div>
<span class="legend-box extreme"></span>
EXTREME
</div>

<div style="font-weight:700; margin-top:10px; margin-bottom:4px;">
Shelters (OpenStreetMap)
</div>

<div>⛺ Shelter / community facility</div>
<div>🏥 Hospital</div>

</div>
"""

m.get_root().html.add_child(
    folium.Element(
        legend_html
    )
)


# =========================================================
# CONTROL PANEL HTML
# =========================================================

panel_html = """
<div id="floodsafe-panel">

    <div class="fs-panel-header">

        <div>
            <div class="fs-title">
                FloodSafe
            </div>

            <div class="fs-subtitle">
                Flood-aware disaster navigation
            </div>
        </div>

        <button
            id="panelToggleBtn"
            class="fs-panel-toggle"
            onclick="togglePanel()"
            title="Minimize panel"
            aria-label="Minimize panel">
            −
        </button>

    </div>


    <div id="floodsafePanelBody">


    <!-- CURRENT LOCATION -->

    <div class="fs-label">
        Current Location
    </div>

    <div class="fs-location-wrap">
        <div class="fs-location-row">
            <input
                id="currentLocationInput"
                type="text"
                placeholder="Enter place name or lat, lon..."
                autocomplete="off">

            <button
                id="currentLocationBtn"
                class="fs-location-btn"
                onclick="useCurrentLocation()">
                📍 GPS
            </button>
        </div>

        <div
            id="locationResults"
            class="fs-results">
        </div>
    </div>

    <div
        id="locationStatus"
        class="fs-status">

        Enter a location or use GPS

    </div>

    <div
        id="liveConditions"
        class="fs-live-conditions"
        style="display:none;">
    </div>


    <!-- DESTINATION -->

    <div class="fs-label">
        Destination
    </div>


    <div class="fs-destination-wrap">
        <div class="fs-search-row">

            <input
                id="searchInput"
                type="text"
                placeholder="Search a place in India..."
                autocomplete="off">

            <button
                class="fs-search-btn"
                onclick="searchPlace()">

                Search

            </button>

        </div>

        <!-- SEARCH RESULTS -->
        <div
            id="searchResults"
            class="fs-results">
        </div>
    </div>


    <!-- SELECTED DESTINATION -->

    <div
        id="selectedDestination"
        class="fs-selected">

        No destination selected

    </div>


    <!-- ROUTE TYPE -->

    <div class="fs-label">
        Route type
    </div>


    <select
        id="routeMode"
        class="fs-select">

        <option value="FASTEST">
            ⚡ Fastest
        </option>

        <option value="SAFEST" selected>
            🛡 Safest
        </option>

    </select>


    <!-- CALCULATE -->

    <button
        class="fs-route-btn"
        onclick="calculateRoute()">

        Calculate Route

    </button>


    <!-- EVACUATE -->

    <button
        class="fs-evacuate-btn"
        onclick="evacuateToShelter()">

        🚨 Evacuate to Nearest Shelter

    </button>


    <!-- COMPARE -->

    <button
        class="fs-compare-btn"
        onclick="compareRoutes()">

        ⇄ Compare Fastest vs Safest

    </button>


    <!-- CLEAR -->

    <button
        class="fs-clear-btn"
        onclick="clearRoute()">

        Clear Route

    </button>


    <!-- RESULT -->

    <div
        id="routeResult"
        class="fs-route-result">
    </div>

    <div
        id="compareResult"
        class="fs-route-result">
    </div>

    <div class="fs-label" style="margin-top:18px;">
        Hazard reports
    </div>

    <div class="fs-hint">
        Click anywhere on the map to report a flooded or
        blocked road. Reports are shared with everyone and
        automatically routed around.
    </div>

    <a
        href="/reports-view"
        target="_blank"
        class="fs-view-reports-link">

        📋 View All Reports

    </a>


    </div>

</div>
"""

m.get_root().html.add_child(
    folium.Element(
        panel_html
    )
)


# =========================================================
# CSS
# =========================================================

css = """
<style>

#floodsafe-panel {

    position: fixed;

    top: 20px;

    left: 50%;

    transform: translateX(-50%);

    width: 470px;

    max-width: calc(100vw - 30px);

    max-height: calc(100vh - 40px);

    overflow-y: auto;

    overflow-x: visible;

    box-sizing: border-box;

    background: white;

    padding: 22px;

    border-radius: 20px;

    box-shadow:
        0 8px 30px rgba(0,0,0,0.25);

    z-index: 999999;

    font-family:
        Arial,
        Helvetica,
        sans-serif;

}


.fs-panel-header {

    display: flex;

    align-items: flex-start;

    justify-content: space-between;

    gap: 10px;

}


.fs-title {

    font-size: 28px;

    font-weight: 700;

    color: #333;

}


.fs-subtitle {

    font-size: 16px;

    color: #777;

    margin-top: 4px;

    margin-bottom: 18px;

}


.fs-panel-toggle {

    flex-shrink: 0;

    width: 36px;

    height: 36px;

    border: 1px solid #ddd;

    background: #fafafa;

    border-radius: 10px;

    font-size: 22px;

    line-height: 1;

    color: #555;

    cursor: pointer;

}


.fs-panel-toggle:hover {

    background: #f1f1f1;

}


#floodsafe-panel.fs-collapsed {

    padding: 14px 22px;

    width: auto;

}


#floodsafe-panel.fs-collapsed .fs-subtitle {

    margin-bottom: 0;

}


#floodsafe-panel.fs-collapsed #floodsafePanelBody {

    display: none;

}


.fs-results-close {

    position: sticky;

    top: 0;

    display: flex;

    justify-content: flex-end;

    background: white;

    padding: 4px 4px 0 0;

}


.fs-results-close button {

    border: none;

    background: #f1f1f1;

    border-radius: 6px;

    width: 24px;

    height: 24px;

    font-size: 14px;

    line-height: 1;

    cursor: pointer;

    color: #555;

}


.fs-results-close button:hover {

    background: #e2e2e2;

}


.fs-location-wrap,
.fs-destination-wrap {
    position: relative;
}

.fs-location-row {
    display: flex;
    gap: 8px;
    align-items: stretch;
}

#currentLocationInput {
    flex: 1;
    min-width: 0;
    padding: 13px;
    border: 1px solid #ccc;
    border-radius: 10px;
    font-size: 16px;
    box-sizing: border-box;
}

.fs-location-btn {

    width: 82px;

    padding: 13px;

    border: 1px solid #ddd;

    background: #fafafa;

    border-radius: 10px;

    font-size: 15px;

    cursor: pointer;

    text-align: center;

}


.fs-location-btn:hover {

    background: #f1f1f1;

}


.fs-status {

    margin-top: 8px;

    margin-bottom: 15px;

    font-size: 14px;

    color: #555;

}


.fs-label {

    font-size: 15px;

    color: #666;

    margin-top: 14px;

    margin-bottom: 7px;

}


.fs-search-row {

    display: flex;

    gap: 8px;

}


#searchInput {

    flex: 1;

    padding: 14px;

    border:
        1px solid #ccc;

    border-radius: 10px;

    font-size: 16px;

    box-sizing: border-box;

}


.fs-search-btn {

    width: 92px;

    border: none;

    background: #1976d2;

    color: white;

    border-radius: 10px;

    font-size: 16px;

    cursor: pointer;

}


.fs-search-btn:hover {

    background: #1565c0;

}


.fs-results {
    position: absolute;
    left: 0;
    right: 0;
    top: calc(100% + 6px);
    margin: 0;
    max-height: 190px;
    overflow-y: auto;
    overflow-x: hidden;
    display: none;
    background: white;
    border: 1px solid #ddd;
    border-radius: 10px;
    box-shadow: 0 8px 22px rgba(0,0,0,0.18);
    z-index: 1000001;
}

.fs-results.fs-visible {
    display: block;
}


.fs-result {

    padding: 10px;

    border-bottom:
        1px solid #eee;

    cursor: pointer;

    font-size: 14px;

    color: #444;

}


.fs-result:hover {

    background: #f2f7ff;

}


.fs-spelling-notice {
    padding: 8px 10px;
    font-size: 12px;
    color: #8a6d00;
    background: #fff8e1;
    border-bottom: 1px solid #eee;
    font-style: italic;
}


.fs-selected {

    margin-top: 8px;

    padding: 10px;

    background: #f5f5f5;

    border-radius: 8px;

    font-size: 14px;

    color: #444;

}


.fs-select {

    width: 100%;

    padding: 13px;

    border:
        1px solid #ccc;

    border-radius: 10px;

    font-size: 16px;

    background: white;

}


.fs-route-btn {

    width: 100%;

    margin-top: 18px;

    padding: 15px;

    border: none;

    border-radius: 10px;

    background: #1976d2;

    color: white;

    font-size: 18px;

    font-weight: 700;

    cursor: pointer;

}


.fs-route-btn:hover {

    background: #1565c0;

}


.fs-route-btn:disabled,
.fs-location-btn:disabled,
.fs-evacuate-btn:disabled,
.fs-compare-btn:disabled,
#currentLocationInput:disabled,
#searchInput:disabled {
    opacity: 0.6;
    cursor: not-allowed;
}


.fs-clear-btn {

    width: 100%;

    margin-top: 10px;

    padding: 13px;

    border:
        1px solid #ddd;

    border-radius: 10px;

    background: white;

    color: #555;

    font-size: 16px;

    cursor: pointer;

}


.fs-compare-btn {

    width: 100%;

    margin-top: 10px;

    padding: 13px;

    border: 1px solid #6a1b9a;

    border-radius: 10px;

    background: #f3e5f5;

    color: #6a1b9a;

    font-size: 15px;

    font-weight: 700;

    cursor: pointer;

}

.fs-compare-btn:hover {
    background: #ead1f0;
}


.fs-evacuate-btn {

    width: 100%;

    margin-top: 18px;

    padding: 14px;

    border: none;

    border-radius: 10px;

    background: #c62828;

    color: white;

    font-size: 16px;

    font-weight: 700;

    cursor: pointer;

}

.fs-evacuate-btn:hover {
    background: #ad2121;
}


.fs-live-conditions {

    margin-top: 8px;

    margin-bottom: 6px;

    padding: 9px 12px;

    border-radius: 8px;

    font-size: 13px;

    line-height: 1.5;

}

.fs-live-conditions.fs-live-low {
    background: #e8f5e9;
    color: #2e7d32;
}

.fs-live-conditions.fs-live-moderate {
    background: #fff8e1;
    color: #f57f17;
}

.fs-live-conditions.fs-live-high {
    background: #fff3e0;
    color: #e65100;
}


.fs-hint {
    font-size: 12px;
    color: #888;
    line-height: 1.5;
}


.fs-view-reports-link {
    display: block;
    margin-top: 10px;
    padding: 10px;
    text-align: center;
    background: #fafafa;
    border: 1px solid #ddd;
    border-radius: 10px;
    color: #444;
    font-size: 14px;
    font-weight: 600;
    text-decoration: none;
}

.fs-view-reports-link:hover {
    background: #f1f1f1;
}


.fs-compare-row {
    display: flex;
    justify-content: space-between;
    padding: 4px 0;
    border-bottom: 1px solid #eee;
}

.fs-compare-row:last-child {
    border-bottom: none;
}


.fs-report-popup textarea {
    width: 200px;
    height: 50px;
    font-size: 13px;
    padding: 6px;
    border-radius: 6px;
    border: 1px solid #ccc;
    box-sizing: border-box;
    resize: none;
}

.fs-report-popup button {
    margin-top: 6px;
    width: 100%;
    padding: 8px;
    border: none;
    border-radius: 6px;
    background: #d32f2f;
    color: white;
    font-weight: 600;
    cursor: pointer;
}


.fs-result-title {
    font-size: 16px;
    font-weight: 700;
    margin-bottom: 4px;
}

.fs-route-mode {
    font-size: 13px;
    color: #666;
}

.fs-distance {
    font-size: 24px;
    font-weight: 700;
    margin: 4px 0 10px;
}

.fs-safe-message {
    padding: 9px;
    margin: 8px 0;
    border-radius: 8px;
    background: #e8f5e9;
    color: #2e7d32;
    font-weight: 600;
}

.fs-warning-message {
    padding: 9px;
    margin: 8px 0;
    border-radius: 8px;
    background: #fff3e0;
    color: #e65100;
    font-weight: 600;
}

.fs-risk-title {
    font-weight: 700;
    margin-top: 8px;
    margin-bottom: 3px;
}

.fs-risk-row {
    display: flex;
    align-items: center;
    gap: 7px;
    margin: 3px 0;
}

.fs-swatch {
    display: inline-block;
    width: 12px;
    height: 12px;
    border-radius: 3px;
    flex-shrink: 0;
}

.fs-route-result {

    margin-top: 14px;

    padding: 12px;

    background: #f7f7f7;

    border-radius: 10px;

    font-size: 14px;

    line-height: 1.6;

}


#floodsafe-legend {

    position: fixed;

    bottom: 30px;

    left: 30px;

    z-index: 999999;

    background: white;

    padding: 12px 16px;

    border-radius: 8px;

    box-shadow:
        0 2px 8px rgba(0,0,0,0.3);

    font-size: 14px;

}


.legend-box {

    display: inline-block;

    width: 14px;

    height: 14px;

    margin-right: 6px;

    vertical-align: -2px;

}


.legend-box.low {
    background: limegreen;
}

.legend-box.moderate {
    background: yellow;
}

.legend-box.significant {
    background: orange;
}

.legend-box.extreme {
    background: red;
}


@media(max-width:600px) {

    #floodsafe-panel {

        width: calc(100% - 30px);
        max-width: none;
        top: 15px;
        max-height: calc(100vh - 30px);
        padding: 18px;

    }

}

</style>
"""

m.get_root().html.add_child(
    folium.Element(
        css
    )
)


# =========================================================
# JAVASCRIPT
# =========================================================
#
# IMPORTANT:
# This is a NORMAL Python string.
# It is NOT an f-string.
#
# Therefore JavaScript { } will NOT cause
# Python f-string syntax errors.
# =========================================================

javascript = """
<script>

let currentLocation = null;

let selectedDestination = null;

let currentMarker = null;

let destinationMarker = null;

let routeLine = null;

let liveWeather = null;

let hazardMarkers = [];

let compareGroup = null;

let shelterMarkers = [];


// =======================================================
// GLOBAL SAFETY NET
//
// If something unexpected throws outside of a try/catch
// (a bug, a bad response, an extension interfering, etc.)
// log it instead of letting the page silently break, so
// the rest of the app keeps working.
// =======================================================

window.addEventListener("error", function(event) {
    console.error("FloodSafe error:", event.error || event.message);
});

window.addEventListener("unhandledrejection", function(event) {
    console.error("FloodSafe unhandled rejection:", event.reason);
});


// =======================================================
// NETWORK HELPER
//
// Wraps fetch() with a timeout and clearer error types so
// callers can tell apart "offline", "timed out" and
// "server returned an error" instead of hanging forever
// or showing a generic failure.
// =======================================================

async function fetchWithTimeout(url, options, timeoutMs) {

    if (!navigator.onLine) {

        const offlineError = new Error(
            "You appear to be offline. Check your connection."
        );
        offlineError.name = "OfflineError";
        throw offlineError;
    }

    const controller = new AbortController();

    const timeoutId = setTimeout(
        function() {
            controller.abort();
        },
        timeoutMs || 15000
    );

    try {

        const response = await fetch(
            url,
            Object.assign({}, options, { signal: controller.signal })
        );

        return response;

    } catch (error) {

        if (error.name === "AbortError") {

            const timeoutError = new Error(
                "Request timed out. Please try again."
            );
            timeoutError.name = "TimeoutError";
            throw timeoutError;
        }

        // Typically a network failure (DNS, connection refused, etc.)
        const networkError = new Error(
            "Could not reach the server. " +
            "Check your connection and that the server is running."
        );
        networkError.name = "NetworkError";
        throw networkError;

    } finally {

        clearTimeout(timeoutId);
    }
}


// =======================================================
// HTML ESCAPE
// =======================================================

function escapeHtml(text) {

    const div = document.createElement("div");

    div.textContent = text;

    return div.innerHTML;
}


// =======================================================
// ROUTE SEGMENT COLORS
//
// Mirrors the flood-risk classes (Normal/Moderate/Significant/
// Extreme) as line colors, so the calculated route itself shows
// where the risk actually is instead of only the summary counts
// underneath it.
// =======================================================

const ROUTE_SEGMENT_COLORS = {
    1: "#1565c0",
    2: "#f9a825",
    4: "#ef6c00",
    8: "#c62828"
};

function segmentColor(riskValue) {
    return ROUTE_SEGMENT_COLORS[riskValue] || ROUTE_SEGMENT_COLORS[1];
}


function showResults(box) {

    box.classList.add("fs-visible");

    // Always give the dropdown an explicit close (✕) button so
    // it can be dismissed even without clicking away — useful on
    // mobile where the map itself is easy to miss-tap.
    if (!box.querySelector(".fs-results-close")) {

        box.insertAdjacentHTML(
            "afterbegin",
            '<div class="fs-results-close">' +
            '<button type="button" aria-label="Close results">✕</button>' +
            '</div>'
        );

        const closeBtn = box.querySelector(".fs-results-close button");

        closeBtn.onclick = function() {
            hideResults(box);
        };
    }
}

function hideResults(box) {
    box.classList.remove("fs-visible");
    box.innerHTML = "";
}

function hideAllSearchResults() {
    hideResults(document.getElementById("locationResults"));
    hideResults(document.getElementById("searchResults"));
}


// =======================================================
// PANEL MINIMIZE / EXPAND
//
// On small screens the control panel can cover most of the
// map. Let the user collapse it down to just the title bar
// so they can see the map underneath, then expand it again.
// =======================================================

function togglePanel() {

    const panel = document.getElementById("floodsafe-panel");
    const toggleBtn = document.getElementById("panelToggleBtn");

    const collapsed = panel.classList.toggle("fs-collapsed");

    if (toggleBtn) {

        toggleBtn.innerHTML = collapsed ? "+" : "−";

        toggleBtn.title =
            collapsed ? "Expand panel" : "Minimize panel";

        toggleBtn.setAttribute(
            "aria-label",
            collapsed ? "Expand panel" : "Minimize panel"
        );
    }

    if (collapsed) {
        hideAllSearchResults();
    }
}



// =======================================================
// SHORT PLACE NAME
// =======================================================

function getShortPlaceName(name) {

    if (!name) {

        return "Selected destination";

    }

    const parts = name.split(",");

    if (parts.length >= 2) {

        return (
            parts[0].trim()
            + ", "
            + parts[1].trim()
        );

    }

    return name;
}


// =======================================================
// CURRENT LOCATION
// =======================================================

function setCurrentLocation(lat, lon, name) {

    currentLocation = {
        lat: Number(lat),
        lon: Number(lon)
    };

    const input = document.getElementById("currentLocationInput");
    const status = document.getElementById("locationStatus");
    const resultsBox = document.getElementById("locationResults");

    if (name) {
        input.value = name;
    } else {
        input.value =
            Number(lat).toFixed(6) +
            ", " +
            Number(lon).toFixed(6);
    }

    hideResults(resultsBox);

    status.innerHTML = "✓ Current location selected";

    if (currentMarker) {
        FLOODSAFE_MAP.removeLayer(currentMarker);
    }

    currentMarker = L.circleMarker(
        [Number(lat), Number(lon)],
        {
            radius: 8,
            color: "#1565c0",
            fillColor: "#2196f3",
            fillOpacity: 1,
            weight: 3
        }
    ).addTo(FLOODSAFE_MAP);

    FLOODSAFE_MAP.setView(
        [Number(lat), Number(lon)],
        12
    );

    fetchLiveConditions(Number(lat), Number(lon));
}


// =======================================================
// LIVE CONDITIONS (rainfall)
// =======================================================

async function fetchLiveConditions(lat, lon) {

    const box = document.getElementById("liveConditions");

    box.style.display = "block";
    box.className = "fs-live-conditions";
    box.innerHTML = "Checking live rainfall...";

    try {

        const response = await fetchWithTimeout(
            "/weather?lat=" + lat + "&lon=" + lon,
            {},
            12000
        );

        const data = await response.json();

        if (!response.ok || data.error) {
            box.style.display = "none";
            liveWeather = null;
            return;
        }

        liveWeather = data;

        const tierClass =
            data.risk_level === "HIGH" ? "fs-live-high" :
            data.risk_level === "MODERATE" ? "fs-live-moderate" :
            "fs-live-low";

        box.className = "fs-live-conditions " + tierClass;

        const icon =
            data.risk_level === "HIGH" ? "⛈" :
            data.risk_level === "MODERATE" ? "🌧" :
            "🌤";

        let message =
            icon + " Live rainfall near you: " +
            data.total_mm.toFixed(1) + "mm (next few hours)";

        if (data.risk_level !== "LOW") {
            message +=
                "<br>Routing is being extra cautious because of " +
                "current rain.";
        }

        box.innerHTML = message;

    } catch (error) {

        console.error(error);
        box.style.display = "none";
        liveWeather = null;
    }
}


function useCurrentLocation() {

    const status =
        document.getElementById("locationStatus");

    const gpsBtn =
        document.getElementById("currentLocationBtn");

    if (!navigator.geolocation) {

        status.innerHTML =
            "Geolocation is not supported by this browser. " +
            "Enter a location manually instead.";

        return;
    }

    if (
        window.isSecureContext === false
    ) {

        status.innerHTML =
            "GPS requires a secure connection (HTTPS or " +
            "localhost). Enter a location manually instead.";

        return;
    }

    status.innerHTML =
        "Getting your current location...";

    if (gpsBtn) {
        gpsBtn.disabled = true;
    }

    function reEnableButton() {
        if (gpsBtn) {
            gpsBtn.disabled = false;
        }
    }

    try {

        navigator.geolocation.getCurrentPosition(

            function(position) {

                reEnableButton();

                try {

                    setCurrentLocation(
                        position.coords.latitude,
                        position.coords.longitude,
                        "GPS: " +
                        position.coords.latitude.toFixed(6) +
                        ", " +
                        position.coords.longitude.toFixed(6)
                    );

                } catch (error) {

                    console.error(error);

                    status.innerHTML =
                        "Got a GPS position but failed to use it. " +
                        "Enter a location manually instead.";
                }

            },

            function(error) {

                reEnableButton();

                let message =
                    "Unable to get your location. " +
                    "Try again or enter it manually.";

                if (error.code === 1) {
                    message =
                        "Location permission was denied. " +
                        "Allow location access, or enter a " +
                        "location manually.";
                }

                if (error.code === 2) {
                    message =
                        "Location is unavailable right now. " +
                        "Try again or enter it manually.";
                }

                if (error.code === 3) {
                    message =
                        "Location request timed out. " +
                        "Try again or enter it manually.";
                }

                status.innerHTML = message;

            },

            {
                enableHighAccuracy: true,
                timeout: 15000,
                maximumAge: 0
            }
        );

    } catch (error) {

        // Some browsers/extensions can throw synchronously here.
        console.error(error);

        reEnableButton();

        status.innerHTML =
            "Couldn't start GPS lookup. Enter a location manually.";
    }
}


// Search manually entered current location.
async function searchCurrentLocation() {

    const input =
        document.getElementById("currentLocationInput");

    const resultsBox =
        document.getElementById("locationResults");

    const status =
        document.getElementById("locationStatus");

    const query = input.value.trim();

    if (!query) {
        currentLocation = null;
        hideResults(resultsBox);
        status.innerHTML = "Enter a location or use GPS";
        return;
    }

    const match = query.match(
        /^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$/
    );

    if (match) {

        const lat = Number(match[1]);
        const lon = Number(match[2]);

        if (
            lat >= -90 && lat <= 90 &&
            lon >= -180 && lon <= 180
        ) {
            setCurrentLocation(lat, lon, "Manual: " + query);
            return;
        }
    }

    resultsBox.innerHTML =
        '<div class="fs-result">Searching...</div>';
    showResults(resultsBox);

    input.disabled = true;

    try {

        const response = await fetchWithTimeout(
            "/search?q=" + encodeURIComponent(query),
            {},
            15000
        );

        let data;

        try {
            data = await response.json();
        } catch (parseError) {
            throw new Error(
                "Server returned an unreadable response " +
                "(status " + response.status + ")."
            );
        }

        if (!response.ok) {
            throw new Error(
                (data && data.error) ||
                ("Server returned " + response.status)
            );
        }

        if (data && data.error) {
            resultsBox.innerHTML =
                '<div class="fs-result">' +
                escapeHtml(data.error) +
                '</div>';
            showResults(resultsBox);
            return;
        }

        const places = (data && Array.isArray(data.results)) ?
            data.results : [];

        if (places.length === 0) {
            resultsBox.innerHTML =
                '<div class="fs-result">No places found. ' +
                'Try a different spelling, or enter ' +
                'latitude, longitude directly.</div>';
            showResults(resultsBox);
            return;
        }

        resultsBox.innerHTML = "";

        if (data.corrected_to) {
            const notice = document.createElement("div");
            notice.className = "fs-spelling-notice";
            notice.innerHTML =
                'Showing results for "' +
                escapeHtml(data.corrected_to) +
                '" (did you mean this instead of "' +
                escapeHtml(data.corrected_from) + '"?)';
            resultsBox.appendChild(notice);
        }

        places.forEach(function(place) {

            if (
                !place ||
                typeof place.lat !== "number" ||
                typeof place.lon !== "number"
            ) {
                return;
            }

            const item = document.createElement("div");

            item.className = "fs-result";
            item.innerHTML = escapeHtml(place.name || "Unnamed place");

            item.onclick = function() {
                setCurrentLocation(
                    Number(place.lat),
                    Number(place.lon),
                    place.name
                );
            };

            resultsBox.appendChild(item);
        });

        showResults(resultsBox);

    }
    catch(error) {

        console.error(error);

        resultsBox.innerHTML =
            '<div class="fs-result">' +
            escapeHtml(error.message || "Search failed. Please try again.") +
            '</div>';
        showResults(resultsBox);
    }
    finally {

        input.disabled = false;
    }
}


// =======================================================
// DEBOUNCE
//
// Waits for a pause in typing before firing, so live-as-you-type
// search doesn't fire a request on every keystroke (and doesn't
// hammer Nominatim's free rate limit).
// =======================================================

function debounce(fn, delayMs) {

    let timer = null;

    return function() {

        const args = arguments;
        const context = this;

        clearTimeout(timer);

        timer = setTimeout(
            function() {
                fn.apply(context, args);
            },
            delayMs
        );
    };
}

const MIN_LIVE_SEARCH_LENGTH = 3;
const LIVE_SEARCH_DEBOUNCE_MS = 450;

const debouncedSearchCurrentLocation = debounce(
    function() {

        const value = document.getElementById(
            "currentLocationInput"
        ).value.trim();

        if (value.length >= MIN_LIVE_SEARCH_LENGTH) {
            searchCurrentLocation();
        }
    },
    LIVE_SEARCH_DEBOUNCE_MS
);

const debouncedSearchPlace = debounce(
    function() {

        const value = document.getElementById(
            "searchInput"
        ).value.trim();

        if (value.length >= MIN_LIVE_SEARCH_LENGTH) {
            searchPlace();
        }
    },
    LIVE_SEARCH_DEBOUNCE_MS
);


// Press Enter in current-location field.
document.addEventListener(
    "DOMContentLoaded",
    function() {

        const input =
            document.getElementById(
                "currentLocationInput"
            );

        if (input) {

            input.addEventListener(
                "keydown",
                function(event) {

                    if (event.key === "Enter") {
                        event.preventDefault();
                        searchCurrentLocation();
                    }

                }
            );
        }

        const destinationInput =
            document.getElementById(
                "searchInput"
            );

        if (destinationInput) {

            destinationInput.addEventListener(
                "keydown",
                function(event) {

                    if (event.key === "Enter") {
                        event.preventDefault();
                        searchPlace();
                    }

                }
            );

            destinationInput.addEventListener(
                "input",
                function() {
                    selectedDestination = null;
                    document.getElementById(
                        "selectedDestination"
                    ).innerHTML = "No destination selected";
                    hideResults(
                        document.getElementById("searchResults")
                    );
                    debouncedSearchPlace();
                }
            );
        }

        const currentInput =
            document.getElementById("currentLocationInput");

        if (currentInput) {
            currentInput.addEventListener(
                "input",
                function() {
                    currentLocation = null;
                    document.getElementById(
                        "locationStatus"
                    ).innerHTML =
                        "Press Enter to set this location, or use GPS";
                    hideResults(
                        document.getElementById("locationResults")
                    );
                    debouncedSearchCurrentLocation();
                }
            );
        }

        document.addEventListener(
            "click",
            function(event) {
                if (
                    !event.target.closest(".fs-location-wrap") &&
                    !event.target.closest(".fs-destination-wrap")
                ) {
                    hideAllSearchResults();
                }
            }
        );

    }
);


// =======================================================

// SEARCH
// =======================================================

async function searchPlace() {

    const input =
        document.getElementById("searchInput");

    const resultsBox =
        document.getElementById("searchResults");

    const query = input.value.trim();

    if (!query) {
        hideResults(resultsBox);
        return;
    }

    // Allow destination to be entered directly as: latitude, longitude
    const coordinateMatch = query.match(
        /^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$/
    );

    if (coordinateMatch) {
        const lat = Number(coordinateMatch[1]);
        const lon = Number(coordinateMatch[2]);

        if (lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180) {
            selectDestination({
                lat: lat,
                lon: lon,
                name: "Manual: " + query
            });
            return;
        }

        resultsBox.innerHTML =
            '<div class="fs-result">Invalid latitude/longitude.</div>';
        showResults(resultsBox);
        return;
    }

    resultsBox.innerHTML =
        '<div class="fs-result">Searching...</div>';
    showResults(resultsBox);

    input.disabled = true;

    try {

        const response = await fetchWithTimeout(
            "/search?q=" + encodeURIComponent(query),
            {},
            15000
        );

        let data;

        try {
            data = await response.json();
        } catch (parseError) {
            throw new Error(
                "Server returned an unreadable response " +
                "(status " + response.status + ")."
            );
        }

        if (!response.ok) {
            throw new Error(
                (data && data.error) ||
                ("Server returned " + response.status)
            );
        }

        if (data && data.error) {
            resultsBox.innerHTML =
                '<div class="fs-result">Search error: ' +
                escapeHtml(data.error) +
                '</div>';
            showResults(resultsBox);
            return;
        }

        const places = (data && Array.isArray(data.results)) ?
            data.results : [];

        if (places.length === 0) {
            resultsBox.innerHTML =
                '<div class="fs-result">No places found. ' +
                'Try a different spelling, or enter ' +
                'latitude, longitude directly.</div>';
            showResults(resultsBox);
            return;
        }

        resultsBox.innerHTML = "";

        if (data.corrected_to) {
            const notice = document.createElement("div");
            notice.className = "fs-spelling-notice";
            notice.innerHTML =
                'Showing results for "' +
                escapeHtml(data.corrected_to) +
                '" (did you mean this instead of "' +
                escapeHtml(data.corrected_from) + '"?)';
            resultsBox.appendChild(notice);
        }

        places.forEach(function(place) {

            if (
                !place ||
                typeof place.lat !== "number" ||
                typeof place.lon !== "number"
            ) {
                return;
            }

            const item = document.createElement("div");

            item.className = "fs-result";
            item.innerHTML = escapeHtml(place.name || "Unnamed place");

            item.onclick = function() {
                selectDestination(place);
            };

            resultsBox.appendChild(item);
        });

        showResults(resultsBox);

    }
    catch(error) {

        console.error(error);

        resultsBox.innerHTML =
            '<div class="fs-result">' +
            escapeHtml(error.message || "Search failed. Please try again.") +
            '</div>';
        showResults(resultsBox);
    }
    finally {

        input.disabled = false;
    }
}


// =======================================================
// SELECT DESTINATION
// =======================================================

function selectDestination(place) {

    selectedDestination = {

        lat:
            Number(place.lat),

        lon:
            Number(place.lon),

        name:
            place.name

    };


    document.getElementById(
        "selectedDestination"
    ).innerHTML =

        "✓ "
        +
        escapeHtml(
            getShortPlaceName(
                place.name
            )
        );


    const destinationResults =
        document.getElementById("searchResults");

    hideResults(destinationResults);

    document.getElementById("searchInput").value =
        getShortPlaceName(place.name);


    if (destinationMarker) {

        FLOODSAFE_MAP.removeLayer(
            destinationMarker
        );

    }


    destinationMarker =
        L.circleMarker(

            [

                selectedDestination.lat,

                selectedDestination.lon

            ],

            {

                radius: 8,

                color: "#b71c1c",

                fillColor: "#f44336",

                fillOpacity: 1,

                weight: 3

            }

        ).addTo(
            FLOODSAFE_MAP
        );


    FLOODSAFE_MAP.setView(

        [

            selectedDestination.lat,

            selectedDestination.lon

        ],

        11

    );

}


// =======================================================
// CALCULATE ROUTE
// =======================================================

async function calculateRoute() {

    if (!currentLocation) {

        alert(
            "Please select your current location first."
        );

        return;

    }


    if (!selectedDestination) {

        alert(
            "Please search and select a destination first."
        );

        return;

    }

    if (
        currentLocation.lat === selectedDestination.lat
        &&
        currentLocation.lon === selectedDestination.lon
    ) {

        alert(
            "Your current location and destination are the same. " +
            "Please choose a different destination."
        );

        return;
    }


    const mode =
        document.getElementById(
            "routeMode"
        ).value;


    const resultBox =
        document.getElementById(
            "routeResult"
        );

    const calculateBtn =
        document.querySelector(".fs-route-btn");


    resultBox.innerHTML =
        "Calculating route...";

    if (calculateBtn) {
        calculateBtn.disabled = true;
    }


    try {

        const response =
            await fetchWithTimeout(

                "/route",

                {

                    method: "POST",

                    headers: {

                        "Content-Type":
                            "application/json"

                    },

                    body:
                        JSON.stringify({

                            start_lat:
                                currentLocation.lat,

                            start_lon:
                                currentLocation.lon,

                            end_lat:
                                selectedDestination.lat,

                            end_lon:
                                selectedDestination.lon,

                            mode:
                                mode,

                            live_rain_mm:
                                liveWeather ? liveWeather.total_mm : null

                        })

                },

                20000

            );


        let data;

        try {
            data = await response.json();
        } catch (parseError) {
            throw new Error(
                "Server returned an unreadable response " +
                "(status " + response.status + ")."
            );
        }


        if (
            !response.ok
            ||
            (data && data.error)
        ) {

            throw new Error(

                (data && (data.details || data.error))
                ||
                "Route calculation failed"

            );

        }

        if (
            !data ||
            !Array.isArray(data.coordinates) ||
            data.coordinates.length === 0
        ) {

            throw new Error(
                "Server returned an empty or invalid route."
            );
        }


        // ------------------------------------------------
        // REMOVE OLD ROUTE
        // ------------------------------------------------

        if (routeLine) {

            FLOODSAFE_MAP.removeLayer(
                routeLine
            );

        }


        // ------------------------------------------------
        // CONVERT [LON,LAT] TO [LAT,LON]
        // ------------------------------------------------

        const leafletCoordinates =
            data.coordinates.map(

                function(point) {

                    return [

                        point[1],

                        point[0]

                    ];

                }

            );


        // ------------------------------------------------
        // DRAW ROUTE
        //
        // Drawn as one short polyline per hop, colored by that
        // hop's own flood risk (segment_risks[i] lines up with
        // the gap between coordinate i and i+1), instead of one
        // flat-colored line for the whole route. Grouped in a
        // featureGroup so the rest of the code (removeLayer,
        // getBounds) can keep treating "routeLine" as one layer.
        // ------------------------------------------------

        const segmentRisks =
            Array.isArray(data.segment_risks) ?
                data.segment_risks :
                [];

        // A route that snaps to a single node has only one
        // coordinate — duplicate it so there's still one drawable
        // (zero-length) segment and featureGroup.getBounds() below
        // has something to work with, instead of throwing.
        if (leafletCoordinates.length === 1) {
            leafletCoordinates.push(leafletCoordinates[0]);
        }

        routeLine = L.featureGroup();

        for (
            let i = 0;
            i < leafletCoordinates.length - 1;
            i++
        ) {

            const riskValue =
                Number(segmentRisks[i]) || 1;

            L.polyline(

                [
                    leafletCoordinates[i],
                    leafletCoordinates[i + 1]
                ],

                {

                    color: segmentColor(riskValue),

                    weight: 6,

                    opacity: 0.9

                }

            ).addTo(routeLine);
        }

        routeLine.addTo(FLOODSAFE_MAP);


        // ------------------------------------------------
        // FIT ROUTE
        // ------------------------------------------------

        FLOODSAFE_MAP.fitBounds(

            routeLine.getBounds(),

            {

                padding: [40, 40]

            }

        );


        // ------------------------------------------------
        // RESULT
        // ------------------------------------------------

        const distance =
            Number(
                data.distance_km
            ).toFixed(2);


        const risk =
            data.risk_counts || {};


        const normal = Number(risk["1.0"] || 0);
        const moderate = Number(risk["2.0"] || 0);
        const significant = Number(risk["4.0"] || 0);
        const extreme = Number(risk["8.0"] || 0);

        let riskMessage = "";

        if (mode === "SAFEST" && extreme === 0) {
            riskMessage =
                '<div class="fs-safe-message">' +
                '✓ Safest route avoids Extreme flood-risk roads' +
                '</div>';
        } else if (mode === "SAFEST" && data.extreme_unavoidable) {
            riskMessage =
                '<div class="fs-warning-message">' +
                '⚠ No route avoiding all Extreme-risk roads exists ' +
                'between these points — this is the safest option ' +
                'available (' + extreme + ' Extreme-risk segment(s) ' +
                'are unavoidable here)' +
                '</div>';
        } else if (extreme > 0) {
            riskMessage =
                '<div class="fs-warning-message">' +
                '⚠ This route passes through ' + extreme +
                ' Extreme-risk road segments' +
                '</div>';
        } else {
            riskMessage =
                '<div class="fs-safe-message">' +
                '✓ No Extreme-risk road segments on this route' +
                '</div>';
        }

        function riskRow(riskValue, label, count) {
            return (
                '<div class="fs-risk-row">' +
                '<span class="fs-swatch" style="background:' +
                segmentColor(riskValue) + '"></span>' +
                label + ': ' + count +
                '</div>'
            );
        }

        let extraNotices = "";

        if (data.reported_hazards_on_route > 0) {
            extraNotices +=
                '<div class="fs-warning-message">' +
                '📍 Crosses ' + data.reported_hazards_on_route +
                ' community-reported hazard(s)' +
                '</div>';
        }

        if (data.live_escalation_applied) {
            extraNotices +=
                '<div class="fs-warning-message">' +
                '🌧 Route adjusted for current heavy rainfall' +
                '</div>';
        }

        if (data.safest_capped) {
            extraNotices +=
                '<div class="fs-warning-message">' +
                '⚖ Avoiding every extreme-risk road here would mean ' +
                'a disproportionately long detour, so Safest relaxed ' +
                'to a strongly risk-averse (but not absolute) route' +
                '</div>';
        }

        resultBox.innerHTML =
            '<div class="fs-result-title">Route calculated</div>' +
            '<div class="fs-route-mode">' + escapeHtml(mode) + '</div>' +
            '<div class="fs-distance">' + distance + ' km</div>' +
            riskMessage +
            extraNotices +
            '<div class="fs-risk-title">Route risk breakdown</div>' +
            riskRow(1, "Normal", normal) +
            riskRow(2, "Moderate", moderate) +
            riskRow(4, "Significant", significant) +
            riskRow(8, "Extreme", extreme);

    }


    catch(error) {

        console.error(error);


        resultBox.innerHTML =

            "Route error: "
            +
            escapeHtml(
                error.message || "Something went wrong."
            );

    }
    finally {

        if (calculateBtn) {
            calculateBtn.disabled = false;
        }
    }

}


// =======================================================
// CLEAR ROUTE
// =======================================================

function clearRoute() {

    if (routeLine) {

        FLOODSAFE_MAP.removeLayer(
            routeLine
        );

        routeLine = null;

    }

    if (compareGroup) {

        FLOODSAFE_MAP.removeLayer(
            compareGroup
        );

        compareGroup = null;

    }

    const compareResultBox = document.getElementById("compareResult");

    if (compareResultBox) {
        compareResultBox.innerHTML = "";
    }


    if (currentMarker) {

        FLOODSAFE_MAP.removeLayer(
            currentMarker
        );

        currentMarker = null;

    }


    if (destinationMarker) {

        FLOODSAFE_MAP.removeLayer(
            destinationMarker
        );

        destinationMarker = null;

    }


    currentLocation = null;

    selectedDestination = null;


    document.getElementById(
        "locationStatus"
    ).innerHTML =
        "Enter a location or use GPS";

    document.getElementById(
        "currentLocationInput"
    ).value = "";

    hideAllSearchResults();


    document.getElementById(
        "selectedDestination"
    ).innerHTML =
        "No destination selected";


    hideResults(
        document.getElementById("searchResults")
    );


    document.getElementById(
        "routeResult"
    ).innerHTML = "";

}


// =======================================================
// COMPARE ROUTES
// =======================================================

async function compareRoutes() {

    if (!currentLocation) {
        alert("Please select your current location first.");
        return;
    }

    if (!selectedDestination) {
        alert("Please search and select a destination first.");
        return;
    }

    const resultBox = document.getElementById("compareResult");
    const btn = document.querySelector(".fs-compare-btn");

    resultBox.innerHTML = "Comparing routes...";
    if (btn) { btn.disabled = true; }

    if (routeLine) {
        FLOODSAFE_MAP.removeLayer(routeLine);
        routeLine = null;
    }

    if (compareGroup) {
        FLOODSAFE_MAP.removeLayer(compareGroup);
        compareGroup = null;
    }

    try {

        const response = await fetchWithTimeout(
            "/compare",
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    start_lat: currentLocation.lat,
                    start_lon: currentLocation.lon,
                    end_lat: selectedDestination.lat,
                    end_lon: selectedDestination.lon,
                    live_rain_mm: liveWeather ? liveWeather.total_mm : null
                })
            },
            25000
        );

        let data;

        try {
            data = await response.json();
        } catch (parseError) {
            throw new Error(
                "Server returned an unreadable response " +
                "(status " + response.status + ")."
            );
        }

        if (!response.ok || (data && data.error)) {
            throw new Error(
                (data && (data.details || data.error)) ||
                "Route comparison failed"
            );
        }

        compareGroup = L.featureGroup();

        const fastestLatLngs = data.fastest.coordinates.map(function(p) {
            return [p[1], p[0]];
        });

        L.polyline(fastestLatLngs, {
            color: "#455a64",
            weight: 5,
            opacity: 0.85,
            dashArray: "10, 8"
        }).addTo(compareGroup);

        const safestLatLngs = data.safest.coordinates.map(function(p) {
            return [p[1], p[0]];
        });

        const safestRisks = data.safest.segment_risks || [];

        for (let i = 0; i < safestLatLngs.length - 1; i++) {

            const riskValue = Number(safestRisks[i]) || 1;

            L.polyline(
                [safestLatLngs[i], safestLatLngs[i + 1]],
                {
                    color: segmentColor(riskValue),
                    weight: 5,
                    opacity: 0.9
                }
            ).addTo(compareGroup);
        }

        compareGroup.addTo(FLOODSAFE_MAP);

        FLOODSAFE_MAP.fitBounds(
            compareGroup.getBounds(),
            { padding: [40, 40] }
        );

        const distanceDelta = (
            data.safest.distance_km - data.fastest.distance_km
        ).toFixed(2);

        const fastestExtreme = Number(
            (data.fastest.risk_counts && data.fastest.risk_counts["8.0"]) || 0
        );

        const safestExtreme = Number(
            (data.safest.risk_counts && data.safest.risk_counts["8.0"]) || 0
        );

        resultBox.innerHTML =
            '<div class="fs-result-title">Fastest vs Safest</div>' +
            '<div class="fs-compare-row"><span>⚡ Fastest (dashed)</span><span>' +
            data.fastest.distance_km.toFixed(2) + ' km</span></div>' +
            '<div class="fs-compare-row"><span>🛡 Safest</span><span>' +
            data.safest.distance_km.toFixed(2) + ' km</span></div>' +
            '<div class="fs-compare-row"><span>Extra distance for safety</span><span>+' +
            distanceDelta + ' km</span></div>' +
            '<div class="fs-compare-row"><span>Extreme-risk segments avoided</span><span>' +
            Math.max(fastestExtreme - safestExtreme, 0) + '</span></div>';

    } catch (error) {

        console.error(error);

        resultBox.innerHTML =
            "Comparison error: " +
            escapeHtml(error.message || "Something went wrong.");

    } finally {

        if (btn) { btn.disabled = false; }
    }
}


// =======================================================
// HAZARD REPORTS
// =======================================================

function hazardIcon() {

    return L.divIcon({
        className: "fs-hazard-icon",
        html: "⚠️",
        iconSize: [24, 24],
        iconAnchor: [12, 12]
    });
}

function addHazardMarker(report) {

    const marker = L.marker(
        [report.lat, report.lon],
        { icon: hazardIcon() }
    ).addTo(FLOODSAFE_MAP);

    const placeLabel = report.place_name ?
        report.place_name.split(",").slice(0, 2).join(",") :
        null;

    marker.bindPopup(
        "<b>Reported hazard</b>" +
        (placeLabel ? "<br>" + escapeHtml(placeLabel) : "") +
        "<br>" + escapeHtml(report.description)
    );

    hazardMarkers.push(marker);
}

async function loadReports() {

    try {

        const response = await fetchWithTimeout("/reports", {}, 10000);
        const data = await response.json();

        if (!Array.isArray(data)) {
            return;
        }

        hazardMarkers.forEach(function(marker) {
            FLOODSAFE_MAP.removeLayer(marker);
        });

        hazardMarkers = [];

        data.forEach(addHazardMarker);

    } catch (error) {
        console.error("Failed to load hazard reports:", error);
    }
}

// =======================================================
// RESCUE SHELTERS
// =======================================================

function shelterIcon(kind) {

    const emoji = kind === "hospital" ? "🏥" : "⛺";

    return L.divIcon({
        className: "fs-shelter-icon",
        html: emoji,
        iconSize: [22, 22],
        iconAnchor: [11, 11]
    });
}

async function loadShelters() {

    try {

        const response = await fetchWithTimeout("/shelters", {}, 15000);
        const data = await response.json();

        if (!Array.isArray(data)) {
            return;
        }

        shelterMarkers.forEach(function(marker) {
            FLOODSAFE_MAP.removeLayer(marker);
        });

        shelterMarkers = [];

        data.forEach(function(shelter) {

            const marker = L.marker(
                [shelter.lat, shelter.lon],
                { icon: shelterIcon(shelter.kind) }
            ).addTo(FLOODSAFE_MAP);

            const label = shelter.kind === "hospital" ? "Hospital" : "Shelter";

            marker.bindPopup(
                "<b>" + label + "</b><br>" + escapeHtml(shelter.name)
            );

            shelterMarkers.push(marker);
        });

    } catch (error) {
        console.error("Failed to load shelters:", error);
    }
}


// =======================================================
// EVACUATE TO NEAREST SHELTER
// =======================================================

async function evacuateToShelter() {

    if (!currentLocation) {
        alert("Please select your current location first.");
        return;
    }

    const mode = document.getElementById("routeMode").value;
    const resultBox = document.getElementById("routeResult");
    const btn = document.querySelector(".fs-evacuate-btn");

    resultBox.innerHTML = "Finding nearest shelter...";
    if (btn) { btn.disabled = true; }

    if (routeLine) {
        FLOODSAFE_MAP.removeLayer(routeLine);
        routeLine = null;
    }

    if (compareGroup) {
        FLOODSAFE_MAP.removeLayer(compareGroup);
        compareGroup = null;
    }

    try {

        const response = await fetchWithTimeout(
            "/evacuate",
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    lat: currentLocation.lat,
                    lon: currentLocation.lon,
                    mode: mode,
                    live_rain_mm: liveWeather ? liveWeather.total_mm : null
                })
            },
            25000
        );

        let data;

        try {
            data = await response.json();
        } catch (parseError) {
            throw new Error(
                "Server returned an unreadable response " +
                "(status " + response.status + ")."
            );
        }

        if (!response.ok || (data && data.error)) {
            throw new Error(
                (data && (data.details || data.error)) ||
                "Evacuation routing failed"
            );
        }

        const leafletCoordinates = data.coordinates.map(function(p) {
            return [p[1], p[0]];
        });

        if (leafletCoordinates.length === 1) {
            leafletCoordinates.push(leafletCoordinates[0]);
        }

        const segmentRisks = Array.isArray(data.segment_risks) ?
            data.segment_risks : [];

        routeLine = L.featureGroup();

        for (let i = 0; i < leafletCoordinates.length - 1; i++) {

            const riskValue = Number(segmentRisks[i]) || 1;

            L.polyline(
                [leafletCoordinates[i], leafletCoordinates[i + 1]],
                { color: segmentColor(riskValue), weight: 6, opacity: 0.9 }
            ).addTo(routeLine);
        }

        routeLine.addTo(FLOODSAFE_MAP);

        FLOODSAFE_MAP.fitBounds(
            routeLine.getBounds(),
            { padding: [40, 40] }
        );

        const distance = Number(data.distance_km).toFixed(2);
        const shelterName = data.shelter ?
            escapeHtml(data.shelter.name) : "nearest shelter";

        const risk = data.risk_counts || {};
        const extreme = Number(risk["8.0"] || 0);

        resultBox.innerHTML =
            '<div class="fs-result-title">🚨 Evacuation route</div>' +
            '<div class="fs-route-mode">To: ' + shelterName + '</div>' +
            '<div class="fs-distance">' + distance + ' km</div>' +
            (extreme > 0 ?
                '<div class="fs-warning-message">⚠ Crosses ' + extreme +
                ' Extreme-risk road segment(s)</div>' :
                '<div class="fs-safe-message">✓ No Extreme-risk segments ' +
                'on this route</div>');

    } catch (error) {

        console.error(error);

        resultBox.innerHTML =
            "Evacuation error: " +
            escapeHtml(error.message || "Something went wrong.");

    } finally {

        if (btn) { btn.disabled = false; }
    }
}


let activeReportPopup = null;

function openReportPopup(latlng) {

    const html =
        '<div class="fs-report-popup">' +
        '<div style="font-weight:700; margin-bottom:6px;">' +
        'Report a flooded / blocked road' +
        '</div>' +
        '<textarea id="reportDescInput" ' +
        'placeholder="What did you see? (optional)"></textarea>' +
        '<button id="submitReportBtn">Submit report</button>' +
        '</div>';

    activeReportPopup = L.popup()
        .setLatLng(latlng)
        .setContent(html)
        .openOn(FLOODSAFE_MAP);

    setTimeout(function() {

        const btn = document.getElementById("submitReportBtn");

        if (btn) {
            btn.onclick = function() {
                submitReport(latlng.lat, latlng.lng);
            };
        }

    }, 0);
}

async function submitReport(lat, lon) {

    const input = document.getElementById("reportDescInput");
    const description = input ? input.value.trim() : "";

    try {

        const response = await fetchWithTimeout(
            "/report",
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    lat: lat,
                    lon: lon,
                    description: description
                })
            },
            10000
        );

        let data;

        try {
            data = await response.json();
        } catch (parseError) {
            throw new Error("Server returned an unreadable response.");
        }

        if (!response.ok || data.error) {
            alert((data && data.error) || "Failed to submit report.");
            return;
        }

        addHazardMarker(data);

        if (activeReportPopup) {
            FLOODSAFE_MAP.closePopup(activeReportPopup);
            activeReportPopup = null;
        }

    } catch (error) {
        console.error(error);
        alert("Failed to submit report. Please try again.");
    }
}

document.addEventListener("DOMContentLoaded", function() {

    FLOODSAFE_MAP.on("click", function(e) {
        openReportPopup(e.latlng);
    });

    loadReports();
    loadShelters();

});

</script>
"""


# =========================================================
# INSERT ACTUAL FOLIUM MAP VARIABLE
# =========================================================

javascript = javascript.replace(
    "FLOODSAFE_MAP",
    MAP_VAR
)


m.get_root().html.add_child(
    folium.Element(
        javascript
    )
)


# =========================================================
# LAYER CONTROL
# =========================================================

folium.LayerControl(
    collapsed=False
).add_to(m)


# =========================================================
# SAVE
# =========================================================

print()
print("Saving map...")

m.save(
    OUTPUT_FILE
)


print()
print("========================================")
print("MAP CREATED SUCCESSFULLY")
print("========================================")
print()

print(
    "Output:"
)

print(
    OUTPUT_FILE
)

print()

print(
    "Map variable:"
)

print(
    MAP_VAR
)

print()

print(
    "Open through Flask:"
)

print(
    "http://127.0.0.1:5000/"
)

print()