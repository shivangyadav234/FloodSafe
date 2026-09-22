# FloodSafe — risk pipeline, PostGIS and API

The new stack, built alongside the existing Flask app in
`data/flood/uttarakhand/` rather than replacing it in one step. Nothing
here is required to run that app; it keeps working unchanged.

```
floodsafe/
  pipeline/   offline build: DEM -> terrain -> FFPI -> model     (heavy deps)
  db/         PostGIS schema, ingest, local cluster, compose      (SQL + psycopg)
  api/        FastAPI service reading PostGIS                     (small deps)
  models/     trained model + its validation metadata
  data/       build artifacts — gitignored, all regenerable
```

Dependencies are split on purpose. The pipeline needs GDAL, rasterio,
WhiteboxTools and XGBoost; the API needs none of them, because PostGIS
does the spatial work at runtime and the model's output is precomputed.
That keeps the deployable image small.

## The data

Every layer is real and openly published. Nothing is simulated.

| Layer | Source | Notes |
|---|---|---|
| Terrain | Copernicus DEM GLO-30 (ESA) | public AWS bucket, no credentials |
| Soil | SoilGrids v2.0 (ISRIC) | 1 km aggregated COG |
| Land cover | ESA WorldCover 2021 v200 | 10 m, resampled by majority class |
| Catchments | HydroSHEDS / HydroBASINS v1c L8 | already in the repo |
| Hazard atlas | State flash-flood hazard atlas | surveyed; covers ~9.7% of the state |
| **Disaster events** | **NASA Global Landslide Catalog via HDX** | **206 real events in-state, 5,276 deaths** |
| Rainfall | Open-Meteo | live, server-side cached |

## Why FFPI exists

The hazard atlas covers only about 9.7% of Uttarakhand, so most places
had no class and no thresholds. Training a model on the atlas and
extrapolating does not work well: under `GroupKFold` on the source
polygon, XGBoost reaches ROC-AUC **0.660**, while a random split claims
**0.987** — a +0.327 gap that is pure spatial leakage between points
sampled from the same polygon. At polygon level (n=189, the real sample
size) the classes separate on elevation (AUC 0.740, p=4e-08) and on
essentially nothing else: slope p=0.62, TWI p=0.11, distance-to-stream
p=0.54, ruggedness p=0.71.

So the **Flash Flood Potential Index** is used instead — the standard
operational approach (Smith 2003, NWS river forecast centres): reindex
each physical driver to 1–10 and take a weighted mean, slope carrying
double weight because runoff concentration time dominates flash
flooding. It needs no labels, and having never seen the atlas it still
agrees with it (polygon-level AUC 0.626, p=0.004) while covering 100%
of the state.

FFPI is a susceptibility index, **not** a probability or a forecast. A
surveyed atlas class always takes precedence over it, and a modelled
class is always labelled as modelled.

## Validating against real disasters

The atlas comparison above has a weakness: both FFPI and the atlas
describe where hazard is *believed* to be. So both were tested against
where rainfall-triggered mass movements have actually **happened** —
206 catalogued events inside Uttarakhand from NASA's Global Landslide
Catalog, together accounting for 5,276 recorded deaths. Neither FFPI nor
the terrain rasters had ever seen this data.

Two things had to be handled honestly. The catalog locates events to
between 1 km and 50 km, so results are reported per accuracy cut rather
than pooling them. And because it is largely news-sourced, events
cluster where people can report them — scoring against uniform
wilderness background measures accessibility as much as hazard, which
moves FFPI's apparent AUC from 70% to 54%. Background points are
therefore drawn at matched settlement proximity.

| | ≤5 km events | ≤10 km events |
|---|---|---|
| FFPI, settlement-matched background | **70.0%** (p=1e-09) | **72.6%** (p=3e-15) |
| FFPI, uniform background | 54.5% (n.s.) | 58.3% |

Against real events the individual terrain variables come alive —
distance-to-stream reaches p=1e-09 — where against the atlas they were
inert (slope p=0.62, TWI p=0.11). Real events are simply better labels.

## Two models, and why the second one exists

`train_model.py` learns from the hazard atlas and plateaus at **ROC-AUC
0.66** under spatial CV, no matter what is thrown at it — 21 features,
HAND, a 72-configuration search. Its top predictors are catchment area
and elevation, meaning it mostly learns *which basin* a point is in.

`train_event_model.py` learns from the real events instead and reaches
**ROC-AUC 0.824** under spatially blocked cross-validation (~25 km
blocks, so neighbouring events cannot straddle a fold). FFPI alone on
the same points scores 0.712, so the model adds about 11 points. Its top
predictors are multi-scale relief, topographic position and
distance-to-stream — actual terrain behaviour.

| | atlas-trained | event-trained |
|---|---|---|
| ROC-AUC (spatial) | 0.660 | **0.824** |
| PR-AUC | 0.290 | **0.463** |
| Brier | 0.206 | **0.064** |
| leakage gap vs random CV | +0.327 | +0.038 |

That last row matters: the atlas model's random-split score was inflated
by 33 points of leakage, the event model's by under 4. The 0.82 is real.

Stable across accuracy cuts (81.0% at ≤5 km, 82.4% at ≤10 km, 80.5% at
≤25 km, 82.7% at ≤50 km). It collapses to 37% at the ≤1 km cut, where
only 28 events survive across 40 spatial blocks — too few positives per
fold to learn from. That is a small-sample artifact and is reported
rather than hidden.

Both are served: `event_prob` is the supporting model, `model_prob` is
kept as legacy so the improvement stays visible. Neither is a forecast,
and landslides overlap flash floods without being identical to them.

## Running it

### Database

With Docker (preferred — pins the Postgres and PostGIS versions):

```bash
cd floodsafe/db
docker compose up -d db
docker compose run --rm ingest
docker compose up api
```

Without Docker (no admin rights needed; stages binaries from an existing
PostgreSQL install, adds the PostGIS bundle, runs an isolated cluster on
port 5433):

```bash
python floodsafe/db/setup_local_db.py
python -m floodsafe.db.ingest
```

`setup_local_db.py --stop` / `--start` / `--status` manage the cluster.
It never modifies the system PostgreSQL — Program Files is only read.

### API

```bash
uvicorn floodsafe.api.main:app --reload --port 8000
```

Interactive docs at `/docs`. Endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /health` | PostGIS version plus row counts per table |
| `GET /ffgs/zones` | every zone with class, thresholds and live status |
| `GET /ffgs/point?lat=&lon=` | guidance for an arbitrary point |
| `GET /shelters` | all shelters, optionally evacuation targets only |
| `GET /shelters/nearest?lat=&lon=` | K nearest by true metric distance |
| `GET /layers/hazard-atlas.geojson` | surveyed hazard polygons |
| `GET /layers/watersheds.geojson` | sub-basins, server-side simplified |

### Verifying the migration

```bash
python floodsafe/db/check_parity.py
```

Runs both stacks side by side and diffs hazard class, source, FFPI,
thresholds, soil group and catchment for every zone plus a set of probe
points. Rainfall is excluded — it is live and cached separately on each
side, so it legitimately differs.

## Rebuilding the pipeline

Only needed after changing a source dataset or the index itself. Run
from `floodsafe/pipeline/` against an environment with the geospatial
stack (see its `requirements.txt`):

```bash
python build_terrain.py --res 90      # ~800MB DEM download, cached
python build_soil.py --res 90
python build_landcover.py --res 90    # ~550MB, cached
python build_ffpi.py --res 90
python build_ffpi_lookup.py --res 90  # writes the Flask app's npz
python build_training_set.py --res 90
python train_model.py --res 90         # atlas-trained (legacy, 0.66)
python train_event_model.py --res 90   # event-trained (0.82)
python score_locations.py --res 90     # writes the app's location_scores.json
```

`validate_against_events.py` tests FFPI against the real disaster
catalog and writes nothing the app consumes — run it to reproduce the
70–73% figures. It needs the NASA Global Landslide Catalog, which
`floodsafe/data/events/` holds after a one-time download:

```bash
curl -L -o floodsafe/data/events/glc_nasa.zip \
  "https://data.humdata.org/dataset/1eb911ba-3681-4a96-b025-ae0c33b80a12/resource/ed703c45-2001-4286-ba16-8248c17fec80/download/global_landslide_catalog_nasa.zip"
```

`experiment_features.py` runs the feature ablation and writes nothing.
Scores for known locations come from `score_locations.py`, which samples
the 90 m raster directly rather than the regridded lookup — in steep
terrain regridding can move a point across a band boundary.

## A note on the local PROJ setup

`pipeline/_proj.py` points pyproj at the conda PROJ database, because
pyproj's own bundled copy cannot be opened in this environment. Import
it **only** where pyproj or geopandas is actually used: the same
override makes rasterio's bundled GDAL abort the process on Interrupted
Goode Homolosine, which is what SoilGrids uses. Modules that only touch
rasterio should transform through `rasterio.warp` and leave it alone.
