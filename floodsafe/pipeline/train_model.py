"""
Train the flash-flood susceptibility model.

Learns P(elevated flash-flood hazard) from terrain, soil and catchment
predictors, using the hazard atlas as ground truth. The atlas only
covers ~9.7% of Uttarakhand's area, so the point of the model is
extrapolation: score the other 90% of the state, which currently comes
back UNMAPPED in the live system.

Validation
----------
Scored with GroupKFold on the source polygon. Points sampled inside one
polygon are near-duplicates, so a random split puts near-identical rows
on both sides and reports a meaningless score. Both are computed and
printed side by side -- the gap between them is the leakage a random
split would have hidden.

XGBoost is used directly rather than through a calibration wrapper;
binary:logistic with modest depth already produces usable probabilities
here, and the out-of-fold Brier score is reported to show it.

Usage:
    python train_model.py --res 90
"""

import argparse
import json
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.model_selection import GroupKFold, KFold
from sklearn.metrics import (
    roc_auc_score, average_precision_score, brier_score_loss,
    confusion_matrix,
)


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TABLE_ROOT = os.path.join(REPO_ROOT, "floodsafe", "data", "tables")
MODEL_ROOT = os.path.join(REPO_ROOT, "floodsafe", "models")

NON_FEATURES = {"group_id", "hazard_class", "label", "x", "y"}

PARAMS = {
    "objective": "binary:logistic",
    "eval_metric": "logloss",
    "max_depth": 4,
    "eta": 0.05,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "min_child_weight": 20,
    "reg_lambda": 2.0,
    "tree_method": "hist",
}
NUM_ROUNDS = 400
N_SPLITS = 5


def log(msg):
    print(f"[train] {msg}", flush=True)


def load_table(res):
    path = os.path.join(TABLE_ROOT, f"training_{res}m.csv")
    if not os.path.exists(path):
        raise SystemExit(f"missing {path}\nRun build_training_set.py first.")

    df = pd.read_csv(path)
    # 0 is the "no soil data" sentinel in the HSG raster, not group A.
    if "soil_hsg" in df.columns:
        df["soil_hsg"] = df["soil_hsg"].replace(0, np.nan)

    log(f"loaded {len(df):,} rows from {os.path.basename(path)}")
    log(f"  labels: {df['label'].value_counts().to_dict()}")
    log(f"  polygons: {df['group_id'].nunique()}")
    return df


def cross_validate(X, y, groups, splitter, feature_names, label):
    """Out-of-fold probabilities under one splitting scheme."""
    oof = np.full(len(y), np.nan)
    fold_aucs = []

    for fold, (tr, te) in enumerate(splitter.split(X, y, groups), 1):
        # A fold whose test side is single-class can't be scored.
        if len(np.unique(y[tr])) < 2:
            continue

        dtrain = xgb.DMatrix(X[tr], label=y[tr], feature_names=feature_names)
        dtest = xgb.DMatrix(X[te], feature_names=feature_names)

        booster = xgb.train(PARAMS, dtrain, num_boost_round=NUM_ROUNDS)
        oof[te] = booster.predict(dtest)

        if len(np.unique(y[te])) > 1:
            auc = roc_auc_score(y[te], oof[te])
            fold_aucs.append(auc)
            log(f"    fold {fold}: n_test={len(te):5d}  AUC={auc:.3f}")

    scored = np.isfinite(oof) & np.isin(y, [0, 1])
    metrics = {
        "roc_auc": float(roc_auc_score(y[scored], oof[scored])),
        "pr_auc": float(average_precision_score(y[scored], oof[scored])),
        "brier": float(brier_score_loss(y[scored], oof[scored])),
        "fold_auc_mean": float(np.mean(fold_aucs)) if fold_aucs else None,
        "fold_auc_std": float(np.std(fold_aucs)) if fold_aucs else None,
    }
    log(f"  {label}: ROC-AUC={metrics['roc_auc']:.3f}  "
        f"PR-AUC={metrics['pr_auc']:.3f}  Brier={metrics['brier']:.3f}")
    return oof, metrics


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--res", type=int, default=90)
    args = ap.parse_args()

    df = load_table(args.res)

    feature_names = [c for c in df.columns if c not in NON_FEATURES]
    X = df[feature_names].to_numpy(dtype="float32")
    y = df["label"].to_numpy(dtype="int32")
    groups = df["group_id"].to_numpy()

    log(f"features ({len(feature_names)}): {feature_names}")

    n_groups = df["group_id"].nunique()
    n_splits = min(N_SPLITS, n_groups)

    log(f"spatial CV (GroupKFold on polygon, {n_splits} folds):")
    _, spatial_metrics = cross_validate(
        X, y, groups, GroupKFold(n_splits=n_splits), feature_names, "spatial")

    log(f"random CV (KFold, {n_splits} folds) -- leaks neighbours, shown for contrast:")
    _, random_metrics = cross_validate(
        X, y, groups, KFold(n_splits=n_splits, shuffle=True, random_state=42),
        feature_names, "random")

    gap = random_metrics["roc_auc"] - spatial_metrics["roc_auc"]
    log(f"leakage gap (random - spatial ROC-AUC): {gap:+.3f}")

    # Final model on everything -- CV above is what the score claims come
    # from; this is the artifact that gets shipped.
    log("fitting final model on all rows ...")
    dall = xgb.DMatrix(X, label=y, feature_names=feature_names)
    booster = xgb.train(PARAMS, dall, num_boost_round=NUM_ROUNDS)

    gain = booster.get_score(importance_type="gain")
    ranked = sorted(gain.items(), key=lambda kv: kv[1], reverse=True)
    log("feature importance (gain):")
    for name, score in ranked:
        log(f"  {name:22s} {score:10.1f}")

    os.makedirs(MODEL_ROOT, exist_ok=True)
    model_path = os.path.join(MODEL_ROOT, f"susceptibility_{args.res}m.json")
    booster.save_model(model_path)

    # Threshold at 0.5 purely to report a readable confusion matrix; the
    # served product is the probability, not a hard class.
    oof_spatial, _ = cross_validate(
        X, y, groups, GroupKFold(n_splits=n_splits), feature_names, "spatial (repeat)")
    scored = np.isfinite(oof_spatial)
    tn, fp, fn, tp = confusion_matrix(y[scored], (oof_spatial[scored] >= 0.5).astype(int)).ravel()

    meta = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "resolution_m": args.res,
        "target": "P(elevated flash-flood hazard: EXTREME/SIGNIFICANT/MODERATE vs LOW)",
        "n_rows": int(len(df)),
        "n_polygons": int(n_groups),
        "label_counts": {str(k): int(v) for k, v in df["label"].value_counts().items()},
        "features": feature_names,
        "params": PARAMS,
        "num_rounds": NUM_ROUNDS,
        "validation": {
            "scheme": "GroupKFold on source hazard polygon",
            "spatial": spatial_metrics,
            "random_for_contrast": random_metrics,
            "leakage_gap_roc_auc": float(gap),
            "confusion_at_0.5": {"tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp)},
        },
        "feature_importance_gain": {k: float(v) for k, v in ranked},
    }

    meta_path = os.path.join(MODEL_ROOT, f"susceptibility_{args.res}m.meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    log(f"wrote {model_path}")
    log(f"wrote {meta_path}")


if __name__ == "__main__":
    main()
