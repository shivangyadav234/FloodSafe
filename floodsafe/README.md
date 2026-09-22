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
class is always labelled as modelled. The XGBoost model is still served,
as an explicitly secondary signal.

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
python train_model.py --res 90
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
