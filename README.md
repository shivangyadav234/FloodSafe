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
  report reroutes you too — without needing to reload the page.
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

## Project layout

- `data/flood/uttarakhand/` — the live app: `server.py` (Flask API, the `/`
  landing page, and the `/reports-view` page), `map_app.py` (generates the
  Leaflet frontend served at `/app`), `routing_engine.py`
  (risk-weighted Dijkstra), the data-build pipeline
  (`extract_hazard.py` → `get_boundary.py` → `clip_hazard.py` →
  `create_road_hazard_geojson.py` → `road_flood_risk.py`) that turns the raw
  hazard atlas into the routing graph, and `extract_shelters.py` (pulls real
  shelter/hospital points from OSM).
- `data/flood/uttarakhand/data/` — the consolidated runtime data the app
  actually loads (geojson layers, the generated map HTML, the routing graph:
  `roads.npz`, `coordinates.npy`, `road_flood_risk.npz`, and `shelters.json`).
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

Open **http://127.0.0.1:5000/** for the landing page, or go straight to
**http://127.0.0.1:5000/app** for the map tool. The routing graph (~2M nodes)
takes a few seconds to load on startup — the server prints "Spatial index
ready." when it's done.

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
