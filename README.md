# FloodSafe

Flood-aware disaster navigation for Uttarakhand, India. FloodSafe routes people
around flood-hazard roads instead of just the shortest path — built on a real,
georeferenced government flash-flood hazard atlas rather than synthetic data.

## The problem

Standard navigation apps (Google Maps, OSRM, etc.) optimize purely for distance
or time. During a flood event, the "fastest" route is often the one that drives
straight through a road segment that's about to be — or already is — underwater.
FloodSafe treats flood risk as a first-class routing cost, not an afterthought.

## How it works

```
Government flash-flood hazard atlas (scanned PDF)
        ↓ georeferenced + OCR'd
Hazard-class GeoTIFF (LOW / MODERATE / SIGNIFICANT / EXTREME)
        ↓ polygonized
Hazard-zone GeoJSON
        ↓ intersected with OSM road network
Per-road-segment flood-risk score (1 / 2 / 4 / 8)
        ↓ baked into the routing graph
Risk-weighted Dijkstra over ~2M road nodes / ~4M edges
```

Two routing modes, on the same underlying graph:

- **Fastest** — ignores flood risk entirely (the baseline).
- **Safest** — treats EXTREME-risk roads as effectively blocked (only used if
  there's truly no other way through) and applies a squared penalty to
  MODERATE/SIGNIFICANT roads. If avoiding every EXTREME segment would mean a
  disproportionately long detour (more than 3x the direct distance), it
  relaxes to a milder penalty instead of forcing an absurd loop.

A third "Balanced" mode existed earlier but was removed — on this hazard
dataset, avoiding a risky road is either cheap (Safest and a hypothetical
middle-ground mode would agree anyway) or requires a huge detour (which the
3x cap above already governs), so a separate Balanced tier never actually
produced a route distinct from both Fastest and Safest.

### Beyond the static hazard map

- **Live rainfall awareness** — the app calls Open-Meteo (no API key needed)
  for current + near-term rainfall at the traveler's location. When it's
  actually raining hard, Safest routing gets more cautious for that trip, on
  top of the static hazard classification.
- **Crowdsourced hazard reports** — anyone can click the map to report a
  flooded or blocked road in real time. Reported locations hard-block nearby
  roads for *every* routing mode (a reported flooded bridge isn't a
  "graduated risk," it's a fact on the ground) and are visible to all users.
  Submitting a report instantly recalculates your own active route around
  it, and every open map polls for new reports every 30s so someone else's
  report reroutes you too — without needing to reload the page. Reports
  also auto-expire after 6 hours, or anyone can mark one "resolved" early
  (from its map popup or the reports page) once the road is actually
  clear, which removes it for everyone immediately and re-checks nearby
  routes in case a shorter path just opened up. Anyone can also confirm
  a report ("Still an issue?") to turn a lone, unverifiable claim into a
  visible trust signal ("Confirmed by 3 other travelers") — guarded
  per-report by a combination of local browser state and a server-side
  IP check so the same visitor can't inflate the count by clicking
  repeatedly.
- **Route comparison** — see Fastest and Safest side by side on one map, with
  a stats card showing the extra distance the safe route costs and how many
  extreme-risk segments it avoids.
- **Evacuate to nearest shelter** — one click routes from your current
  location to the closest reachable shelter/community facility, using the
  same flood-aware engine (not just straight-line distance).
- **Route to nearest hospital** — the same shortlist-then-route logic, run
  against hospitals instead of shelters, for when the need is medical
  rather than evacuation. Shelter and hospital locations come from real
  OpenStreetMap tags (`amenity=shelter`, `community_centre`,
  `social_facility`, `hospital`) — not an official government list, so
  treat them as a starting point, not verified capacity or open status.
- **Flash Flood Guidance System (FFGS)** — a dedicated page (`/ffgs`, linked
  from a button on the landing page's Flood Guidance section and from the
  main nav) that goes beyond the landing page's single-reading "headroom"
  panel: it pairs each mapped hazard zone with live rainfall over three
  separate windows (1h / 3h / 24h), each with its own watch/critical
  threshold, plus a heuristic adjustment for antecedent 48h rainfall
  (wetter ground needs less fresh rain to reach the same risk). A map
  shades the hazard atlas by class and plots every zone colored by its
  current worst status across the three windows, a sortable table lists
  all zones with a "check my location" option, and a banner surfaces any
  zone currently in WATCH or CRITICAL. It auto-refreshes every 60s (one
  batched Open-Meteo request covers every zone, regardless of count) and
  supports the same English/Hindi toggle and text-size control as the
  landing page. This is a heuristic built on this project's own hazard
  atlas, explicitly **not** an official CWC/IMD Flash Flood Guidance
  product — that would require a full hydrological model this repo
  doesn't have.
- **Locality-level granularity where the data supports it** — India's
  municipal corporations don't publish machine-readable ward boundaries
  (checked: zero ward-level boundaries exist in OSM for any Uttarakhand
  ULB, and no free GeoJSON/shapefile exists beyond PDF ward maps), so
  instead of faking ward polygons, FFGS pulls real, named OSM localities
  (`extract_localities.py`) via two passes and includes every one the
  hazard atlas actually covers: localities near a known town (Rishikesh's
  20 real neighborhoods along the Ganga — Laxman Jhula, Muni Ki Reti,
  Tapovan, etc.), and separately, any named village/hamlet/suburb within
  1km of a MODERATE/SIGNIFICANT/EXTREME hazard polygon specifically —
  which surfaces real flood-prone villages across Uttarkashi, Rudraprayag,
  Bageshwar, Pithoragarh and Champawat districts (~50 more, labeled by
  district when no anchor town is close enough to name them by). Each
  locality is labeled with its parent town or district (e.g. "Muni Ki
  Reti — Rishikesh", "Bhatwari — Uttarkashi district") rather than
  presented as an official ward.
- **The same locality layer on the routing map** — `/app`'s map carries a
  live-status marker for every one of those localities too (colored
  SAFE/WATCH/CRITICAL, same thresholds as FFGS), so route planning has
  the same neighborhood-level context without leaving the map tool. Each
  marker's popup links back to the full FFGS page.
- **Ward-aware hazard reports and shelters** — every hazard report and
  every shelter/hospital is tagged server-side with its nearest known
  locality (if one is within 5km), shown as "Near Muni Ki Reti,
  Rishikesh" in report cards, map popups, and the shelter list — using
  the same locality data as FFGS, not a separate lookup.

## Project layout

- `data/flood/uttarakhand/` — the live app: `server.py` (Flask API, the `/`
  landing page, the `/reports-view` page, and the `/ffgs` Flash Flood
  Guidance System page + its `/ffgs/*` endpoints), `map_app.py` (generates
  the Leaflet frontend served at `/app`), `routing_engine.py`
  (risk-weighted Dijkstra), the data-build pipeline
  (`extract_hazard.py` → `get_boundary.py` → `clip_hazard.py` →
  `create_road_hazard_geojson.py` → `road_flood_risk.py`) that turns the raw
  hazard atlas into the routing graph, `extract_shelters.py` (pulls real
  shelter/hospital points from OSM), and `extract_localities.py` (pulls
  real named localities from OSM for FFGS's locality-level granularity).
- `data/flood/uttarakhand/data/` — the consolidated runtime data the app
  actually loads (geojson layers, the generated map HTML, the routing graph:
  `roads.npz`, `coordinates.npy`, `road_flood_risk.npz`, `shelters.json`,
  and `localities.json`).
- `legacy/` — an earlier, synthetic-data prototype (`routing/` package,
  ad hoc test scripts) and abandoned diagnostic scripts, kept for reference
  but not part of the live app.

## Setup

The exact environment this has been developed and run against is a conda env
(`floodsafe`) with GDAL-backed geopandas — recommended on Windows since
plain `pip install geopandas` often fights with GDAL:

```bash
conda create -n floodsafe python=3.12
conda activate floodsafe
pip install -r requirements.txt
```

Plain `pip install -r requirements.txt` in a regular venv works too (tested
wheels available on Linux/Render); conda is only a Windows convenience.

## Running it

```bash
cd data/flood/uttarakhand
python server.py
```

Open **http://127.0.0.1:5000/** for the landing page, go straight to
**http://127.0.0.1:5000/app** for the map tool, or
**http://127.0.0.1:5000/ffgs** for the Flash Flood Guidance System. The
routing graph (~2M nodes) takes a few seconds to load on startup — the
server prints "Spatial index ready." when it's done.

If `pyproj` complains about its CRS database on Windows, point it at your
env's copy:

```bash
set PROJ_DATA_OVERRIDE=<path to your env>\Library\share\proj
```

Regenerating the map HTML (only needed if the hazard data changes):

```bash
python map_app.py
```

## Demo script

1. Start from the landing page (`/`) — point out the live stats strip (it's
   pulling real numbers from this same server's `/status`, `/shelters`,
   `/reports`, and `/weather` endpoints), then click **Launch app** to reach
   the map tool (`/app`).
2. Set your current location (search "Rishikesh" or use GPS) and a
   destination ("Dehradun"). Note the live-rainfall banner that appears.
3. Calculate a **Safest** route — point out the risk-colored segments and
   the risk breakdown.
4. Click **Compare Fastest vs Safest** — show the stats card: how much extra
   distance safety costs, and how many extreme-risk segments it avoids.
5. Click anywhere on the map to **report a hazard**, then recalculate the
   route — show it detouring around the reported point.
6. Click **Evacuate to Nearest Shelter** — shows the closest reachable
   shelter/community facility, routed to using the same flood-aware engine.
7. Click **Route to Nearest Hospital** — same idea, against the hospital
   list instead of shelters.
8. Back on the landing page, click **Open full Flash Flood Guidance System →**
   in the Flood Guidance section (or `/ffgs` directly) — show the map shaded
   by hazard class with live-status markers, the per-zone 1h/3h/24h table,
   and "Check guidance at my location."

## Tech stack

Flask + flask-cors (API) · Leaflet via Folium (frontend map) · NumPy/SciPy
sparse Dijkstra + cKDTree (routing) · GeoPandas/Shapely/pyproj (GIS) ·
pyrosm/rasterio (one-time data build, not needed at runtime) · Open-Meteo
(live rainfall, no API key required).

## Data source

Flood hazard classification derived from a state flash-flood hazard atlas
(georeferenced and OCR'd from the source PDF included in this repo's build
history). Road network from OpenStreetMap.

## Deployment

See `Procfile` / `render.yaml` for a one-click Render deployment (free tier).
Push this repo to GitHub, then create a new Render Blueprint pointing at it —
Render will pick up `render.yaml` automatically.
