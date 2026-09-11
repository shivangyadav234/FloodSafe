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
  --bg: #06090f;
  --bg-2: #0a0f18;
  --surface: rgba(255,255,255,0.035);
  --surface-2: rgba(255,255,255,0.06);
  --border: rgba(255,255,255,0.09);
  --border-strong: rgba(255,255,255,0.18);
  --text: #eef2f7;
  --text-muted: #93a0b4;
  --text-faint: #57627a;
  --cyan: #33e0ff;
  --teal: #2dd9b0;
  --violet: #9b8cff;
  --amber: #ffb84d;
  --red: #ff6b6b;
  --shadow-glow: 0 0 0 1px rgba(255,255,255,0.06), 0 20px 60px rgba(0,0,0,0.55);
}

* { box-sizing: border-box; }
html { scroll-behavior: smooth; }

body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: 'Source Sans 3', -apple-system, BlinkMacSystemFont, sans-serif;
  font-size: 16.5px;
  line-height: 1.6;
  position: relative;
  overflow-x: hidden;
}

/* ambient background: grid + drifting glow orbs + grain */

.bg-fixed {
  position: fixed;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  overflow: hidden;
}

.bg-grid {
  position: absolute;
  inset: -2px;
  background-image:
    linear-gradient(rgba(255,255,255,0.035) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255,255,255,0.035) 1px, transparent 1px);
  background-size: 64px 64px;
  mask-image: radial-gradient(ellipse 80% 60% at 50% 0%, black 0%, transparent 75%);
}

.orb {
  position: absolute;
  border-radius: 50%;
  filter: blur(90px);
  opacity: 0.5;
  animation: drift 22s ease-in-out infinite alternate;
}

.orb.o1 { width: 560px; height: 560px; top: -220px; left: -140px; background: radial-gradient(circle, var(--cyan), transparent 70%); }
.orb.o2 { width: 460px; height: 460px; top: 80px; right: -160px; background: radial-gradient(circle, var(--violet), transparent 70%); animation-duration: 28s; animation-delay: -6s; }
.orb.o3 { width: 500px; height: 500px; top: 1400px; left: 30%; background: radial-gradient(circle, var(--teal), transparent 70%); opacity: 0.28; animation-duration: 26s; }

@keyframes drift {
  0% { transform: translate(0,0) scale(1); }
  100% { transform: translate(40px,60px) scale(1.08); }
}

.grain {
  position: absolute; inset: 0;
  opacity: 0.05;
  mix-blend-mode: overlay;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='120'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
}

h1, h2, h3 {
  font-family: 'Bricolage Grotesque', 'Source Sans 3', sans-serif;
  color: var(--text);
  text-wrap: balance;
  margin: 0;
  letter-spacing: -0.01em;
}

code, .mono { font-family: 'IBM Plex Mono', ui-monospace, monospace; }

a { color: var(--cyan); }

.wrap { max-width: 1120px; margin: 0 auto; padding: 0 28px; position: relative; z-index: 1; }

.grad-text {
  background: linear-gradient(90deg, var(--cyan), var(--teal));
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}

.reveal {
  opacity: 0;
  transform: translateY(24px);
  transition: opacity 0.7s ease, transform 0.7s ease;
}
.reveal.in {
  opacity: 1;
  transform: translateY(0);
}

/* ---------------- NAV ---------------- */

nav {
  position: sticky; top: 0; z-index: 30;
  background: rgba(6,9,15,0.72);
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
  border-bottom: 1px solid var(--border);
}

nav .wrap {
  display: flex; align-items: center; justify-content: space-between;
  padding-top: 16px; padding-bottom: 16px;
}

.brand {
  display: flex; align-items: center; gap: 10px;
  font-family: 'Bricolage Grotesque', sans-serif;
  font-weight: 700; font-size: 19px; color: var(--text);
  text-decoration: none;
}

.brand .mark {
  width: 30px; height: 30px; border-radius: 9px;
  background: linear-gradient(155deg, var(--cyan), var(--violet));
  display: inline-flex; align-items: center; justify-content: center;
  color: #06090f; font-size: 15px;
  box-shadow: 0 0 24px rgba(51,224,255,0.35);
}

.nav-links { display: flex; align-items: center; gap: 26px; }
.nav-links a.plain { color: var(--text-muted); text-decoration: none; font-size: 14.5px; transition: color .15s ease; }
.nav-links a.plain:hover { color: var(--text); }

.btn-primary {
  background: linear-gradient(90deg, var(--cyan), var(--teal));
  color: #04121a !important;
  padding: 10px 20px;
  border-radius: 999px;
  text-decoration: none;
  font-weight: 700;
  font-size: 14.5px;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  transition: transform .18s ease, box-shadow .18s ease;
  box-shadow: 0 0 0 1px rgba(51,224,255,0.25), 0 10px 30px -8px rgba(51,224,255,0.55);
}
.btn-primary:hover { transform: translateY(-2px); box-shadow: 0 0 0 1px rgba(51,224,255,0.4), 0 16px 36px -6px rgba(51,224,255,0.7); }

.btn-glass {
  background: var(--surface);
  border: 1px solid var(--border-strong);
  color: var(--text) !important;
  padding: 10px 20px;
  border-radius: 999px;
  text-decoration: none;
  font-weight: 600;
  font-size: 14.5px;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  transition: background .15s ease, border-color .15s ease, transform .15s ease;
}
.btn-glass:hover { background: var(--surface-2); border-color: rgba(255,255,255,0.3); transform: translateY(-2px); }

/* ---------------- HERO ---------------- */

.hero {
  padding: 84px 0 56px;
  position: relative;
}

.hero-grid {
  display: grid;
  grid-template-columns: 1.05fr 0.95fr;
  gap: 54px;
  align-items: center;
}

.eyebrow {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 12.5px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--cyan);
  background: rgba(51,224,255,0.1);
  border: 1px solid rgba(51,224,255,0.25);
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 6px 13px;
  border-radius: 999px;
  margin-bottom: 22px;
}

.eyebrow .dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--cyan);
  box-shadow: 0 0 8px var(--cyan);
}

.hero h1 {
  font-size: 58px;
  line-height: 1.04;
  font-weight: 700;
}

.hero h1 em {
  font-style: normal;
}

.hero p.sub {
  font-size: 18.5px;
  color: var(--text-muted);
  max-width: 490px;
  margin: 20px 0 30px;
}

.hero-ctas { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }

/* hero art: mock "product" card */

.hero-art {
  background: linear-gradient(165deg, #0d1420, #070b12);
  border: 1px solid var(--border);
  border-radius: 22px;
  box-shadow: var(--shadow-glow);
  padding: 0;
  aspect-ratio: 1 / 0.95;
  position: relative;
  overflow: hidden;
}

.hero-art .grid-pattern {
  position: absolute; inset: 0;
  background-image:
    linear-gradient(rgba(255,255,255,0.05) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255,255,255,0.05) 1px, transparent 1px);
  background-size: 28px 28px;
  mask-image: radial-gradient(ellipse 90% 70% at 50% 40%, black 30%, transparent 90%);
}

.hero-art .blob {
  position: absolute;
  border-radius: 50%;
  filter: blur(60px);
}
.hero-art .blob.b1 { width: 220px; height: 220px; top: -40px; right: -40px; background: radial-gradient(circle, rgba(155,140,255,0.4), transparent 70%); }
.hero-art .blob.b2 { width: 260px; height: 260px; bottom: -60px; left: -60px; background: radial-gradient(circle, rgba(51,224,255,0.32), transparent 70%); }

.hero-art svg { width: 100%; height: 100%; position: relative; z-index: 1; }

.route-safe {
  stroke-dasharray: 700;
  stroke-dashoffset: 700;
  animation: draw 2.2s ease-out 0.3s forwards;
}
.route-risk {
  stroke-dasharray: 6 8;
  opacity: 0;
  animation: fadein 1s ease 1.6s forwards;
}
@keyframes draw { to { stroke-dashoffset: 0; } }
@keyframes fadein { to { opacity: 0.85; } }

.pulse-node {
  animation: nodepulse 2.4s ease-in-out infinite;
}
@keyframes nodepulse {
  0%, 100% { r: 6; opacity: 1; }
  50% { r: 8.5; opacity: 0.7; }
}

.hero-chip {
  position: absolute;
  background: rgba(10,15,24,0.85);
  backdrop-filter: blur(10px);
  border: 1px solid var(--border-strong);
  border-radius: 12px;
  padding: 9px 13px;
  font-family: 'IBM Plex Mono', monospace;
  font-size: 11.5px;
  display: flex;
  align-items: center;
  gap: 8px;
  box-shadow: 0 12px 30px rgba(0,0,0,0.45);
  z-index: 2;
  opacity: 0;
  animation: chipin 0.6s ease forwards;
}
.hero-chip .sw { width: 8px; height: 8px; border-radius: 50%; }
.hero-chip.c1 { top: 24px; left: 22px; animation-delay: 0.4s; }
.hero-chip.c2 { bottom: 60px; right: 20px; animation-delay: 2.1s; }
@keyframes chipin { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }

/* ---------------- LIVE STRIP ---------------- */

.live-strip {
  padding: 30px 0 8px;
}

.live-status-pill {
  display: inline-flex; align-items: center; gap: 10px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 8px 16px;
  font-family: 'IBM Plex Mono', monospace;
  font-size: 12.5px;
  color: var(--text-muted);
  margin-bottom: 22px;
}

.pulse-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--teal);
  box-shadow: 0 0 10px var(--teal);
  animation: pulse 2.2s infinite;
}
.pulse-dot.off { background: var(--red); box-shadow: 0 0 10px var(--red); animation: none; }

@keyframes pulse {
  0% { box-shadow: 0 0 0 0 rgba(45,217,176,0.55); }
  70% { box-shadow: 0 0 0 10px transparent; }
  100% { box-shadow: 0 0 0 0 transparent; }
}

.live-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 14px;
}

.live-tile {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 20px 20px 18px;
  position: relative;
  overflow: hidden;
  transition: border-color .2s ease, transform .2s ease;
}
.live-tile:hover { border-color: var(--border-strong); transform: translateY(-3px); }

.live-tile .lv-icon {
  width: 34px; height: 34px; border-radius: 10px;
  display: flex; align-items: center; justify-content: center;
  font-size: 16px;
  margin-bottom: 14px;
}
.live-tile.t1 .lv-icon { background: rgba(51,224,255,0.14); }
.live-tile.t2 .lv-icon { background: rgba(155,140,255,0.14); }
.live-tile.t3 .lv-icon { background: rgba(255,184,77,0.14); }
.live-tile.t4 .lv-icon { background: rgba(45,217,176,0.14); }

.live-tile .lv-num {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 28px;
  font-weight: 600;
  color: var(--text);
  font-variant-numeric: tabular-nums;
  min-height: 34px;
}

.live-tile .lv-label {
  font-size: 12.5px;
  color: var(--text-faint);
  margin-top: 4px;
}

.skeleton { opacity: 0.25; }

/* ---------------- HAZARD LEGEND ---------------- */

.legend-band {
  display: flex; align-items: center; gap: 20px; flex-wrap: wrap;
  padding: 26px 0;
  margin-top: 10px;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
  font-family: 'IBM Plex Mono', monospace;
  font-size: 12.5px;
  color: var(--text-muted);
}

.legend-band .lb-label { color: var(--text); font-weight: 600; margin-right: 4px; }
.legend-sw { display: inline-flex; align-items: center; gap: 7px; }
.legend-sw .dot { width: 9px; height: 9px; border-radius: 3px; box-shadow: 0 0 8px currentColor; }

/* ---------------- STORY ---------------- */

.story {
  padding: 72px 0;
  border-bottom: 1px solid var(--border);
}

.story-grid {
  display: grid;
  grid-template-columns: 0.9fr 1.1fr;
  gap: 48px;
  align-items: start;
}

.section-kicker {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 12px;
  color: var(--amber);
  letter-spacing: 0.1em;
  text-transform: uppercase;
  margin-bottom: 12px;
  display: flex; align-items: center; gap: 8px;
}
.section-kicker::before {
  content: '';
  width: 16px; height: 1px;
  background: var(--amber);
}

.story h2 { font-size: 32px; margin-bottom: 16px; }
.story p { color: var(--text-muted); font-size: 15.5px; }

.compare-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 18px;
  box-shadow: var(--shadow-glow);
  padding: 26px 28px;
}

.compare-card .route-label {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 12px;
  color: var(--text-faint);
  margin-bottom: 18px;
  letter-spacing: 0.03em;
}

.compare-rows { display: flex; flex-direction: column; gap: 12px; }

.compare-row {
  display: grid;
  grid-template-columns: 92px 1fr 74px;
  align-items: center;
  gap: 12px;
  font-size: 13.5px;
}

.compare-row .bar-track {
  background: rgba(255,255,255,0.05);
  border-radius: 8px;
  height: 26px;
  position: relative;
  overflow: hidden;
}

.compare-row .bar-fill {
  position: absolute; left: 0; top: 0; bottom: 0;
  border-radius: 8px;
  display: flex; align-items: center;
  padding-left: 10px;
  font-family: 'IBM Plex Mono', monospace;
  font-size: 11px;
  color: #04121a;
  font-weight: 600;
  white-space: nowrap;
  width: 0;
  animation: growbar 1.4s cubic-bezier(.2,.8,.2,1) forwards;
  animation-delay: 0.3s;
}

.compare-row.fastest .bar-fill { background: linear-gradient(90deg, #ff8a80, var(--red)); --target: 76%; }
.compare-row.safest .bar-fill { background: linear-gradient(90deg, var(--teal), var(--cyan)); --target: 100%; animation-delay: 0.6s; }

@keyframes growbar { to { width: var(--target); } }

.compare-row .val { font-family: 'IBM Plex Mono', monospace; text-align: right; font-weight: 700; color: var(--text); }

.compare-foot {
  margin-top: 16px; padding-top: 16px; border-top: 1px dashed var(--border);
  font-size: 12.5px; color: var(--text-faint); font-family: 'IBM Plex Mono', monospace;
}

/* ---------------- FEATURES ---------------- */

.features { padding: 72px 0; border-bottom: 1px solid var(--border); }

.features h2 { font-size: 32px; margin-bottom: 10px; }
.features > .wrap > p.lede { color: var(--text-muted); margin: 0 0 36px; font-size: 15.5px; max-width: 560px; }

.feature-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 18px;
}

.feature-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 18px;
  padding: 24px;
  transition: border-color .2s ease, transform .2s ease, background .2s ease;
  position: relative;
}
.feature-card:hover { border-color: var(--border-strong); transform: translateY(-4px); background: var(--surface-2); }

.feature-card .ic {
  width: 42px; height: 42px; border-radius: 12px;
  display: flex; align-items: center; justify-content: center;
  font-size: 19px; margin-bottom: 16px;
}
.feature-card:nth-child(1) .ic { background: linear-gradient(155deg, rgba(51,224,255,0.25), rgba(51,224,255,0.05)); }
.feature-card:nth-child(2) .ic { background: linear-gradient(155deg, rgba(155,140,255,0.25), rgba(155,140,255,0.05)); }
.feature-card:nth-child(3) .ic { background: linear-gradient(155deg, rgba(255,184,77,0.25), rgba(255,184,77,0.05)); }
.feature-card:nth-child(4) .ic { background: linear-gradient(155deg, rgba(255,107,107,0.25), rgba(255,107,107,0.05)); }

.feature-card h3 { font-size: 17px; font-family: 'Source Sans 3',sans-serif; font-weight: 700; margin-bottom: 7px; }
.feature-card p { font-size: 14px; color: var(--text-muted); margin: 0; line-height: 1.55; }

/* ---------------- SOURCES ---------------- */

.sources-band {
  padding: 44px 0;
  border-bottom: 1px solid var(--border);
}

.sources-band .sb-label {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 11.5px;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: var(--text-faint);
  margin-bottom: 18px;
}

.pill-row { display: flex; gap: 10px; flex-wrap: wrap; }

.src-pill {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 8px 15px;
  font-size: 13px;
  color: var(--text-muted);
  display: flex; align-items: center; gap: 7px;
  transition: border-color .2s ease;
}
.src-pill:hover { border-color: var(--border-strong); }
.src-pill b { color: var(--text); font-weight: 600; }

/* ---------------- FINAL CTA ---------------- */

.final-cta {
  padding: 88px 0 72px;
  text-align: center;
  position: relative;
}

.final-cta h2 { font-size: 40px; margin-bottom: 14px; }
.final-cta p { color: var(--text-muted); margin: 0 0 30px; font-size: 16px; }

footer {
  border-top: 1px solid var(--border);
  padding: 28px 0 44px;
  display: flex; justify-content: space-between; align-items: center;
  flex-wrap: wrap; gap: 10px;
  font-size: 13px; color: var(--text-faint);
  position: relative; z-index: 1;
}

footer a { color: var(--text-faint); }
footer a:hover { color: var(--text-muted); }

@media (max-width: 860px) {
  .hero-grid { grid-template-columns: 1fr; }
  .hero h1 { font-size: 42px; }
  .live-grid { grid-template-columns: repeat(2, 1fr); }
  .story-grid { grid-template-columns: 1fr; }
  .feature-grid { grid-template-columns: 1fr; }
  .nav-links a.plain { display: none; }
  .hero-chip.c2 { right: auto; left: 20px; bottom: 20px; }
}

</style>
</head>
<body>

<div class="bg-fixed">
  <div class="bg-grid"></div>
  <div class="orb o1"></div>
  <div class="orb o2"></div>
  <div class="orb o3"></div>
  <div class="grain"></div>
</div>

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
      <span class="eyebrow"><span class="dot"></span>Uttarakhand &middot; live routing</span>
      <h1>Fastest isn't<br>always <em class="grad-text">safe</em>.</h1>
      <p class="sub">FloodSafe routes you around flood-hazard roads using a real government hazard atlas, not just distance &mdash; on a live graph of 2 million road nodes across Uttarakhand.</p>
      <div class="hero-ctas">
        <a class="btn-primary" href="/app">Open the map →</a>
        <a class="btn-glass" href="#live">See it working live ↓</a>
      </div>
    </div>
    <div class="hero-art">
      <div class="grid-pattern"></div>
      <div class="blob b1"></div>
      <div class="blob b2"></div>
      <div class="hero-chip c1"><span class="sw" style="background:var(--red); box-shadow:0 0 8px var(--red);"></span>EXTREME risk detected</div>
      <div class="hero-chip c2"><span class="sw" style="background:var(--teal); box-shadow:0 0 8px var(--teal);"></span>Rerouted +29km safer</div>
      <svg viewBox="0 0 340 300" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <filter id="glow" x="-50%" y="-50%" width="200%" height="200%">
            <feGaussianBlur stdDeviation="4" result="blur"/>
            <feMerge>
              <feMergeNode in="blur"/>
              <feMergeNode in="SourceGraphic"/>
            </feMerge>
          </filter>
        </defs>
        <path class="route-risk" d="M20 250 Q 90 180 140 200 T 260 130 Q 300 100 320 60" fill="none" stroke="#ff6b6b" stroke-width="3" stroke-linecap="round"/>
        <path class="route-safe" filter="url(#glow)" d="M20 250 Q 70 230 100 245 Q 150 270 180 230 Q 210 190 190 150 Q 170 105 210 85 Q 260 60 320 60" fill="none" stroke="url(#safeGrad)" stroke-width="4" stroke-linecap="round"/>
        <linearGradient id="safeGrad" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stop-color="#33e0ff"/>
          <stop offset="100%" stop-color="#2dd9b0"/>
        </linearGradient>
        <circle class="pulse-node" cx="20" cy="250" r="6" fill="#eef2f7"/>
        <circle class="pulse-node" cx="320" cy="60" r="6" fill="#eef2f7"/>
        <circle cx="190" cy="150" r="5" fill="#ff6b6b" filter="url(#glow)"/>
      </svg>
    </div>
  </div>
</header>

<section class="wrap live-strip" id="live">
  <div class="live-status-pill"><span class="pulse-dot" id="statusDot"></span> <span id="statusText">Checking live status…</span></div>
  <div class="live-grid">
    <div class="live-tile t1"><div class="lv-icon">🗺️</div><div class="lv-num skeleton" id="statNodes">—</div><div class="lv-label">road nodes in the graph</div></div>
    <div class="live-tile t2"><div class="lv-icon">⛺</div><div class="lv-num skeleton" id="statShelters">—</div><div class="lv-label">shelters &amp; hospitals mapped</div></div>
    <div class="live-tile t3"><div class="lv-icon">🚨</div><div class="lv-num skeleton" id="statReports">—</div><div class="lv-label">active hazard reports right now</div></div>
    <div class="live-tile t4"><div class="lv-icon">🌧️</div><div class="lv-num skeleton" id="statWeather">—</div><div class="lv-label">live rainfall, Dehradun</div></div>
  </div>

  <div class="legend-band">
    <span class="lb-label">Hazard classes:</span>
    <span class="legend-sw" style="color:#4ade80"><span class="dot" style="background:#4ade80"></span>LOW</span>
    <span class="legend-sw" style="color:#facc15"><span class="dot" style="background:#facc15"></span>MODERATE</span>
    <span class="legend-sw" style="color:#fb923c"><span class="dot" style="background:#fb923c"></span>SIGNIFICANT</span>
    <span class="legend-sw" style="color:#ff6b6b"><span class="dot" style="background:#ff6b6b"></span>EXTREME</span>
    <span style="margin-left:auto; color:var(--text-faint);">from a georeferenced state flash-flood hazard atlas</span>
  </div>
</section>

<section class="story reveal">
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
          <div class="bar-track"><div class="bar-fill">10 extreme-risk segments</div></div>
          <div class="val">90.2 km</div>
        </div>
        <div class="compare-row safest">
          <div>🛡 Safest</div>
          <div class="bar-track"><div class="bar-fill">0 extreme-risk segments</div></div>
          <div class="val">119.3 km</div>
        </div>
      </div>
      <div class="compare-foot">+29 km to eliminate every extreme-risk segment on the route</div>
    </div>
  </div>
</section>

<section class="features reveal" id="features">
  <div class="wrap">
    <div class="section-kicker">What it does</div>
    <h2>Built for the moment it actually rains</h2>
    <p class="lede">Every feature exists because a static hazard map alone isn't enough once water is actually rising.</p>
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

<section class="sources-band reveal">
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

<section class="final-cta reveal">
  <div class="wrap">
    <h2>Uttarakhand's roads, <span class="grad-text">mapped by risk.</span></h2>
    <p>No signup. No app to install. Just open it.</p>
    <a class="btn-primary" href="/app" style="padding:15px 30px; font-size:16px;">Launch FloodSafe →</a>
  </div>
</section>

<footer>
  <div class="wrap" style="display:flex; justify-content:space-between; width:100%; flex-wrap:wrap; gap:10px;">
    <span>FloodSafe &middot; a flood-aware navigation project for Uttarakhand, India</span>
    <span><a href="/reports-view">Community reports</a> &middot; <a href="/status">API status</a></span>
  </div>
</footer>

<script>

function animateCount(el, target, suffix, duration) {
    suffix = suffix || '';
    duration = duration || 900;
    const start = performance.now();
    function tick(now) {
        const progress = Math.min((now - start) / duration, 1);
        const eased = 1 - Math.pow(1 - progress, 3);
        const value = Math.round(target * eased);
        el.textContent = value + suffix;
        if (progress < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
}

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
        el.classList.remove('skeleton');
        animateCount(el, Array.isArray(shelters) ? shelters.length : 0);
    } catch (error) {
        document.getElementById('statShelters').textContent = '—';
    }

    try {
        const reports = await (await fetch('/reports')).json();
        const el = document.getElementById('statReports');
        el.classList.remove('skeleton');
        animateCount(el, Array.isArray(reports) ? reports.length : 0);
    } catch (error) {
        document.getElementById('statReports').textContent = '—';
    }

    try {
        // Called directly from the browser, not proxied through this
        // server — see fetchLiveConditions() in map_app.py for why.
        const payload = await (await fetch(
            'https://api.open-meteo.com/v1/forecast?latitude=30.3165&longitude=78.0322' +
            '&current=precipitation&hourly=precipitation&forecast_days=1&timezone=auto'
        )).json();

        const el = document.getElementById('statWeather');
        el.classList.remove('skeleton');

        if (payload && payload.current) {
            const currentMm = Number(payload.current.precipitation || 0);
            const hourlyTimes = (payload.hourly && payload.hourly.time) || [];
            const hourlyPrecip = (payload.hourly && payload.hourly.precipitation) || [];
            const currentTime = payload.current.time;
            let next3hMm = 0;
            if (currentTime && hourlyTimes.length) {
                let startIndex = hourlyTimes.indexOf(currentTime);
                if (startIndex === -1) startIndex = 0;
                next3hMm = hourlyPrecip
                    .slice(startIndex, startIndex + 3)
                    .reduce((sum, v) => sum + (Number(v) || 0), 0);
            }
            el.textContent = (currentMm + next3hMm).toFixed(1) + ' mm';
        } else {
            el.style.fontSize = '15px';
            el.style.color = 'var(--text-faint)';
            el.textContent = 'unavailable right now';
        }
    } catch (error) {
        const el = document.getElementById('statWeather');
        el.classList.remove('skeleton');
        el.style.fontSize = '15px';
        el.style.color = 'var(--text-faint)';
        el.textContent = 'unavailable right now';
    }

    const nodesEl = document.getElementById('statNodes');
    nodesEl.classList.remove('skeleton');
    nodesEl.textContent = '2.02M';
}

loadLiveStrip();

const revealEls = document.querySelectorAll('.reveal');
const revealObserver = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
        if (entry.isIntersecting) {
            entry.target.classList.add('in');
            revealObserver.unobserve(entry.target);
        }
    });
}, { threshold: 0.15 });
revealEls.forEach((el) => revealObserver.observe(el));

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