"""
Score every FFGS location with the FFPI surface and the susceptibility
model, and write one JSON the Flask app can load at startup.

Covers the nine guidance towns plus every locality in localities.json,
including the ones the hazard atlas never mapped -- which is the point:
those currently render as UNMAPPED with no threshold at all.

Two numbers per location, deliberately kept distinct:

    ffpi        1-10 physical susceptibility index, no labels involved,
                available everywhere the input rasters are
    model_prob  P(elevated hazard) from the XGBoost model, honest
                spatial-CV ROC-AUC 0.66, dominated by elevation

The app presents FFPI as the primary signal and the model probability as
a secondary, explicitly caveated one. Neither is a calibrated flood
forecast.

Usage:
    python score_locations.py --res 90
"""

import argparse
import json
import os
from datetime import datetime, timezone

import numpy as np
import rasterio
from rasterio.warp import transform as warp_transform
import xgboost as xgb


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LEGACY_DATA = os.path.join(REPO_ROOT, "data", "flood", "uttarakhand", "data")
OUT_ROOT = os.path.join(REPO_ROOT, "floodsafe", "data", "terrain")
MODEL_ROOT = os.path.join(REPO_ROOT, "floodsafe", "models")
TABLE_ROOT = os.path.join(REPO_ROOT, "floodsafe", "data", "tables")

# Same nine towns the live system already uses (GUIDANCE_TOWNS in
# server.py). Duplicated rather than imported: server.py runs a Flask app
# at import time and isn't safe to pull into a build script.
GUIDANCE_TOWNS = [
    {"name": "Dehradun", "lat": 30.3165, "lon": 78.0322},
    {"name": "Rishikesh", "lat": 30.0869, "lon": 78.2676},
    {"name": "Haridwar", "lat": 29.9457, "lon": 78.1642},
    {"name": "Mussoorie", "lat": 30.4598, "lon": 78.0664},
    {"name": "Nainital", "lat": 29.3803, "lon": 79.4636},
    {"name": "Haldwani", "lat": 29.2183, "lon": 79.5130},
    {"name": "Almora", "lat": 29.5892, "lon": 79.6467},
    {"name": "Pithoragarh", "lat": 29.5822, "lon": 80.2181},
    {"name": "Joshimath", "lat": 30.5551, "lon": 79.5643},
]

# Must match train_model.py's feature order exactly.
MODEL_FEATURES = [
    "elevation_m", "slope_deg", "plan_curv", "prof_curv", "twi", "spi",
    "sca", "flowacc_cells", "dist_to_stream_m", "ruggedness",
    "soil_sand_pct", "soil_clay_pct", "soil_silt_pct", "soil_hsg",
    "catchment_up_km2", "catchment_sub_km2",
]

FEATURE_RASTERS = {
    "elevation_m": "dem.tif", "slope_deg": "slope.tif",
    "plan_curv": "plan_curv.tif", "prof_curv": "prof_curv.tif",
    "twi": "twi.tif", "spi": "spi.tif", "sca": "sca.tif",
    "flowacc_cells": "flowacc_cells.tif",
    "dist_to_stream_m": "dist_to_stream.tif", "ruggedness": "ruggedness.tif",
    "soil_sand_pct": "soil_sand.tif", "soil_clay_pct": "soil_clay.tif",
    "soil_silt_pct": "soil_silt.tif", "soil_hsg": "soil_hsg.tif",
}

FFPI_RASTERS = {
    "ffpi": "ffpi.tif",
    "ffpi_slope": "ffpi_slope.tif",
    "ffpi_soil": "ffpi_soil.tif",
    "ffpi_landcover": "ffpi_landcover.tif",
    "ffpi_convergence": "ffpi_convergence.tif",
}

FFPI_BANDS = [(3.5, "VERY LOW"), (4.5, "LOW"), (5.5, "MODERATE"),
              (6.5, "HIGH"), (99.0, "VERY HIGH")]


def log(msg):
    print(f"[score] {msg}", flush=True)


def band_for(value):
    if value is None:
        return None
    for upper, name in FFPI_BANDS:
        if value < upper:
            return name
    return FFPI_BANDS[-1][1]


def load_points():
    points = [dict(t, kind="town", parent_town=None) for t in GUIDANCE_TOWNS]

    path = os.path.join(LEGACY_DATA, "localities.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for loc in json.load(f):
                points.append({
                    "name": loc["name"], "lat": loc["lat"], "lon": loc["lon"],
                    "kind": "locality", "parent_town": loc.get("town"),
                })

    log(f"points to score: {len(points)}")
    return points


def sample_stack(points, out_dir, names_to_files):
    """Sample a set of rasters at every point. Row/col computed by hand --
    src.sample() segfaults in this environment (see build_training_set)."""

    with rasterio.open(os.path.join(out_dir, "dem.tif")) as ref:
        transform, height, width, crs = ref.transform, ref.height, ref.width, ref.crs

    lons = [p["lon"] for p in points]
    lats = [p["lat"] for p in points]
    xs, ys = warp_transform("EPSG:4326", crs, lons, lats)

    cols = ((np.array(xs) - transform.c) / transform.a).astype("int64")
    rows = ((np.array(ys) - transform.f) / transform.e).astype("int64")
    inside = (rows >= 0) & (rows < height) & (cols >= 0) & (cols < width)

    safe_rows = np.clip(rows, 0, height - 1)
    safe_cols = np.clip(cols, 0, width - 1)

    out = {}
    for name, fname in names_to_files.items():
        path = os.path.join(out_dir, fname)
        if not os.path.exists(path):
            log(f"  WARNING: missing {fname}")
            out[name] = np.full(len(points), np.nan)
            continue

        with rasterio.open(path) as src:
            band = src.read(1)
            nodata = src.nodata

        vals = band[safe_rows, safe_cols].astype("float64")
        vals[~inside] = np.nan
        if nodata is not None:
            vals = np.where(vals == nodata, np.nan, vals)
        vals = np.where(vals < -1e30, np.nan, vals)
        out[name] = vals

    return out


def join_catchments(points):
    import _proj  # noqa: F401 -- geopandas needs the PROJ override
    import geopandas as gpd

    path = os.path.join(LEGACY_DATA, "uttarakhand_watersheds.geojson")
    if not os.path.exists(path):
        return np.full(len(points), np.nan), np.full(len(points), np.nan)

    basins = gpd.read_file(path)
    pts = gpd.GeoDataFrame(
        {"i": range(len(points))},
        geometry=gpd.points_from_xy([p["lon"] for p in points],
                                    [p["lat"] for p in points]),
        crs="EPSG:4326",
    )
    joined = gpd.sjoin(pts, basins[["UP_AREA", "SUB_AREA", "geometry"]],
                       how="left", predicate="within")
    joined = joined[~joined.index.duplicated(keep="first")]
    return joined["UP_AREA"].to_numpy(), joined["SUB_AREA"].to_numpy()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--res", type=int, default=90)
    args = ap.parse_args()

    out_dir = os.path.join(OUT_ROOT, f"{args.res}m")
    points = load_points()

    log("sampling FFPI ...")
    ffpi_vals = sample_stack(points, out_dir, FFPI_RASTERS)

    log("sampling model features ...")
    feat_vals = sample_stack(points, out_dir, FEATURE_RASTERS)
    feat_vals["soil_hsg"] = np.where(feat_vals["soil_hsg"] == 0,
                                     np.nan, feat_vals["soil_hsg"])

    log("joining catchments ...")
    up, sub = join_catchments(points)
    feat_vals["catchment_up_km2"] = up
    feat_vals["catchment_sub_km2"] = sub

    model_path = os.path.join(MODEL_ROOT, f"susceptibility_{args.res}m.json")
    probs = np.full(len(points), np.nan)
    if os.path.exists(model_path):
        booster = xgb.Booster()
        booster.load_model(model_path)
        X = np.column_stack([feat_vals[f] for f in MODEL_FEATURES]).astype("float32")
        probs = booster.predict(xgb.DMatrix(X, feature_names=MODEL_FEATURES))
        log(f"model scored {int(np.isfinite(probs).sum())} points")
    else:
        log(f"WARNING: no model at {model_path}, skipping probabilities")

    def clean(v, nd=2):
        return None if not np.isfinite(v) else round(float(v), nd)

    records = []
    for i, p in enumerate(points):
        ffpi = clean(ffpi_vals["ffpi"][i])
        records.append({
            "name": p["name"], "lat": p["lat"], "lon": p["lon"],
            "kind": p["kind"], "parent_town": p["parent_town"],
            "ffpi": ffpi,
            "ffpi_band": band_for(ffpi),
            "ffpi_components": {
                "slope": clean(ffpi_vals["ffpi_slope"][i], 1),
                "soil": clean(ffpi_vals["ffpi_soil"][i], 1),
                "landcover": clean(ffpi_vals["ffpi_landcover"][i], 1),
                "convergence": clean(ffpi_vals["ffpi_convergence"][i], 1),
            },
            "model_prob": clean(probs[i], 3),
            "terrain": {
                "elevation_m": clean(feat_vals["elevation_m"][i], 0),
                "slope_deg": clean(feat_vals["slope_deg"][i], 1),
                "twi": clean(feat_vals["twi"][i], 2),
                "dist_to_stream_m": clean(feat_vals["dist_to_stream_m"][i], 0),
            },
        })

    scored = [r for r in records if r["ffpi"] is not None]
    log(f"scored {len(scored)} of {len(records)} points")

    from collections import Counter
    log(f"  FFPI bands: {dict(Counter(r['ffpi_band'] for r in scored))}")
    if scored:
        vals = [r["ffpi"] for r in scored]
        log(f"  FFPI range {min(vals):.2f} .. {max(vals):.2f}  "
            f"mean {sum(vals) / len(vals):.2f}")

    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "resolution_m": args.res,
        "ffpi_scale": "1-10, higher = greater flash-flood potential (not a probability)",
        "model_note": "P(elevated hazard) from XGBoost trained on the state hazard "
                      "atlas; spatial-CV ROC-AUC 0.66, dominated by elevation. "
                      "Secondary signal only.",
        "locations": records,
    }

    os.makedirs(TABLE_ROOT, exist_ok=True)
    out_path = os.path.join(TABLE_ROOT, "location_scores.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log(f"wrote {out_path}")


if __name__ == "__main__":
    main()
