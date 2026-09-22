"""
Train a susceptibility model on real recorded disasters instead of the
hazard atlas.

Why this exists
---------------
The atlas-trained model plateaus near ROC-AUC 0.65 no matter what is
thrown at it, because the atlas classes separate on elevation and
almost nothing else -- against them, slope scores p=0.62 and TWI
p=0.11. Terrain cannot learn from labels that carry no terrain signal.

Against real events the same variables come alive: distance-to-stream
reaches p=1e-09 and FFPI 70% AUC. So the labels change here, from
"where an atlas says hazard is" to "where rainfall-triggered mass
movements actually happened" -- 206 catalogued events in Uttarakhand
with 5,276 recorded deaths, from NASA's Global Landslide Catalog.

Design decisions that keep the number honest
--------------------------------------------
Background points are settlement-matched. The catalog is news-sourced,
so events cluster where people can report them. Scoring against uniform
wilderness background measures accessibility as much as hazard -- it
moves FFPI's apparent AUC from 70% to 54%. Background is therefore
drawn within the same distance of settlements as the events.

Cross-validation is spatially blocked. Events cluster in valleys, so a
random split would put neighbouring events on both sides, the same
leakage that inflated the atlas model from 0.66 to 0.99. Rows are
grouped into ~25 km blocks and whole blocks are held out.

Location accuracy is a parameter, not an assumption. The catalog locates
events to between 1 km and 50 km; --accuracy controls which are trusted.

Usage:
    python train_event_model.py --res 90
    python train_event_model.py --res 90 --accuracy 5km
"""

import argparse
import json
import os
from datetime import datetime, timezone

import _proj  # noqa: F401

import numpy as np
import pandas as pd
import rasterio
import geopandas as gpd
import xgboost as xgb
from shapely.geometry import shape, Point
from shapely.ops import unary_union
from sklearn.model_selection import GroupKFold
from sklearn.metrics import (
    roc_auc_score, average_precision_score, brier_score_loss,
    confusion_matrix, f1_score, balanced_accuracy_score,
)


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_DATA = os.path.join(REPO_ROOT, "data", "flood", "uttarakhand", "data")
EVENTS_SHP = os.path.join(REPO_ROOT, "floodsafe", "data", "events", "glc",
                          "global_landslide_catalog_NASA.shp")
TERRAIN_ROOT = os.path.join(REPO_ROOT, "floodsafe", "data", "terrain")
MODEL_ROOT = os.path.join(REPO_ROOT, "floodsafe", "models")

RAINFALL_TRIGGERS = {"downpour", "rain", "continuous_rain", "monsoon",
                     "tropical_cyclone", "flooding"}
ACCURACY_ORDER = ["1km", "5km", "10km", "25km", "50km"]

FEATURES = {
    "elevation_m": "dem.tif", "slope_deg": "slope.tif",
    "plan_curv": "plan_curv.tif", "prof_curv": "prof_curv.tif",
    "twi": "twi.tif", "spi": "spi.tif", "sca": "sca.tif",
    "dist_to_stream_m": "dist_to_stream.tif", "ruggedness": "ruggedness.tif",
    "hand_m": "hand.tif", "dev_elev_small": "dev_elev_small.tif",
    "dev_elev_large": "dev_elev_large.tif", "elev_percentile": "elev_percentile.tif",
    "rel_topo_position": "rel_topo_position.tif",
    "soil_sand_pct": "soil_sand.tif", "soil_clay_pct": "soil_clay.tif",
    "soil_silt_pct": "soil_silt.tif", "soil_hsg": "soil_hsg.tif",
    "ffpi": "ffpi.tif",
}

BACKGROUND_PER_POSITIVE = 10
MIN_BACKGROUND_DIST_KM = 5.0
SETTLEMENT_MATCH_KM = 10.0
BLOCK_DEG = 0.25          # ~25 km spatial CV blocks
SEED = 42

PARAMS = {
    "objective": "binary:logistic", "eval_metric": "logloss",
    "tree_method": "hist", "max_depth": 3, "eta": 0.05,
    "subsample": 0.8, "colsample_bytree": 0.8,
    "min_child_weight": 10, "reg_lambda": 3.0,
}
ROUNDS = 300


def log(msg):
    print(f"[eventmodel] {msg}", flush=True)


def state_geometry():
    with open(os.path.join(APP_DATA, "uttarakhand_boundary.geojson"),
              "r", encoding="utf-8") as f:
        gj = json.load(f)
    return unary_union([shape(f["geometry"]) for f in gj["features"]])


def haversine_min(lat, lon, ref):
    p = np.radians([lat, lon])
    dphi = ref[:, 0] - p[0]
    dlam = ref[:, 1] - p[1]
    a = (np.sin(dphi / 2) ** 2
         + np.cos(p[0]) * np.cos(ref[:, 0]) * np.sin(dlam / 2) ** 2)
    return float(np.min(2 * 6371.0 * np.arcsin(np.sqrt(a))))


def build_points(state, accuracy_cut, rng):
    g = gpd.read_file(EVENTS_SHP)
    uk = g.cx[77.5:81.1, 28.7:31.5].copy()
    uk = uk[uk.geometry.within(state)]
    uk["trigger"] = uk["landslid_1"].fillna("unknown")
    uk["accuracy"] = uk["location_a"].fillna("unknown")

    rain = uk[uk["trigger"].isin(RAINFALL_TRIGGERS)]
    keep = ACCURACY_ORDER[:ACCURACY_ORDER.index(accuracy_cut) + 1] + ["exact"]
    pos = rain[rain["accuracy"].isin(keep)].copy()

    log(f"positives: {len(pos)} rainfall-triggered events with accuracy <= {accuracy_cut}")

    positives = pd.DataFrame({"lat": pos.geometry.y.to_numpy(),
                              "lon": pos.geometry.x.to_numpy(), "label": 1})

    with open(os.path.join(APP_DATA, "localities.json"), "r", encoding="utf-8") as f:
        loc = pd.DataFrame(json.load(f))

    ev_ref = np.radians(np.column_stack([rain.geometry.y, rain.geometry.x]))
    loc_ref = np.radians(np.column_stack([loc["lat"], loc["lon"]]))

    minx, miny, maxx, maxy = state.bounds
    want = len(positives) * BACKGROUND_PER_POSITIVE
    rows, guard = [], 0
    while len(rows) < want and guard < want * 500:
        guard += 1
        lon = rng.uniform(minx, maxx)
        lat = rng.uniform(miny, maxy)
        if not state.contains(Point(lon, lat)):
            continue
        if haversine_min(lat, lon, ev_ref) < MIN_BACKGROUND_DIST_KM:
            continue
        if haversine_min(lat, lon, loc_ref) > SETTLEMENT_MATCH_KM:
            continue
        rows.append((lat, lon, 0))

    background = pd.DataFrame(rows, columns=["lat", "lon", "label"])
    log(f"background: {len(background)} settlement-matched points "
        f"(>= {MIN_BACKGROUND_DIST_KM} km from any event)")

    return pd.concat([positives, background], ignore_index=True)


def sample_features(df, out_dir):
    from rasterio.warp import transform as warp_transform

    with rasterio.open(os.path.join(out_dir, "dem.tif")) as ref:
        transform, height, width, crs = ref.transform, ref.height, ref.width, ref.crs

    xs, ys = warp_transform("EPSG:4326", crs, df["lon"].tolist(), df["lat"].tolist())
    cols = ((np.array(xs) - transform.c) / transform.a).astype("int64")
    rows = ((np.array(ys) - transform.f) / transform.e).astype("int64")
    inside = (rows >= 0) & (rows < height) & (cols >= 0) & (cols < width)
    sr = np.clip(rows, 0, height - 1)
    sc = np.clip(cols, 0, width - 1)

    for name, fname in FEATURES.items():
        path = os.path.join(out_dir, fname)
        if not os.path.exists(path):
            log(f"  WARNING missing {fname}")
            continue
        with rasterio.open(path) as src:
            band = src.read(1)
            nodata = src.nodata
        vals = band[sr, sc].astype("float64")
        vals[~inside] = np.nan
        if nodata is not None:
            vals = np.where(vals == nodata, np.nan, vals)
        vals = np.where(vals < -1e30, np.nan, vals)
        if name == "soil_hsg":
            vals = np.where(vals == 0, np.nan, vals)
        df[name] = vals

    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--res", type=int, default=90)
    ap.add_argument("--accuracy", default="10km", choices=ACCURACY_ORDER)
    args = ap.parse_args()

    out_dir = os.path.join(TERRAIN_ROOT, f"{args.res}m")
    rng = np.random.default_rng(SEED)
    state = state_geometry()

    df = build_points(state, args.accuracy, rng)
    df = sample_features(df, out_dir)

    feature_names = [c for c in FEATURES if c in df.columns]
    before = len(df)
    df = df.dropna(subset=["elevation_m", "slope_deg"])
    log(f"dropped {before - len(df)} rows outside raster coverage; {len(df)} remain")

    # Spatial blocks: whole ~25 km cells are held out together, so
    # neighbouring events cannot straddle a fold boundary.
    df["block"] = (((df["lat"] / BLOCK_DEG).astype(int).astype(str)) + "_"
                   + ((df["lon"] / BLOCK_DEG).astype(int).astype(str)))
    log(f"spatial blocks: {df['block'].nunique()} at {BLOCK_DEG} deg (~25 km)")
    log(f"labels: {df['label'].value_counts().to_dict()}")

    X = df[feature_names].to_numpy("float32")
    y = df["label"].to_numpy("int32")
    groups = df["block"].to_numpy()

    n_splits = min(5, df["block"].nunique())
    oof = np.full(len(y), np.nan)
    for tr, te in GroupKFold(n_splits=n_splits).split(X, y, groups):
        if len(np.unique(y[tr])) < 2:
            continue
        booster = xgb.train(PARAMS, xgb.DMatrix(X[tr], label=y[tr],
                                                feature_names=feature_names),
                            num_boost_round=ROUNDS)
        oof[te] = booster.predict(xgb.DMatrix(X[te], feature_names=feature_names))

    mask = np.isfinite(oof)
    auc = roc_auc_score(y[mask], oof[mask])
    ap_ = average_precision_score(y[mask], oof[mask])
    brier = brier_score_loss(y[mask], oof[mask])

    best_t, best_f1 = 0.5, -1
    for t in np.arange(0.05, 0.95, 0.01):
        f1 = f1_score(y[mask], (oof[mask] >= t).astype(int), zero_division=0)
        if f1 > best_f1:
            best_t, best_f1 = float(t), f1

    pred = (oof[mask] >= best_t).astype(int)
    tn, fp, fn, tp = confusion_matrix(y[mask], pred, labels=[0, 1]).ravel()
    base = max(tn + fp, tp + fn) / max(len(pred), 1)

    log("")
    log("SPATIALLY BLOCKED CROSS-VALIDATION (honest)")
    log(f"  ROC-AUC {auc * 100:5.1f}%   PR-AUC {ap_ * 100:5.1f}%   Brier {brier:.3f}")
    log(f"  balanced acc {balanced_accuracy_score(y[mask], pred) * 100:5.1f}%   "
        f"precision {tp / max(tp + fp, 1) * 100:5.1f}%   "
        f"recall {tp / max(tp + fn, 1) * 100:5.1f}%   F1 {best_f1 * 100:5.1f}%")
    log(f"  accuracy {(tp + tn) / max(len(pred), 1) * 100:5.1f}%  "
        f"(majority baseline {base * 100:.1f}%)")

    # FFPI alone, on the identical points, as the reference to beat.
    ffpi_auc = roc_auc_score(y[mask], df["ffpi"].to_numpy()[mask]) \
        if "ffpi" in df.columns else None
    if ffpi_auc:
        log(f"  FFPI alone on the same points: {ffpi_auc * 100:.1f}% "
            f"(model adds {(auc - ffpi_auc) * 100:+.1f} points)")

    booster = xgb.train(PARAMS, xgb.DMatrix(X, label=y, feature_names=feature_names),
                        num_boost_round=ROUNDS)
    gain = booster.get_score(importance_type="gain")
    ranked = sorted(gain.items(), key=lambda kv: kv[1], reverse=True)
    log("")
    log("feature importance (gain), top 10:")
    for n, s in ranked[:10]:
        log(f"  {n:20s} {s:9.1f}")

    os.makedirs(MODEL_ROOT, exist_ok=True)
    model_path = os.path.join(MODEL_ROOT, f"event_susceptibility_{args.res}m.json")
    booster.save_model(model_path)

    meta = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "resolution_m": args.res,
        "target": "P(rainfall-triggered mass movement) from real recorded events",
        "label_source": "NASA Global Landslide Catalog (1970-2019) via HDX",
        "accuracy_cut": args.accuracy,
        "n_positive": int(y.sum()), "n_background": int((y == 0).sum()),
        "background": "settlement-matched, >=5km from any event",
        "features": feature_names,
        "params": PARAMS, "rounds": ROUNDS,
        "validation": {
            "scheme": f"GroupKFold on {BLOCK_DEG} deg spatial blocks",
            "n_blocks": int(df["block"].nunique()),
            "roc_auc": float(auc), "pr_auc": float(ap_), "brier": float(brier),
            "threshold": best_t, "f1": float(best_f1),
            "balanced_accuracy": float(balanced_accuracy_score(y[mask], pred)),
            "precision": float(tp / max(tp + fp, 1)),
            "recall": float(tp / max(tp + fn, 1)),
            "accuracy": float((tp + tn) / max(len(pred), 1)),
            "majority_baseline_accuracy": float(base),
            "confusion": {"tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp)},
            "ffpi_alone_auc": float(ffpi_auc) if ffpi_auc else None,
        },
        "feature_importance_gain": {k: float(v) for k, v in ranked},
    }
    meta_path = os.path.join(MODEL_ROOT, f"event_susceptibility_{args.res}m.meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    log("")
    log(f"wrote {model_path}")
    log(f"wrote {meta_path}")


if __name__ == "__main__":
    main()
