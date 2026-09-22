"""
Ablation: which feature set and weighting actually generalises to
unseen terrain?

Scored only under GroupKFold on the source polygon. The random-split
score is ~0.99 for every variant below and means nothing -- the atlas
has 189 polygons, so the effective sample size is 189, not 13,792.

Run this, not train_model.py, when changing features. It prints a table;
it writes nothing.
"""

import os
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.model_selection import GroupKFold
from sklearn.metrics import roc_auc_score, average_precision_score

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TABLE = os.path.join(REPO_ROOT, "floodsafe", "data", "tables", "training_90m.csv")

LOCAL_TERRAIN = ["slope_deg", "plan_curv", "prof_curv", "twi", "spi",
                 "dist_to_stream_m", "ruggedness"]
SOIL = ["soil_sand_pct", "soil_clay_pct", "soil_silt_pct", "soil_hsg"]
REGIONAL = ["elevation_m", "catchment_up_km2", "catchment_sub_km2",
            "sca", "flowacc_cells"]

BASE_PARAMS = {
    "objective": "binary:logistic", "eval_metric": "logloss",
    "subsample": 0.8, "colsample_bytree": 0.8, "tree_method": "hist",
}


def spatial_score(df, features, depth, weight_by_polygon, rounds=400,
                  min_child_weight=20, positives="all"):

    d = df
    if positives == "extreme_only":
        d = d[(d["label"] == 0) | (d["hazard_class"] == "EXTREME")]

    X = d[features].to_numpy(dtype="float32")
    y = d["label"].to_numpy(dtype="int32")
    groups = d["group_id"].to_numpy()

    if weight_by_polygon:
        # Each polygon contributes equally regardless of how many points
        # fell inside it, so a few huge LOW polygons can't dominate.
        counts = pd.Series(groups).map(pd.Series(groups).value_counts())
        w = (1.0 / counts).to_numpy(dtype="float32")
        w = w / w.mean()
    else:
        w = np.ones(len(y), dtype="float32")

    params = {**BASE_PARAMS, "max_depth": depth, "eta": 0.05,
              "min_child_weight": min_child_weight, "reg_lambda": 2.0}

    oof = np.full(len(y), np.nan)
    for tr, te in GroupKFold(n_splits=5).split(X, y, groups):
        if len(np.unique(y[tr])) < 2:
            continue
        dtr = xgb.DMatrix(X[tr], label=y[tr], weight=w[tr])
        dte = xgb.DMatrix(X[te])
        bst = xgb.train(params, dtr, num_boost_round=rounds)
        oof[te] = bst.predict(dte)

    m = np.isfinite(oof)
    return (roc_auc_score(y[m], oof[m]),
            average_precision_score(y[m], oof[m]),
            float(y.mean()))


def main():
    df = pd.read_csv(TABLE)
    df["soil_hsg"] = df["soil_hsg"].replace(0, np.nan)

    all_feats = LOCAL_TERRAIN + SOIL + REGIONAL

    variants = [
        ("baseline: all features, depth 4",        all_feats, 4, False, "all"),
        ("all features + polygon weights",         all_feats, 4, True,  "all"),
        ("local terrain only",                     LOCAL_TERRAIN, 4, False, "all"),
        ("local terrain + soil (no regional)",     LOCAL_TERRAIN + SOIL, 4, False, "all"),
        ("local+soil, polygon weights",            LOCAL_TERRAIN + SOIL, 4, True,  "all"),
        ("local+soil, depth 3, weights",           LOCAL_TERRAIN + SOIL, 3, True,  "all"),
        ("local+soil, depth 2, weights",           LOCAL_TERRAIN + SOIL, 2, True,  "all"),
        ("all feats, depth 2, weights",            all_feats, 2, True,  "all"),
        ("local+soil d3 w, EXTREME vs LOW only",   LOCAL_TERRAIN + SOIL, 3, True, "extreme_only"),
        ("all feats d3 w, EXTREME vs LOW only",    all_feats, 3, True, "extreme_only"),
    ]

    print(f"{'variant':44s} {'n_feat':>6s} {'ROC-AUC':>8s} {'PR-AUC':>7s} {'base':>6s}")
    print("-" * 78)

    results = []
    for name, feats, depth, weighted, positives in variants:
        auc, ap, base = spatial_score(df, feats, depth, weighted, positives=positives)
        results.append((auc, name))
        print(f"{name:44s} {len(feats):6d} {auc:8.3f} {ap:7.3f} {base:6.3f}")

    print("-" * 78)
    best_auc, best_name = max(results)
    print(f"best spatial ROC-AUC: {best_auc:.3f}  ({best_name})")


if __name__ == "__main__":
    main()
