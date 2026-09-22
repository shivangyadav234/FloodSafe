"""
Train the flash-flood susceptibility model.

Learns P(elevated flash-flood hazard) from terrain, soil and catchment
predictors, using the hazard atlas as ground truth. The atlas covers
only ~9.7% of Uttarakhand, so the model's job is extrapolation: score
the rest of the state.

How the data is divided
-----------------------
80% train / 20% test, split **by source hazard polygon**, not by row.

That distinction is the whole ballgame. The 13,792 rows come from 189
polygons, and points inside one polygon are near-duplicates of each
other -- same slope, same soil, same catchment, metres apart. A random
row-wise 80/20 puts near-identical rows on both sides, so the model is
tested on what it memorised and the score becomes meaningless. Splitting
on the polygon means every test polygon is terrain the model has never
seen, which is exactly the situation it faces in the 90% of the state
the atlas never mapped.

Both splits are computed and reported side by side. The gap between
them is the leakage a row-wise split would have hidden.

Hyperparameters are searched with GroupKFold *inside the training 80%*,
so the test 20% is touched exactly once, at the end. The decision
threshold is chosen on the training folds for the same reason.

Usage:
    python train_model.py --res 90
    python train_model.py --res 90 --quick     # skip the search
"""

import argparse
import json
import os
from datetime import datetime, timezone
from itertools import product

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.model_selection import GroupShuffleSplit, GroupKFold, train_test_split
from sklearn.metrics import (
    roc_auc_score, average_precision_score, brier_score_loss,
    confusion_matrix, f1_score, balanced_accuracy_score,
)


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TABLE_ROOT = os.path.join(REPO_ROOT, "floodsafe", "data", "tables")
MODEL_ROOT = os.path.join(REPO_ROOT, "floodsafe", "models")

NON_FEATURES = {"group_id", "hazard_class", "label", "x", "y"}

TEST_SIZE = 0.20
SEED = 42

BASE_PARAMS = {
    "objective": "binary:logistic",
    "eval_metric": "logloss",
    "tree_method": "hist",
}

# Deliberately small. With 189 effective samples a wide search would
# just overfit the validation folds.
SEARCH_GRID = {
    "max_depth": [2, 3, 4],
    "min_child_weight": [10, 20, 40],
    "eta": [0.03, 0.05],
    "subsample": [0.8],
    "colsample_bytree": [0.6, 0.8],
    "reg_lambda": [2.0, 5.0],
}
SEARCH_ROUNDS = 300
FINAL_ROUNDS = 400


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


def polygon_weights(groups):
    """Weight rows so each polygon counts equally.

    Without this the handful of very large LOW polygons dominate: they
    cover ~531,000 ha against EXTREME's ~2,000 ha.
    """
    counts = pd.Series(groups).map(pd.Series(groups).value_counts())
    w = (1.0 / counts).to_numpy(dtype="float32")
    return w / w.mean()


def fit(params, X, y, w, rounds, feature_names=None):
    dtrain = xgb.DMatrix(X, label=y, weight=w, feature_names=feature_names)
    return xgb.train(params, dtrain, num_boost_round=rounds)


def predict(booster, X, feature_names=None):
    return booster.predict(xgb.DMatrix(X, feature_names=feature_names))


def out_of_fold(params, X, y, groups, w, rounds, feature_names=None):
    """Out-of-fold predictions over the training set.

    Anything chosen from in-sample predictions is chosen from what the
    model memorised: picking the decision threshold that way scored F1
    0.945 on training and 0.213 on the test polygons.
    """
    oof = np.full(len(y), np.nan)
    for tr, va in GroupKFold(n_splits=5).split(X, y, groups):
        if len(np.unique(y[tr])) < 2:
            continue
        booster = fit(params, X[tr], y[tr], w[tr], rounds, feature_names)
        oof[va] = predict(booster, X[va], feature_names)
    return oof


def search_hyperparameters(X, y, groups, w):
    """GroupKFold inside the training set only."""
    keys = list(SEARCH_GRID)
    combos = [dict(zip(keys, v)) for v in product(*(SEARCH_GRID[k] for k in keys))]
    log(f"searching {len(combos)} configurations with 5-fold GroupKFold "
        f"on the training set ...")

    splitter = GroupKFold(n_splits=5)
    folds = list(splitter.split(X, y, groups))

    best, best_auc = None, -1.0
    for i, combo in enumerate(combos, 1):
        params = {**BASE_PARAMS, **combo}
        oof = np.full(len(y), np.nan)

        for tr, va in folds:
            if len(np.unique(y[tr])) < 2:
                continue
            booster = fit(params, X[tr], y[tr], w[tr], SEARCH_ROUNDS)
            oof[va] = predict(booster, X[va])

        mask = np.isfinite(oof)
        auc = roc_auc_score(y[mask], oof[mask])
        if auc > best_auc:
            best, best_auc = combo, auc
            log(f"  [{i:2d}/{len(combos)}] new best AUC {auc:.4f}  {combo}")

    log(f"best config: {best}  (inner CV AUC {best_auc:.4f})")
    return {**BASE_PARAMS, **best}, best_auc


def pick_threshold(y, prob):
    """Threshold maximising F1, chosen on training data only."""
    best_t, best_f1 = 0.5, -1.0
    for t in np.arange(0.05, 0.95, 0.01):
        f1 = f1_score(y, (prob >= t).astype(int), zero_division=0)
        if f1 > best_f1:
            best_t, best_f1 = float(t), f1
    return round(best_t, 3), round(best_f1, 4)


def evaluate(y, prob, threshold, label):
    pred = (prob >= threshold).astype(int)
    tn, fp, fn, tp = confusion_matrix(y, pred, labels=[0, 1]).ravel()
    majority = max((tn + fp), (tp + fn)) / max(len(y), 1)

    m = {
        "roc_auc": float(roc_auc_score(y, prob)),
        "pr_auc": float(average_precision_score(y, prob)),
        "brier": float(brier_score_loss(y, prob)),
        "threshold": threshold,
        "accuracy": float((tp + tn) / max(len(y), 1)),
        "balanced_accuracy": float(balanced_accuracy_score(y, pred)),
        "precision": float(tp / max(tp + fp, 1)),
        "recall": float(tp / max(tp + fn, 1)),
        "f1": float(f1_score(y, pred, zero_division=0)),
        "majority_baseline_accuracy": float(majority),
        "confusion": {"tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp)},
        "n": int(len(y)),
        "positives": int(y.sum()),
    }

    log(f"  {label}")
    log(f"    ROC-AUC {m['roc_auc'] * 100:5.1f}%   PR-AUC {m['pr_auc'] * 100:5.1f}%   "
        f"Brier {m['brier']:.3f}")
    log(f"    accuracy {m['accuracy'] * 100:5.1f}%  (majority baseline "
        f"{m['majority_baseline_accuracy'] * 100:.1f}%)")
    log(f"    balanced acc {m['balanced_accuracy'] * 100:5.1f}%   "
        f"precision {m['precision'] * 100:5.1f}%   recall {m['recall'] * 100:5.1f}%   "
        f"F1 {m['f1'] * 100:5.1f}%")
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--res", type=int, default=90)
    ap.add_argument("--quick", action="store_true",
                    help="skip the hyperparameter search")
    args = ap.parse_args()

    df = load_table(args.res)

    feature_names = [c for c in df.columns if c not in NON_FEATURES]
    X = df[feature_names].to_numpy(dtype="float32")
    y = df["label"].to_numpy(dtype="int32")
    groups = df["group_id"].to_numpy()

    log(f"features ({len(feature_names)}): {feature_names}")

    # ---- 80/20, split by polygon ----
    gss = GroupShuffleSplit(n_splits=1, test_size=TEST_SIZE, random_state=SEED)
    train_idx, test_idx = next(gss.split(X, y, groups))

    log("")
    log(f"SPLIT: 80/20 grouped by hazard polygon")
    log(f"  train {len(train_idx):,} rows / {len(np.unique(groups[train_idx]))} polygons"
        f"  (positives {int(y[train_idx].sum()):,})")
    log(f"  test  {len(test_idx):,} rows / {len(np.unique(groups[test_idx]))} polygons"
        f"  (positives {int(y[test_idx].sum()):,})")

    w = polygon_weights(groups)

    if args.quick:
        params = {**BASE_PARAMS, "max_depth": 3, "min_child_weight": 20,
                  "eta": 0.05, "subsample": 0.8, "colsample_bytree": 0.8,
                  "reg_lambda": 2.0}
        inner_auc = None
    else:
        params, inner_auc = search_hyperparameters(
            X[train_idx], y[train_idx], groups[train_idx], w[train_idx])

    booster = fit(params, X[train_idx], y[train_idx], w[train_idx],
                  FINAL_ROUNDS, feature_names)

    # Threshold from out-of-fold predictions within the training set, so
    # it is chosen on polygons the scoring model had not seen. The test
    # 20% is still untouched at this point.
    oof = out_of_fold(params, X[train_idx], y[train_idx], groups[train_idx],
                      w[train_idx], FINAL_ROUNDS, feature_names)
    mask = np.isfinite(oof)
    threshold, oof_f1 = pick_threshold(y[train_idx][mask], oof[mask])
    log(f"decision threshold {threshold} (F1 {oof_f1:.3f} out-of-fold on training)")

    log("")
    log("RESULTS")
    test_prob = predict(booster, X[test_idx], feature_names)
    grouped = evaluate(y[test_idx], test_prob, threshold,
                       "held-out 20% — unseen polygons (HONEST)")

    # ---- the same 80/20 done row-wise, for contrast ----
    log("")
    log("contrast: the identical 80/20 split done row-wise instead")
    rtr, rte = train_test_split(np.arange(len(y)), test_size=TEST_SIZE,
                                random_state=SEED, stratify=y)
    rbooster = fit(params, X[rtr], y[rtr], w[rtr], FINAL_ROUNDS, feature_names)
    rprob = predict(rbooster, X[rte], feature_names)
    random_split = evaluate(y[rte], rprob, threshold,
                            "held-out 20% — random rows (LEAKS, do not quote)")

    gap = random_split["roc_auc"] - grouped["roc_auc"]
    log("")
    log(f"leakage gap: {gap * 100:+.1f} ROC-AUC points "
        f"({grouped['roc_auc'] * 100:.1f}% honest vs "
        f"{random_split['roc_auc'] * 100:.1f}% leaked)")

    # ---- how much does the 80/20 number depend on which split you drew? ----
    #
    # With 189 polygons the test side of an 80/20 is ~38 polygons, which
    # is small enough that a single split is mostly noise. Repeating it
    # across seeds is the only way to see that, and it is worth seeing:
    # quoting one lucky seed would be indistinguishable from cheating.
    log("")
    log("stability: repeating the 80/20 grouped split across 25 seeds ...")
    repeat_aucs = []
    for seed in range(25):
        tr, te = next(GroupShuffleSplit(
            n_splits=1, test_size=TEST_SIZE, random_state=seed).split(X, y, groups))
        if len(np.unique(y[te])) < 2:
            continue
        b = fit(params, X[tr], y[tr], w[tr], FINAL_ROUNDS, feature_names)
        repeat_aucs.append(float(roc_auc_score(
            y[te], predict(b, X[te], feature_names))))

    arr = np.array(repeat_aucs)
    stability = {
        "n_seeds": int(len(arr)),
        "roc_auc_mean": float(arr.mean()),
        "roc_auc_std": float(arr.std()),
        "roc_auc_min": float(arr.min()),
        "roc_auc_max": float(arr.max()),
    }
    log(f"  ROC-AUC {arr.mean() * 100:.1f}% ± {arr.std() * 100:.1f} "
        f"(min {arr.min() * 100:.1f}%, max {arr.max() * 100:.1f}%)")
    log(f"  a single 80/20 draw can land anywhere in that range, so the "
        f"cross-validated figure is the one to quote")

    gain = booster.get_score(importance_type="gain")
    ranked = sorted(gain.items(), key=lambda kv: kv[1], reverse=True)
    log("")
    log("feature importance (gain), top 10:")
    for name, score in ranked[:10]:
        log(f"  {name:22s} {score:10.1f}")

    os.makedirs(MODEL_ROOT, exist_ok=True)
    model_path = os.path.join(MODEL_ROOT, f"susceptibility_{args.res}m.json")
    booster.save_model(model_path)

    meta = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "resolution_m": args.res,
        "target": "P(elevated flash-flood hazard: EXTREME/SIGNIFICANT/MODERATE vs LOW)",
        "n_rows": int(len(df)),
        "n_polygons": int(df["group_id"].nunique()),
        "label_counts": {str(k): int(v) for k, v in df["label"].value_counts().items()},
        "features": feature_names,
        "params": params,
        "num_rounds": FINAL_ROUNDS,
        "inner_cv_auc": inner_auc,
        "split": {
            "scheme": "80/20 GroupShuffleSplit on source hazard polygon",
            "test_size": TEST_SIZE,
            "seed": SEED,
            "train_rows": int(len(train_idx)),
            "test_rows": int(len(test_idx)),
            "train_polygons": int(len(np.unique(groups[train_idx]))),
            "test_polygons": int(len(np.unique(groups[test_idx]))),
        },
        "results": {
            "held_out_grouped": grouped,
            "held_out_random_for_contrast": random_split,
            "leakage_gap_roc_auc": float(gap),
            "split_stability_25_seeds": stability,
        },
        "feature_importance_gain": {k: float(v) for k, v in ranked},
    }

    meta_path = os.path.join(MODEL_ROOT, f"susceptibility_{args.res}m.meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    log("")
    log(f"wrote {model_path}")
    log(f"wrote {meta_path}")


if __name__ == "__main__":
    main()
