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
<title>FloodSafe — flood-aware navigation for Uttarakhand</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,600;12..96,700;12..96,800&family=Source+Sans+3:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>

:root {
  --ink: #0f2027;
  --ink-2: #16323b;
  --paper: #f5f8f7;
  --paper-raised: #ffffff;
  --line: #dbe4e2;
  --text: #16232a;
  --text-muted: #57696c;
  --accent: #1f7a8c;
  --accent-strong: #145a68;
  --accent-tint: #e3f1f2;
  --amber: #c76a2e;
  --amber-tint: #fbead9;
  --hz-low: #4c8c4a;
  --hz-mod: #c99a2e;
  --hz-sig: #cf7a2a;
  --hz-ext: #b0402e;
  --shadow: 0 1px 2px rgba(15,32,39,0.05), 0 10px 30px rgba(15,32,39,0.08);
}

@media (prefers-color-scheme: dark) {
  :root {
    --ink: #eef3f2;
    --ink-2: #cfdbd9;
    --paper: #0c1517;
    --paper-raised: #121e21;
    --line: #223234;
    --text: #e6ede9;
    --text-muted: #93a5a3;
    --accent: #5fb2c2;
    --accent-strong: #86c9d6;
    --accent-tint: #142a2d;
    --amber: #d99b5f;
    --amber-tint: #2a2015;
    --hz-low: #6fae6b;
    --hz-mod: #d9b354;
    --hz-sig: #dd9455;
    --hz-ext: #d1685a;
    --shadow: 0 1px 2px rgba(0,0,0,0.3), 0 12px 30px rgba(0,0,0,0.4);
  }
}

* { box-sizing: border-box; }
html { scroll-behavior: smooth; }

body {
  margin: 0;
  background: var(--paper);
  color: var(--text);
  font-family: 'Source Sans 3', -apple-system, BlinkMacSystemFont, sans-serif;
  font-size: 16.5px;
  line-height: 1.6;
}

h1, h2, h3 {
  font-family: 'Bricolage Grotesque', 'Source Sans 3', sans-serif;
  color: var(--ink);
  text-wrap: balance;
  margin: 0;
}

code, .mono { font-family: 'IBM Plex Mono', ui-monospace, monospace; }

a { color: var(--accent-strong); }

.wrap { max-width: 1100px; margin: 0 auto; padding: 0 28px; }

/* ---------------- NAV ---------------- */

nav {
  position: sticky; top: 0; z-index: 20;
  background: color-mix(in srgb, var(--paper) 88%, transparent);
  backdrop-filter: blur(10px);
  border-bottom: 1px solid var(--line);
}

nav .wrap {
  display: flex; align-items: center; justify-content: space-between;
  padding-top: 16px; padding-bottom: 16px;
}

.brand {
  display: flex; align-items: center; gap: 9px;
  font-family: 'Bricolage Grotesque', sans-serif;
  font-weight: 700; font-size: 19px; color: var(--ink);
  text-decoration: none;
}

.brand .mark {
  width: 26px; height: 26px; border-radius: 7px;
  background: linear-gradient(155deg, var(--accent), var(--accent-strong));
  display: inline-flex; align-items: center; justify-content: center;
  color: white; font-size: 14px;
}

.nav-links { display: flex; align-items: center; gap: 22px; }
.nav-links a.plain { color: var(--text-muted); text-decoration: none; font-size: 14.5px; }
.nav-links a.plain:hover { color: var(--accent-strong); }

.btn-primary {
  background: var(--ink);
  color: var(--paper) !important;
  padding: 10px 18px;
  border-radius: 8px;
  text-decoration: none;
  font-weight: 600;
  font-size: 14.5px;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  transition: transform .15s ease, box-shadow .15s ease;
}
.btn-primary:hover { transform: translateY(-1px); box-shadow: var(--shadow); }

/* ---------------- HERO ---------------- */

.hero {
  padding: 68px 0 40px;
  border-bottom: 1px solid var(--line);
}

.hero-grid {
  display: grid;
  grid-template-columns: 1.15fr 0.85fr;
  gap: 48px;
  align-items: center;
}

.eyebrow {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 12.5px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--accent-strong);
  background: var(--accent-tint);
  display: inline-block;
  padding: 5px 11px;
  border-radius: 20px;
  margin-bottom: 18px;
}

.hero h1 {
  font-size: 50px;
  line-height: 1.06;
  font-weight: 700;
  letter-spacing: -0.01em;
}

.hero h1 em {
  font-style: normal;
  color: var(--accent-strong);
}

.hero p.sub {
  font-size: 18px;
  color: var(--text-muted);
  max-width: 480px;
  margin: 18px 0 28px;
}

.hero-ctas { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }

.btn-ghost {
  color: var(--text);
  text-decoration: none;
  font-weight: 600;
  font-size: 14.5px;
  padding: 10px 4px;
  border-bottom: 2px solid var(--line);
}
.btn-ghost:hover { border-bottom-color: var(--accent); }

.hero-art {
  background: var(--paper-raised);
  border: 1px solid var(--line);
  border-radius: 16px;
  box-shadow: var(--shadow);
  padding: 20px;
  aspect-ratio: 1 / 0.95;
  position: relative;
  overflow: hidden;
}

.hero-art svg { width: 100%; height: 100%; }

.hero-art .cap {
  position: absolute; bottom: 14px; left: 20px; right: 20px;
  font-family: 'IBM Plex Mono', monospace;
  font-size: 11.5px;
  color: var(--text-muted);
  background: color-mix(in srgb, var(--paper-raised) 85%, transparent);
  padding: 6px 0;
}

/* ---------------- LIVE STRIP ---------------- */

.live-strip {
  padding: 26px 0;
  border-bottom: 1px solid var(--line);
}

.live-strip-head {
  display: flex; align-items: center; gap: 9px;
  font-family: 'IBM Plex Mono', monospace;
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--text-muted);
  margin-bottom: 16px;
}

.pulse-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--hz-low);
  box-shadow: 0 0 0 0 color-mix(in srgb, var(--hz-low) 60%, transparent);
  animation: pulse 2.2s infinite;
}
.pulse-dot.off { background: var(--hz-ext); animation: none; }

@keyframes pulse {
  0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--hz-low) 45%, transparent); }
  70% { box-shadow: 0 0 0 8px transparent; }
  100% { box-shadow: 0 0 0 0 transparent; }
}

.live-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 1px;
  background: var(--line);
  border: 1px solid var(--line);
  border-radius: 12px;
  overflow: hidden;
}

.live-tile {
  background: var(--paper-raised);
  padding: 18px 18px 16px;
}

.live-tile .lv-num {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 26px;
  font-weight: 600;
  color: var(--ink);
  font-variant-numeric: tabular-nums;
  min-height: 32px;
}

.live-tile .lv-label {
  font-size: 12.5px;
  color: var(--text-muted);
  margin-top: 4px;
}

.skeleton { opacity: 0.35; }

/* ---------------- HAZARD LEGEND ---------------- */

.legend-band {
  display: flex; align-items: center; gap: 22px; flex-wrap: wrap;
  padding: 20px 0;
  border-bottom: 1px solid var(--line);
  font-family: 'IBM Plex Mono', monospace;
  font-size: 12.5px;
  color: var(--text-muted);
}

.legend-band .lb-label { color: var(--ink); font-weight: 600; margin-right: 4px; }
.legend-sw { display: inline-flex; align-items: center; gap: 6px; }
.legend-sw .dot { width: 10px; height: 10px; border-radius: 3px; }

/* ---------------- STORY ---------------- */

.story {
  padding: 56px 0;
  border-bottom: 1px solid var(--line);
}

.story-grid {
  display: grid;
  grid-template-columns: 0.9fr 1.1fr;
  gap: 44px;
  align-items: start;
}

.section-kicker {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 12px;
  color: var(--amber);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  margin-bottom: 10px;
}

.story h2 { font-size: 30px; margin-bottom: 14px; }
.story p { color: var(--text-muted); font-size: 15.5px; }

.compare-card {
  background: var(--paper-raised);
  border: 1px solid var(--line);
  border-radius: 14px;
  box-shadow: var(--shadow);
  padding: 22px 24px;
}

.compare-card .route-label {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 12px;
  color: var(--text-muted);
  margin-bottom: 14px;
}

.compare-rows { display: flex; flex-direction: column; gap: 10px; }

.compare-row {
  display: grid;
  grid-template-columns: 90px 1fr 70px;
  align-items: center;
  gap: 12px;
  font-size: 13.5px;
}

.compare-row .bar-track {
  background: var(--line);
  border-radius: 6px;
  height: 22px;
  position: relative;
  overflow: hidden;
}

.compare-row .bar-fill {
  position: absolute; left: 0; top: 0; bottom: 0;
  border-radius: 6px;
  display: flex; align-items: center;
  padding-left: 8px;
  font-family: 'IBM Plex Mono', monospace;
  font-size: 11px;
  color: white;
  white-space: nowrap;
}

.compare-row.fastest .bar-fill { background: var(--hz-ext); }
.compare-row.safest .bar-fill { background: var(--accent); }

.compare-row .val { font-family: 'IBM Plex Mono', monospace; text-align: right; font-weight: 600; color: var(--ink); }

.compare-foot {
  margin-top: 14px; padding-top: 14px; border-top: 1px dashed var(--line);
  font-size: 12.5px; color: var(--text-muted); font-family: 'IBM Plex Mono', monospace;
}

/* ---------------- FEATURES ---------------- */

.features { padding: 56px 0; border-bottom: 1px solid var(--line); }

.features h2 { font-size: 30px; margin-bottom: 8px; }
.features > .wrap > p { color: var(--text-muted); margin: 0 0 32px; font-size: 15.5px; max-width: 560px; }

.feature-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 16px;
}

.feature-card {
  background: var(--paper-raised);
  border: 1px solid var(--line);
  border-radius: 14px;
  padding: 22px;
  box-shadow: var(--shadow);
}

.feature-card .ic {
  width: 38px; height: 38px; border-radius: 10px;
  background: var(--accent-tint);
  display: flex; align-items: center; justify-content: center;
  font-size: 18px; margin-bottom: 14px;
}

.feature-card h3 { font-size: 16.5px; font-family: 'Source Sans 3',sans-serif; font-weight: 700; margin-bottom: 6px; }
.feature-card p { font-size: 14px; color: var(--text-muted); margin: 0; }

/* ---------------- SOURCES ---------------- */

.sources-band {
  padding: 40px 0;
  border-bottom: 1px solid var(--line);
}

.sources-band .sb-label {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 11.5px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--text-muted);
  margin-bottom: 16px;
}

.pill-row { display: flex; gap: 10px; flex-wrap: wrap; }

.src-pill {
  background: var(--paper-raised);
  border: 1px solid var(--line);
  border-radius: 20px;
  padding: 7px 14px;
  font-size: 13px;
  color: var(--text-muted);
  display: flex; align-items: center; gap: 7px;
}
.src-pill b { color: var(--ink); font-weight: 600; }

/* ---------------- FINAL CTA ---------------- */

.final-cta {
  padding: 64px 0 56px;
  text-align: center;
}

.final-cta h2 { font-size: 32px; margin-bottom: 12px; }
.final-cta p { color: var(--text-muted); margin: 0 0 26px; }

footer {
  border-top: 1px solid var(--line);
  padding: 26px 0 40px;
  display: flex; justify-content: space-between; align-items: center;
  flex-wrap: wrap; gap: 10px;
  font-size: 13px; color: var(--text-muted);
}

footer a { color: var(--text-muted); }

@media (max-width: 860px) {
  .hero-grid { grid-template-columns: 1fr; }
  .hero h1 { font-size: 38px; }
  .live-grid { grid-template-columns: repeat(2, 1fr); }
  .story-grid { grid-template-columns: 1fr; }
  .feature-grid { grid-template-columns: 1fr; }
  .nav-links a.plain { display: none; }
}

</style>
</head>
<body>

<nav>
  <div class="wrap">
    <a class="brand" href="/"><span class="mark">🌊</span> FloodSafe</a>
    <div class="nav-links">
      <a class="plain" href="#live">Live status</a>
      <a class="plain" href="#features">Features</a>
      <a class="plain" href="/reports-view">Reports</a>
      <a class="btn-primary" href="/app">Launch app →</a>
    </div>
  </div>
</nav>

<header class="hero">
  <div class="wrap hero-grid">
    <div>
      <span class="eyebrow">Uttarakhand &middot; live routing</span>
      <h1>Fastest isn't<br>always <em>safe</em>.</h1>
      <p class="sub">FloodSafe routes you around flood-hazard roads using a real government hazard atlas, not just distance &mdash; on a live graph of 2 million road nodes across Uttarakhand.</p>
      <div class="hero-ctas">
        <a class="btn-primary" href="/app">Open the map →</a>
        <a class="btn-ghost" href="#live">See it working live ↓</a>
      </div>
    </div>
    <div class="hero-art">
      <svg viewBox="0 0 340 300" xmlns="http://www.w3.org/2000/svg">
        <rect x="0" y="0" width="340" height="300" fill="none"/>
        <path d="M20 250 Q 90 180 140 200 T 260 130 Q 300 100 320 60" fill="none" stroke="var(--hz-ext)" stroke-width="4" stroke-linecap="round" opacity="0.85"/>
        <path d="M20 250 Q 70 230 100 245 Q 150 270 180 230 Q 210 190 190 150 Q 170 105 210 85 Q 260 60 320 60" fill="none" stroke="var(--accent)" stroke-width="4" stroke-linecap="round"/>
        <circle cx="20" cy="250" r="7" fill="var(--ink)"/>
        <circle cx="320" cy="60" r="7" fill="var(--ink)"/>
        <circle cx="190" cy="150" r="5" fill="var(--hz-ext)"/>
        <text x="150" y="140" font-family="IBM Plex Mono, monospace" font-size="10" fill="var(--hz-ext)">EXTREME risk</text>
        <text x="14" y="270" font-family="IBM Plex Mono, monospace" font-size="10" fill="var(--text-muted)">start</text>
        <text x="285" y="50" font-family="IBM Plex Mono, monospace" font-size="10" fill="var(--text-muted)">destination</text>
      </svg>
      <div class="cap">two real routes, same start and end &mdash; red crosses risk, teal avoids it</div>
    </div>
  </div>
</header>

<section class="live-strip" id="live">
  <div class="wrap">
    <div class="live-strip-head"><span class="pulse-dot" id="statusDot"></span> <span id="statusText">Checking live status…</span></div>
    <div class="live-grid">
      <div class="live-tile"><div class="lv-num skeleton" id="statNodes">—</div><div class="lv-label">road nodes in the graph</div></div>
      <div class="live-tile"><div class="lv-num skeleton" id="statShelters">—</div><div class="lv-label">shelters &amp; hospitals mapped</div></div>
      <div class="live-tile"><div class="lv-num skeleton" id="statReports">—</div><div class="lv-label">active hazard reports right now</div></div>
      <div class="live-tile"><div class="lv-num skeleton" id="statWeather">—</div><div class="lv-label">live rainfall, Dehradun</div></div>
    </div>
  </div>
</section>

<div class="legend-band wrap">
  <span class="lb-label">Hazard classes:</span>
  <span class="legend-sw"><span class="dot" style="background:var(--hz-low)"></span>LOW</span>
  <span class="legend-sw"><span class="dot" style="background:var(--hz-mod)"></span>MODERATE</span>
  <span class="legend-sw"><span class="dot" style="background:var(--hz-sig)"></span>SIGNIFICANT</span>
  <span class="legend-sw"><span class="dot" style="background:var(--hz-ext)"></span>EXTREME</span>
  <span style="margin-left:auto; color:var(--text-muted);">from a georeferenced state flash-flood hazard atlas</span>
</div>

<section class="story">
  <div class="wrap story-grid">
    <div>
      <div class="section-kicker">Real example</div>
      <h2>It knows when a detour isn't worth it</h2>
      <p>Between Pachora and Chamun in Pithoragarh district, the direct road crosses 10 extreme-risk segments. Safest mode reroutes around all of them for a reasonable 32% longer trip &mdash; a genuine, different path, not just a warning label.</p>
      <p>But it also knows when to stop. On a different route where avoiding risk would triple the distance, Safest refuses the detour and says so, instead of sending you on an hour-long loop to dodge 300 metres of bad road.</p>
    </div>
    <div class="compare-card">
      <div class="route-label">PACHORA → CHAMUN, PITHORAGARH DISTRICT</div>
      <div class="compare-rows">
        <div class="compare-row fastest">
          <div>⚡ Fastest</div>
          <div class="bar-track"><div class="bar-fill" style="width:76%">10 extreme-risk segments</div></div>
          <div class="val">90.2 km</div>
        </div>
        <div class="compare-row safest">
          <div>🛡 Safest</div>
          <div class="bar-track"><div class="bar-fill" style="width:100%">0 extreme-risk segments</div></div>
          <div class="val">119.3 km</div>
        </div>
      </div>
      <div class="compare-foot">+29 km to eliminate every extreme-risk segment on the route</div>
    </div>
  </div>
</section>

<section class="features" id="features">
  <div class="wrap">
    <div class="section-kicker">What it does</div>
    <h2>Built for the moment it actually rains</h2>
    <p>Every feature exists because a static hazard map alone isn't enough once water is actually rising.</p>
    <div class="feature-grid">
      <div class="feature-card">
        <div class="ic">🛡</div>
        <h3>Risk-weighted routing</h3>
        <p>Fastest and Safest run on the same graph, weighted by real per-road hazard scores &mdash; not a flat "avoid this area" toggle.</p>
      </div>
      <div class="feature-card">
        <div class="ic">⛺</div>
        <h3>Evacuate to nearest shelter</h3>
        <p>Shortlists the closest OSM-tagged shelters, then routes to each and picks the shortest real trip &mdash; not just the nearest pin on the map.</p>
      </div>
      <div class="feature-card">
        <div class="ic">🌧</div>
        <h3>Live rainfall awareness</h3>
        <p>Current and forecast rain feed directly into Safest's routing weights, not just a banner &mdash; the route itself gets more cautious while it's raining.</p>
      </div>
      <div class="feature-card">
        <div class="ic">🚨</div>
        <h3>Crowdsourced hazard reports</h3>
        <p>Anyone can flag a flooded or blocked road from the map. Reports hard-block that road for every routing mode and expire automatically after 6 hours.</p>
      </div>
    </div>
  </div>
</section>

<section class="sources-band">
  <div class="wrap">
    <div class="sb-label">Built on</div>
    <div class="pill-row">
      <span class="src-pill"><b>Hazard data</b> &middot; state flash-flood atlas</span>
      <span class="src-pill"><b>Roads</b> &middot; OpenStreetMap</span>
      <span class="src-pill"><b>Weather</b> &middot; Open-Meteo</span>
      <span class="src-pill"><b>Search</b> &middot; Nominatim</span>
      <span class="src-pill"><b>Engine</b> &middot; SciPy sparse Dijkstra</span>
    </div>
  </div>
</section>

<section class="final-cta">
  <div class="wrap">
    <h2>Uttarakhand's roads, mapped by risk.</h2>
    <p>No signup. No app to install. Just open it.</p>
    <a class="btn-primary" href="/app" style="padding:13px 26px; font-size:15.5px;">Launch FloodSafe →</a>
  </div>
</section>

<footer>
  <div class="wrap" style="display:flex; justify-content:space-between; width:100%; flex-wrap:wrap; gap:10px;">
    <span>FloodSafe &middot; a flood-aware navigation project for Uttarakhand, India</span>
    <span><a href="/reports-view">Community reports</a> &middot; <a href="/status">API status</a></span>
  </div>
</footer>

<script>

async function loadLiveStrip() {

    try {
        const status = await (await fetch('/status')).json();
        const dot = document.getElementById('statusDot');
        const text = document.getElementById('statusText');

        if (status.routing === 'online') {
            text.textContent = 'Routing engine online — live on this page right now';
        } else {
            dot.classList.add('off');
            text.textContent = 'Routing engine temporarily unavailable';
        }
    } catch (error) {
        document.getElementById('statusDot').classList.add('off');
        document.getElementById('statusText').textContent = 'Could not reach the server';
    }

    try {
        const shelters = await (await fetch('/shelters')).json();
        const el = document.getElementById('statShelters');
        el.textContent = Array.isArray(shelters) ? shelters.length : '—';
        el.classList.remove('skeleton');
    } catch (error) {
        document.getElementById('statShelters').textContent = '—';
    }

    try {
        const reports = await (await fetch('/reports')).json();
        const el = document.getElementById('statReports');
        el.textContent = Array.isArray(reports) ? reports.length : '0';
        el.classList.remove('skeleton');
    } catch (error) {
        document.getElementById('statReports').textContent = '—';
    }

    try {
        const weather = await (await fetch('/weather?lat=30.3165&lon=78.0322')).json();
        const el = document.getElementById('statWeather');
        if (weather && typeof weather.total_mm === 'number') {
            el.textContent = weather.total_mm.toFixed(1) + ' mm';
        } else {
            el.textContent = '—';
        }
        el.classList.remove('skeleton');
    } catch (error) {
        document.getElementById('statWeather').textContent = '—';
    }

    // Node count is a fixed property of the deployed graph, not a live
    // API value — shown here for context alongside the truly live tiles.
    const nodesEl = document.getElementById('statNodes');
    nodesEl.textContent = '2.02M';
    nodesEl.classList.remove('skeleton');
}

loadLiveStrip();

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
        "timestamp": time.time()
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