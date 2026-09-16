import os
import json
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

    try:

        with open(REPORTS_FILE, "w", encoding="utf-8") as f:
            json.dump(reports, f)

    except OSError as e:

        print("WARNING: failed to save reports.json:", repr(e))


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

        _reports[:] = fresh
        _save_reports(_reports)

    return fresh


# ============================================================
# LANDING PAGE
#
# The site's front door. Pulls live numbers from this same
# server's own endpoints (/status, /shelters, /reports, /weather)
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

</style>
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
    finalTitle: "Access the flood-aware map tool",
    finalText: "No registration required. Available to all road users in Uttarakhand.",
    finalCta: "Open FloodSafe Map Tool →",
    footerMap: "Map Tool",
    footerReports: "Hazard Reports",
    footerStatus: "System Status (API)",
    footerTagline: "FloodSafe — Flood-Aware Road Advisory Service for Uttarakhand.",
    footerDisclaimer: "FloodSafe is an independent citizen-safety project and is not an official service of the Government of Uttarakhand or the Government of India. Hazard classifications are derived from published government flash-flood hazard data; road conditions should always be independently verified before travel, particularly during active monsoon or alert conditions."
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
    finalTitle: "बाढ़-जागरूक मानचित्र टूल खोलें",
    finalText: "कोई पंजीकरण आवश्यक नहीं। उत्तराखंड के सभी सड़क उपयोगकर्ताओं के लिए उपलब्ध।",
    finalCta: "FloodSafe मानचित्र टूल खोलें →",
    footerMap: "मानचित्र टूल",
    footerReports: "खतरा रिपोर्ट",
    footerStatus: "सिस्टम स्थिति (API)",
    footerTagline: "FloodSafe — उत्तराखंड के लिए बाढ़-जागरूक सड़क परामर्श सेवा।",
    footerDisclaimer: "FloodSafe एक स्वतंत्र नागरिक-सुरक्षा परियोजना है और यह उत्तराखंड सरकार या भारत सरकार की कोई आधिकारिक सेवा नहीं है। खतरा वर्गीकरण प्रकाशित सरकारी बाढ़ खतरा डेटा से लिया गया है; यात्रा से पहले सड़क की स्थिति की हमेशा स्वतंत्र रूप से पुष्टि करें, विशेष रूप से सक्रिय मानसून या चेतावनी की स्थिति के दौरान।"
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

// ---- Dynamic (fetched) text that also needs to track the current language ----

let statusStateKey = null;
let weatherStateKey = null;

function renderDynamicText() {
    if (statusStateKey) {
        document.getElementById('statusText').textContent = t(statusStateKey);
    }
    if (weatherStateKey) {
        document.getElementById('statWeatherWet').textContent = t(weatherStateKey);
        document.getElementById('statWeatherDry').textContent = t(weatherStateKey);
        document.getElementById('statWeatherWetName').textContent = '';
        document.getElementById('statWeatherDryName').textContent = '';
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

function animateCount(el, target, suffix, duration) {
    suffix = suffix || '';
    duration = duration || 700;
    const start = performance.now();
    function tick(now) {
        const progress = Math.min((now - start) / duration, 1);
        const value = Math.round(target * progress);
        el.textContent = value + suffix;
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
    } catch (error) {
        document.getElementById('statusDot').classList.add('off');
        statusStateKey = 'statusUnreachable';
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
        // Called directly from the browser, not proxied through this
        // server — see fetchLiveConditions() in map_app.py for why.
        // Checking several towns spread across the state (rather than
        // one fixed point) and showing the current wettest and driest
        // side by side makes it obvious this is a live reading, not a
        // hard-coded number — if it isn't raining anywhere right now,
        // both will genuinely show 0.0mm rather than looking stuck.
        const results = await Promise.all(RAINFALL_STATIONS.map(function(station) {
            return fetch(
                'https://api.open-meteo.com/v1/forecast?latitude=' + station.lat +
                '&longitude=' + station.lon +
                '&current=precipitation&hourly=precipitation&forecast_days=1&timezone=auto'
            )
                .then(function(r) { return r.json(); })
                .then(function(payload) {
                    if (!payload || !payload.current) return null;
                    // Current conditions only — not blended with the next
                    // few hours' forecast. This figure exists specifically
                    // to demonstrate live data, so it needs to match what
                    // a visitor can independently verify is happening
                    // right now (e.g. against any weather app), not read
                    // as "raining" purely because rain is forecast soon.
                    const currentMm = Number(payload.current.precipitation || 0);
                    return { name: station.name, mm: currentMm };
                })
                .catch(function() { return null; });
        }));

        const valid = results.filter(function(r) { return r !== null; });

        if (valid.length > 0) {
            valid.sort(function(a, b) { return b.mm - a.mm; });
            const wettest = valid[0];
            const driest = valid[valid.length - 1];

            document.getElementById('statWeatherWet').textContent = wettest.mm.toFixed(1) + ' mm';
            document.getElementById('statWeatherWetName').textContent = '(' + wettest.name + ')';
            document.getElementById('statWeatherDry').textContent = driest.mm.toFixed(1) + ' mm';
            document.getElementById('statWeatherDryName').textContent = '(' + driest.name + ')';

            weatherStateKey = null;
        } else {
            weatherStateKey = 'weatherUnavailable';
        }
    } catch (error) {
        weatherStateKey = 'weatherUnavailable';
    }
    renderDynamicText();

    document.getElementById('statNodes').textContent = '2,021,505';
}

loadLiveStrip();

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
        "routing_error": None if ROUTING_ENGINE_AVAILABLE else ROUTING_ENGINE_ERROR
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

            const marker = L.marker([report.lat, report.lon]).addTo(map);

            marker.bindPopup(
                "<b>" + escapeHtml(label) + "</b><br>" +
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
# LIVE WEATHER
#
# Proxies Open-Meteo (no API key required) so the frontend can
# show current conditions and the router can become more
# cautious when it's actually raining, without either of them
# calling a third-party API directly from the browser.
#
# Cached briefly per rounded coordinate (~1km) because Render's
# free tier shares one outbound IP across tenants, and Open-Meteo
# rate-limits (429) that shared IP once request volume climbs —
# the same failure mode hit earlier with Nominatim. Weather doesn't
# need second-by-second freshness, so a short TTL cache avoids
# almost all real outbound calls. On a failed refresh, a stale
# cached value is served instead of erroring out.
# ============================================================

RAIN_LOW_THRESHOLD_MM = 5.0
RAIN_HIGH_THRESHOLD_MM = 15.0

WEATHER_CACHE_TTL_SECONDS = 10 * 60
_weather_cache = {}


def _weather_cache_key(lat, lon):
    return (round(lat, 2), round(lon, 2))


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

    cache_key = _weather_cache_key(lat, lon)
    cached = _weather_cache.get(cache_key)
    now = time.time()

    if cached and (now - cached["timestamp"]) < WEATHER_CACHE_TTL_SECONDS:
        return jsonify(cached["data"])

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

        result = {
            "status": "ok",
            "current_mm": current_mm,
            "next_3h_mm": next_3h_mm,
            "total_mm": total_mm,
            "risk_level": risk_level
        }

        _weather_cache[cache_key] = {"timestamp": now, "data": result}

        return jsonify(result)

    except requests.exceptions.Timeout:

        if cached:
            return jsonify(cached["data"])

        return jsonify({
            "error": "Weather request timed out."
        }), 504

    except requests.exceptions.RequestException as e:

        if cached:
            return jsonify(cached["data"])

        return jsonify({
            "error": "Weather request failed.",
            "details": str(e)
        }), 502

    except Exception as e:

        if cached:
            return jsonify(cached["data"])

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