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

<div style="font-weight:700; margin-top:10px; margin-bottom:4px;">
Locality Hazard Status (live)
</div>

<div><span class="legend-box" style="background:#14532d;"></span>SAFE</div>
<div><span class="legend-box" style="background:#b5860f;"></span>WATCH</div>
<div><span class="legend-box" style="background:#7a1f1f;"></span>CRITICAL</div>

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


    <!-- NEAREST HOSPITAL -->

    <button
        class="fs-hospital-btn"
        onclick="routeToNearestHospital()">

        🏥 Route to Nearest Hospital

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


    <!-- REROUTE NOTICE -->

    <div
        id="rerouteNotice"
        class="fs-warning-message"
        style="display:none;">
    </div>


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
.fs-hospital-btn:disabled,
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


.fs-hospital-btn {

    width: 100%;

    margin-top: 10px;

    padding: 14px;

    border: none;

    border-radius: 10px;

    background: #00838f;

    color: white;

    font-size: 16px;

    font-weight: 700;

    cursor: pointer;

}

.fs-hospital-btn:hover {
    background: #006970;
}

.fs-confirm-count {
    display: block;
    margin-top: 4px;
    font-size: 12px;
    color: #6a4a00;
}

.fs-hazard-actions {
    display: flex;
    gap: 6px;
    margin-top: 8px;
    flex-wrap: wrap;
}

.fs-resolve-btn {
    padding: 6px 10px;
    border: 1px solid #2e7d32;
    border-radius: 8px;
    background: #e8f5e9;
    color: #2e7d32;
    font-size: 12.5px;
    font-weight: 700;
    cursor: pointer;
}

.fs-resolve-btn:hover {
    background: #d5ecd6;
}

.fs-confirm-btn {
    padding: 6px 10px;
    border: 1px solid #b8860b;
    border-radius: 8px;
    background: #fff8e1;
    color: #8a6300;
    font-size: 12.5px;
    font-weight: 700;
    cursor: pointer;
}

.fs-confirm-btn:hover {
    background: #ffedb3;
}

.fs-confirm-btn:disabled {
    opacity: 0.65;
    cursor: default;
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


.fs-report-popup input[type="text"] {
    width: 200px;
    font-size: 13px;
    padding: 6px;
    margin-bottom: 6px;
    border-radius: 6px;
    border: 1px solid #ccc;
    box-sizing: border-box;
    display: block;
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

// Tracks which route action (if any) is currently on screen, so a
// new hazard report — this user's own, or anyone else's picked up
// by polling — can automatically recompute the same thing instead
// of leaving a stale route displayed.
let lastRouteAction = null;
let knownReportIds = null;


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

    // textContent/innerHTML escapes &, < and > but not quote
    // characters (they aren't special in a text node) — escape those
    // too so the result is also safe to use inside an HTML attribute
    // value, not just tag content.
    return div.innerHTML
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
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

        // Called directly from the browser (not proxied through this
        // server) so every visitor uses their own IP against
        // Open-Meteo's free API, instead of all visitors sharing
        // Render's one outbound IP and tripping its rate limit — the
        // same fix already applied to place search/geocoding.
        const response = await fetchWithTimeout(
            "https://api.open-meteo.com/v1/forecast?latitude=" + lat +
            "&longitude=" + lon +
            "&current=precipitation&hourly=precipitation" +
            "&forecast_days=1&timezone=auto",
            {},
            12000
        );

        const payload = await response.json();

        if (!response.ok || !payload.current) {
            box.style.display = "none";
            liveWeather = null;
            return;
        }

        const currentMm = Number(payload.current.precipitation || 0);
        const hourlyTimes = (payload.hourly && payload.hourly.time) || [];
        const hourlyPrecip = (payload.hourly && payload.hourly.precipitation) || [];
        const currentTime = payload.current.time;

        let next3hMm = 0;

        if (currentTime && hourlyTimes.length) {
            let startIndex = hourlyTimes.indexOf(currentTime);
            if (startIndex === -1) startIndex = 0;
            next3hMm = hourlyPrecip
                .slice(startIndex + 1, startIndex + 4)
                .reduce((sum, v) => sum + (Number(v) || 0), 0);
        }

        const totalMm = currentMm + next3hMm;

        // Mirrors RAIN_LOW_THRESHOLD_MM / RAIN_HIGH_THRESHOLD_MM in server.py
        const riskLevel =
            totalMm < 5.0 ? "LOW" :
            totalMm < 15.0 ? "MODERATE" :
            "HIGH";

        const data = {
            status: "ok",
            current_mm: currentMm,
            next_3h_mm: next3hMm,
            total_mm: totalMm,
            risk_level: riskLevel
        };

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


// =======================================================
// CLIENT-SIDE GEOCODING
//
// Search and reverse-geocoding call Nominatim directly from the
// browser instead of proxying through our server. Every visitor
// then uses their own IP, so one busy demo (several people
// searching/reporting at once) can't trip Nominatim's per-IP rate
// limit for everyone at once the way a server-side proxy would.
// =======================================================

const NOMINATIM_BASE = "https://nominatim.openstreetmap.org";

const KNOWN_PLACES = [
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
];

const MIN_SPELLING_SIMILARITY = 0.6;

function levenshteinDistance(a, b) {

    const rows = a.length + 1;
    const cols = b.length + 1;
    const dp = [];

    for (let i = 0; i < rows; i++) {
        dp.push(new Array(cols).fill(0));
        dp[i][0] = i;
    }

    for (let j = 0; j < cols; j++) {
        dp[0][j] = j;
    }

    for (let i = 1; i < rows; i++) {
        for (let j = 1; j < cols; j++) {
            if (a[i - 1] === b[j - 1]) {
                dp[i][j] = dp[i - 1][j - 1];
            } else {
                dp[i][j] = 1 + Math.min(
                    dp[i - 1][j],
                    dp[i][j - 1],
                    dp[i - 1][j - 1]
                );
            }
        }
    }

    return dp[rows - 1][cols - 1];
}

function spellingSimilarity(a, b) {

    const maxLen = Math.max(a.length, b.length);

    if (maxLen === 0) {
        return 1;
    }

    const distance = levenshteinDistance(a.toLowerCase(), b.toLowerCase());

    return 1 - (distance / maxLen);
}

// Best-effort typo fix against KNOWN_PLACES — only corrects the
// first comma-separated token (the actual place name; later tokens
// are usually "India"/district names) and only when it's close
// enough to a known place but not already an exact match.
function correctSpelling(query) {

    const firstToken = query.split(",")[0].trim();

    if (!firstToken) {
        return query;
    }

    let bestMatch = null;
    let bestRatio = 0;

    KNOWN_PLACES.forEach(function(place) {

        const ratio = spellingSimilarity(firstToken, place);

        if (ratio > bestRatio) {
            bestRatio = ratio;
            bestMatch = place;
        }
    });

    if (
        !bestMatch ||
        bestRatio < MIN_SPELLING_SIMILARITY ||
        bestMatch.toLowerCase() === firstToken.toLowerCase()
    ) {
        return query;
    }

    return query.replace(firstToken, bestMatch);
}

// Uttarakhand's bounding box (matches the routing graph's own
// coordinate range) -- with bounded=1, Nominatim only returns
// places actually inside it, since results outside this area
// aren't routable in this app anyway.
const UTTARAKHAND_VIEWBOX = "77.5,31.5,81.1,28.6";

async function nominatimSearch(query) {

    const url = NOMINATIM_BASE + "/search?" + new URLSearchParams({
        q: query + ", Uttarakhand, India",
        format: "jsonv2",
        limit: 5,
        countrycodes: "in",
        addressdetails: 1,
        viewbox: UTTARAKHAND_VIEWBOX,
        bounded: 1
    });

    const response = await fetchWithTimeout(url, {}, 15000);

    if (!response.ok) {
        throw new Error("Search service returned " + response.status);
    }

    const places = await response.json();

    if (!Array.isArray(places)) {
        return [];
    }

    const results = [];

    places.forEach(function(place) {

        const lat = Number(place.lat);
        const lon = Number(place.lon);

        if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
            return;
        }

        results.push({
            lat: lat,
            lon: lon,
            name: place.display_name || place.name || query
        });
    });

    return results;
}

// Returns {results, corrected_from, corrected_to} — same shape the
// old server-side /search endpoint returned, so callers don't need
// to change beyond where they get their data from.
async function geocodePlace(query) {

    let results = await nominatimSearch(query);

    let correctedFrom = null;
    let correctedTo = null;

    if (results.length === 0) {

        const corrected = correctSpelling(query);

        if (corrected !== query) {

            const retryResults = await nominatimSearch(corrected);

            if (retryResults.length > 0) {
                results = retryResults;
                correctedFrom = query;
                correctedTo = corrected;
            }
        }
    }

    return {
        results: results,
        corrected_from: correctedFrom,
        corrected_to: correctedTo
    };
}

// Best-effort place name for a coordinate — used only for display
// (hazard-report list), never blocks or fails the caller.
async function reverseGeocode(lat, lon) {

    try {

        const url = NOMINATIM_BASE + "/reverse?" + new URLSearchParams({
            lat: lat,
            lon: lon,
            format: "jsonv2",
            zoom: 16
        });

        const response = await fetchWithTimeout(url, {}, 8000);

        if (!response.ok) {
            return null;
        }

        const place = await response.json();

        return place.display_name || null;

    } catch (error) {

        console.error("Reverse geocode failed:", error);
        return null;
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

    // Not disabling the input here (unlike a click-triggered search)
    // -- this also runs on every keystroke via debounce, and a
    // disabled field loses focus, kicking the user out mid-typing.

    try {

        const data = await geocodePlace(query);

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

    // Not disabling the input here (unlike a click-triggered search)
    // -- this also runs on every keystroke via debounce, and a
    // disabled field loses focus, kicking the user out mid-typing.

    try {

        const data = await geocodePlace(query);

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

        if (compareGroup) {

            FLOODSAFE_MAP.removeLayer(
                compareGroup
            );

            compareGroup = null;

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

        lastRouteAction = "route";

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

        lastRouteAction = "compare";

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

function getConfirmedReportIds() {

    try {
        const raw = localStorage.getItem("floodsafeConfirmedReports");
        return raw ? JSON.parse(raw) : [];
    } catch (error) {
        return [];
    }
}

function markReportConfirmedLocally(reportId) {

    try {

        const ids = getConfirmedReportIds();

        if (!ids.includes(reportId)) {
            ids.push(reportId);
            localStorage.setItem(
                "floodsafeConfirmedReports",
                JSON.stringify(ids)
            );
        }

    } catch (error) {
        // Private browsing / storage disabled -- just skip remembering it.
    }
}

function hazardPopupHtml(report) {

    const placeLabel = report.place_name ?
        report.place_name.split(",").slice(0, 2).join(",") :
        null;

    const wardLabel = report.nearest_locality ?
        "Near " + escapeHtml(report.nearest_locality.name) + ", " + escapeHtml(report.nearest_locality.town) :
        null;

    const reporterLabel = report.reporter_name ?
        "Reported by " + escapeHtml(report.reporter_name) :
        "Reported anonymously";

    const confirmations = Number(report.confirmations) || 0;
    const alreadyConfirmed = getConfirmedReportIds().includes(report.id);

    const confirmCountText = confirmations > 0 ?
        "Confirmed by " + confirmations +
        (confirmations === 1 ? " other traveler" : " other travelers") :
        "Not yet confirmed by anyone else";

    const confirmBtnHtml = alreadyConfirmed ?
        "<button class='fs-confirm-btn' disabled>✓ You confirmed this</button>" :
        "<button class='fs-confirm-btn'>👍 Still an issue?</button>";

    return (
        "<div class='fs-hazard-popup'>" +
        "<b>Reported hazard</b>" +
        (placeLabel ? "<br>" + escapeHtml(placeLabel) : "") +
        (wardLabel ? "<br><span style='color:#888; font-size:12px;'>" + wardLabel + "</span>" : "") +
        "<br>" + escapeHtml(report.description) +
        "<br><span style='color:#888; font-size:12px;'>" +
        reporterLabel + "</span>" +
        "<br><span class='fs-confirm-count'>" + confirmCountText + "</span>" +
        "<div class='fs-hazard-actions'>" +
        confirmBtnHtml +
        "<button class='fs-resolve-btn'>✓ Road is clear now</button>" +
        "</div>" +
        "</div>"
    );
}

function wireHazardPopupButtons(marker, report) {

    const resolveBtn = document.querySelector(".fs-hazard-popup .fs-resolve-btn");

    if (resolveBtn) {
        resolveBtn.onclick = function() {
            resolveReport(report.id, marker);
        };
    }

    const confirmBtn = document.querySelector(".fs-hazard-popup .fs-confirm-btn");

    if (confirmBtn && !confirmBtn.disabled) {
        confirmBtn.onclick = function() {
            confirmReport(report, marker);
        };
    }
}

function addHazardMarker(report) {

    const marker = L.marker(
        [report.lat, report.lon],
        { icon: hazardIcon() }
    ).addTo(FLOODSAFE_MAP);

    marker.bindPopup(hazardPopupHtml(report));

    marker.on("popupopen", function() {
        wireHazardPopupButtons(marker, report);
    });

    hazardMarkers.push(marker);
}

async function confirmReport(report, marker) {

    try {

        const response = await fetchWithTimeout(
            "/report/" + encodeURIComponent(report.id) + "/confirm",
            { method: "POST" },
            10000
        );

        let data;

        try {
            data = await response.json();
        } catch (parseError) {
            data = null;
        }

        if (!response.ok) {
            alert((data && data.error) || "Failed to confirm this report.");
            return;
        }

        report.confirmations = data.confirmations;
        markReportConfirmedLocally(report.id);

        if (marker && marker.isPopupOpen()) {
            marker.setPopupContent(hazardPopupHtml(report));
            wireHazardPopupButtons(marker, report);
        }

    } catch (error) {
        console.error(error);
        alert("Failed to confirm this report. Please try again.");
    }
}

async function resolveReport(reportId, marker) {

    const confirmed = confirm(
        "Mark this hazard as resolved? It will be removed for everyone " +
        "immediately, and routes will no longer avoid it."
    );

    if (!confirmed) return;

    try {

        const response = await fetchWithTimeout(
            "/report/" + encodeURIComponent(reportId) + "/resolve",
            { method: "POST" },
            10000
        );

        let data;

        try {
            data = await response.json();
        } catch (parseError) {
            data = null;
        }

        if (!response.ok) {
            alert((data && data.error) || "Failed to mark this report resolved.");
            return;
        }

        FLOODSAFE_MAP.closePopup();

        if (marker) {
            FLOODSAFE_MAP.removeLayer(marker);
            hazardMarkers = hazardMarkers.filter(function(m) {
                return m !== marker;
            });
        }

        if (knownReportIds) {
            knownReportIds.delete(reportId);
        }

        if (lastRouteAction) {
            rerunLastRouteAction(
                "✓ Hazard cleared — checking for a better route…"
            );
        }

    } catch (error) {

        console.error(error);
        alert("Failed to mark this report resolved. Please try again.");
    }
}

function showRerouteNotice(text) {

    const el = document.getElementById("rerouteNotice");

    if (!el) return;

    el.textContent = text;
    el.style.display = "block";

    clearTimeout(showRerouteNotice._timer);

    showRerouteNotice._timer = setTimeout(function() {
        el.style.display = "none";
    }, 7000);
}

function rerunLastRouteAction(noticeText) {

    if (!lastRouteAction) return;

    if (noticeText) {
        showRerouteNotice(noticeText);
    }

    if (lastRouteAction === "route") {
        calculateRoute();
    } else if (lastRouteAction === "evacuate") {
        evacuateToShelter();
    } else if (lastRouteAction === "hospital") {
        routeToNearestHospital();
    } else if (lastRouteAction === "compare") {
        compareRoutes();
    }
}

async function loadReports(options) {

    const isPoll = !!(options && options.isPoll);

    try {

        const response = await fetchWithTimeout("/reports", {}, 10000);
        const data = await response.json();

        if (!Array.isArray(data)) {
            return;
        }

        const currentIds = new Set(data.map(function(r) { return r.id; }));

        // knownReportIds starts null so the very first load (page open)
        // never counts as "new/cleared reports appeared" — only a
        // change from an already-established baseline, seen on a
        // later poll, should trigger an automatic reroute.
        let hasNewReport = false;
        let hasRemovedReport = false;

        if (isPoll && knownReportIds) {

            currentIds.forEach(function(id) {
                if (!knownReportIds.has(id)) {
                    hasNewReport = true;
                }
            });

            knownReportIds.forEach(function(id) {
                if (!currentIds.has(id)) {
                    hasRemovedReport = true;
                }
            });
        }

        knownReportIds = currentIds;

        hazardMarkers.forEach(function(marker) {
            FLOODSAFE_MAP.removeLayer(marker);
        });

        hazardMarkers = [];

        data.forEach(addHazardMarker);

        if (hasNewReport && lastRouteAction) {
            rerunLastRouteAction(
                "⚠ New hazard reported nearby — updating your route…"
            );
        } else if (hasRemovedReport && lastRouteAction) {
            rerunLastRouteAction(
                "✓ A nearby hazard was cleared — checking for a better route…"
            );
        }

    } catch (error) {
        console.error("Failed to load hazard reports:", error);
    }
}

// Poll for reports made by other users while this map stays open —
// without this, a hazard someone else reports is invisible until
// the page is reloaded.
setInterval(function() {
    loadReports({ isPoll: true });
}, 30000);

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

            const wardLabel = shelter.nearest_locality ?
                "<br><span style='color:#888; font-size:12px;'>Near " +
                escapeHtml(shelter.nearest_locality.name) + ", " +
                escapeHtml(shelter.nearest_locality.town) + "</span>" :
                "";

            marker.bindPopup(
                "<b>" + label + "</b><br>" + escapeHtml(shelter.name) + wardLabel
            );

            shelterMarkers.push(marker);
        });

    } catch (error) {
        console.error("Failed to load shelters:", error);
    }
}


// =======================================================
// LOCALITY HAZARD LAYER (ward-level granularity)
//
// Shows the same real, named localities as the Flash Flood Guidance
// System (/ffgs) -- OSM place=suburb/neighbourhood/quarter/village/
// hamlet nodes the hazard atlas actually covers -- as a live-status
// layer on the routing map itself, so route planning has the same
// neighborhood-level context FFGS shows on its own page. Both zone
// metadata AND live rainfall come from this same server's
// /ffgs/zones, which caches one server-side Open-Meteo call for
// every visitor (see _fetch_ffgs_live_rainfall in server.py) rather
// than each visitor's browser re-fetching the same data -- that used
// to be client-side here too, but N visitors independently hitting
// Open-Meteo for identical data burns through its free-tier daily
// quota fast, especially from a shared network.
// =======================================================

let localityMarkers = [];

function localityStatusForDuration(rainMm, thresholds) {
    if (rainMm == null || !thresholds) return null;
    if (rainMm >= thresholds.critical) return "CRITICAL";
    if (rainMm >= thresholds.watch) return "WATCH";
    return "SAFE";
}

function localityWorseStatus(a, b) {
    const order = { SAFE: 0, WATCH: 1, CRITICAL: 2 };
    if (!a) return b;
    if (!b) return a;
    return order[a] >= order[b] ? a : b;
}

function localityStatusColor(status) {
    if (status === "CRITICAL") return "#7a1f1f";
    if (status === "WATCH") return "#b5860f";
    if (status === "SAFE") return "#14532d";
    return "#6b7680";
}

async function loadLocalityZones() {

    try {

        const response = await fetchWithTimeout("/ffgs/zones", {}, 15000);
        const data = await response.json();

        if (!data.available || !Array.isArray(data.zones)) {
            return;
        }

        // effective_class, not hazard_class: localities outside the
        // hazard atlas now get a class from FFPI, and filtering on the
        // atlas class would keep dropping them from this layer.
        const localities = data.zones.filter(function(z) { return z.kind === "locality" && z.effective_class; });
        const durations = data.durations || ["1h", "3h", "24h"];

        localityMarkers.forEach(function(marker) {
            FLOODSAFE_MAP.removeLayer(marker);
        });
        localityMarkers = [];

        localities.forEach(function(zone) {

            const rain = zone.live_rainfall || null;
            let overall = null;

            const durationText = durations.map(function(d) {
                const rainMm = rain ? rain[d] : null;
                const thresholds = zone.thresholds_mm ? zone.thresholds_mm[d] : null;
                const status = localityStatusForDuration(rainMm, thresholds);
                overall = localityWorseStatus(overall, status);
                const rainText = rainMm == null ? "—" : rainMm.toFixed(1) + " mm";
                return d + ": " + rainText;
            }).join(" · ");

            const color = localityStatusColor(overall);

            const marker = L.circleMarker([zone.lat, zone.lon], {
                radius: 7, color: color, fillColor: color, fillOpacity: 0.85, weight: 2
            }).addTo(FLOODSAFE_MAP);

            marker.bindPopup(
                "<b>" + escapeHtml(zone.name) + "</b> — " + escapeHtml(zone.parent_town) + "<br>" +
                escapeHtml(zone.effective_class) + " hazard" +
                (zone.hazard_source === "ffpi" ? " (modelled, FFPI " + zone.ffpi.toFixed(1) + ")" : "") +
                " · " + (overall || "—") + "<br>" +
                durationText + "<br>" +
                '<a href="/ffgs" target="_blank">Full Flash Flood Guidance System →</a>'
            );

            localityMarkers.push(marker);
        });

    } catch (error) {
        console.error("Failed to load locality hazard zones:", error);
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

        lastRouteAction = "evacuate";

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


// =======================================================
// ROUTE TO NEAREST HOSPITAL
// =======================================================

async function routeToNearestHospital() {

    if (!currentLocation) {
        alert("Please select your current location first.");
        return;
    }

    const mode = document.getElementById("routeMode").value;
    const resultBox = document.getElementById("routeResult");
    const btn = document.querySelector(".fs-hospital-btn");

    resultBox.innerHTML = "Finding nearest hospital...";
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
            "/nearest-hospital",
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
                "Hospital routing failed"
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
        const hospitalName = data.hospital ?
            escapeHtml(data.hospital.name) : "nearest hospital";

        const risk = data.risk_counts || {};
        const extreme = Number(risk["8.0"] || 0);

        lastRouteAction = "hospital";

        resultBox.innerHTML =
            '<div class="fs-result-title">🏥 Hospital route</div>' +
            '<div class="fs-route-mode">To: ' + hospitalName + '</div>' +
            '<div class="fs-distance">' + distance + ' km</div>' +
            (extreme > 0 ?
                '<div class="fs-warning-message">⚠ Crosses ' + extreme +
                ' Extreme-risk road segment(s)</div>' :
                '<div class="fs-safe-message">✓ No Extreme-risk segments ' +
                'on this route</div>');

    } catch (error) {

        console.error(error);

        resultBox.innerHTML =
            "Hospital routing error: " +
            escapeHtml(error.message || "Something went wrong.");

    } finally {

        if (btn) { btn.disabled = false; }
    }
}


let activeReportPopup = null;

function getSavedReporterName() {

    try {
        return localStorage.getItem("floodsafeReporterName") || "";
    } catch (error) {
        return "";
    }
}

function saveReporterName(name) {

    try {
        localStorage.setItem("floodsafeReporterName", name);
    } catch (error) {
        // Private browsing / storage disabled -- just skip remembering it.
    }
}

function openReportPopup(latlng) {

    const savedName = escapeHtml(getSavedReporterName());

    const html =
        '<div class="fs-report-popup">' +
        '<div style="font-weight:700; margin-bottom:6px;">' +
        'Report a flooded / blocked road' +
        '</div>' +
        '<input id="reportNameInput" type="text" ' +
        'placeholder="Your name (optional)" value="' + savedName + '">' +
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

    const descInput = document.getElementById("reportDescInput");
    const description = descInput ? descInput.value.trim() : "";

    const nameInput = document.getElementById("reportNameInput");
    const reporterName = nameInput ? nameInput.value.trim() : "";

    saveReporterName(reporterName);

    try {

        // Resolved in the browser (own IP), not proxied through our
        // server — see the CLIENT-SIDE GEOCODING block above.
        const placeName = await reverseGeocode(lat, lon);

        const response = await fetchWithTimeout(
            "/report",
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    lat: lat,
                    lon: lon,
                    description: description,
                    place_name: placeName,
                    reporter_name: reporterName
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

        if (knownReportIds) {
            knownReportIds.add(data.id);
        }

        if (activeReportPopup) {
            FLOODSAFE_MAP.closePopup(activeReportPopup);
            activeReportPopup = null;
        }

        if (lastRouteAction) {

            // The reporter is presumably at (or right next to) the
            // point they just reported — they're on this road, seeing
            // the hazard right now — so the reroute should start from
            // there, not from wherever "Current Location" was set at
            // the beginning of the trip. Without this, reporting a
            // hazard 20km into a drive would recompute the route from
            // the original starting point instead of from here.
            setCurrentLocation(lat, lon, placeName);

            rerunLastRouteAction(
                "✓ Report submitted — recalculating your route from here…"
            );
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
    loadLocalityZones();

    // Rainfall itself now comes from /ffgs/zones (server-cached, see
    // _fetch_ffgs_live_rainfall in server.py) rather than a client-side
    // Open-Meteo call, so this 60s poll is just a same-origin request —
    // no external rate-limit exposure at all, regardless of visitor count.
    setInterval(loadLocalityZones, 60000);

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