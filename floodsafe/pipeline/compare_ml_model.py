"""
Compare machine-learning flood-day models with the calibrated rainfall
rule, on exactly the same data and the same test.

The live FFGS status comes from a rule: rain measured against each
place's own climate, with WATCH/CRITICAL set at a chosen share of dry
monsoon days (calibrate_thresholds.py, build_calibrated_thresholds.py).
The obvious question is whether a trained model would catch more floods.
This answers it without tilting the comparison either way:

  - Same rows: the district-days, labels and exclusions of
    calibrate_thresholds.py -- IMD-recorded rain-driven floods from the
    India Flood Inventory (positives, scored over each event's span)
    against every other monsoon district-day, 2000-2023.
  - Same test: leave-one-year-out. Each year is scored by a model
    trained on the other 23 years, as the rule is.
  - Same operating points: flags are set so that 10% (CRITICAL) and 20%
    (WATCH) of *training-year* dry days would fire, the rule's
    "balanced" setting. For a model, that cut is taken from
    out-of-fold predictions on the training years (grouped by year), not
    from in-sample predictions, which would be over-confident and fire
    more often on the held-out year than intended.

Models:
  xgb_rain_only  gradient-boosted trees on the rule's own information:
                 1h / 3h / 24h rain in mm and relative to local climate.
  xgb_all        the same plus 5-day antecedent rain, the district and
                 the day of the season.
  linear_all     a linear logistic model on the same features as
                 xgb_all (XGBoost's linear booster, standardised inputs).

Reported per method: ROC AUC of the pooled held-out scores; floods
caught (POD) and dry days flagged (POFD) at the two operating points;
and, for each model, a bootstrap 95% interval for its POD minus the
rule's, resampling flood rows, so a gain within the noise of 156 events
is not mistaken for a real one.

It needs the ERA5 cache and the inventory that calibrate_thresholds.py
uses (floodsafe/data/, not in git). Writes floodsafe/models/ml_comparison.json.

Usage:
    python compare_ml_model.py
"""

import json
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import calibrate_thresholds as C  # noqa: E402

OUT = os.path.join(C.REPO, "floodsafe", "models", "ml_comparison.json")

TARGETS = {"critical": 0.10, "watch": 0.20}
INNER_GROUPS = 5
BOOTSTRAP = 2000
SEED = 7

RAIN_FEATURES = ["1h", "3h", "24h", "1h_rel", "3h_rel", "24h_rel"]

XGB_TREE = {
    "objective": "binary:logistic", "eval_metric": "logloss", "tree_method": "hist",
    "max_depth": 3, "eta": 0.05, "subsample": 0.8, "colsample_bytree": 0.8,
    "min_child_weight": 5, "reg_lambda": 3.0, "seed": SEED,
}
XGB_LINEAR = {
    "objective": "binary:logistic", "eval_metric": "logloss", "booster": "gblinear",
    "lambda": 1.0, "alpha": 0.0, "updater": "coord_descent", "seed": SEED,
}
ROUNDS = {"tree": 300, "linear": 200}


# ------------------------------------------------------------------
# Features
# ------------------------------------------------------------------

def feature_table(data):
    """Model inputs for every row, all numeric."""
    table = data[RAIN_FEATURES + ["antecedent_5d"]].astype(float).copy()
    days = pd.to_datetime(data["day"])
    table["day_of_season"] = (days - pd.to_datetime(days.dt.year.astype(str) + "-06-01")).dt.days.astype(float)
    districts = pd.get_dummies(data["district"], prefix="district", dtype=float)
    return pd.concat([table, districts], axis=1)


MODELS = {
    "xgb_rain_only": ("tree", lambda columns: RAIN_FEATURES),
    "xgb_all": ("tree", lambda columns: list(columns)),
    "linear_all": ("linear", lambda columns: list(columns)),
}


def fit_predict(kind, x_train, y_train, x_test, rounds=None):
    import xgboost as xgb

    if kind == "linear":
        # Standardise on the training rows only; means and spreads are
        # elementwise, no matrix products.
        mean = x_train.mean(axis=0)
        spread = x_train.std(axis=0)
        spread[spread == 0] = 1.0
        x_train, x_test = (x_train - mean) / spread, (x_test - mean) / spread

    params = XGB_LINEAR if kind == "linear" else XGB_TREE
    if kind == "linear":
        # With ~0.4% positives the L2 penalty otherwise holds every
        # weight near zero: predictions differ in the fourth decimal and
        # tie, and ranking by them is noise. Balancing the classes is the
        # standard fix for an imbalanced logistic model; it leaves the
        # ranking, which is all the comparison uses, free to form.
        positives = max(1, int((y_train == 1).sum()))
        params = dict(params, scale_pos_weight=float((y_train == 0).sum()) / positives)
    booster = xgb.train(params, xgb.DMatrix(x_train, label=y_train),
                        num_boost_round=rounds or ROUNDS[kind])
    return booster.predict(xgb.DMatrix(x_test)), booster


# ------------------------------------------------------------------
# Leave-one-year-out
# ------------------------------------------------------------------

def year_groups(years, groups):
    """Split the training years into `groups` interleaved sets."""
    ordered = sorted(years)
    return [ordered[i::groups] for i in range(groups)]


def model_leave_one_year_out(data, features, kind, rounds=None):
    """
    Held-out scores for every row, and flags at each operating point
    with the cut chosen from out-of-fold scores on the training years.
    """
    x = features.to_numpy(dtype=float)
    y = data["label"].to_numpy()
    years = data["year"].to_numpy()

    scores = np.zeros(len(data))
    flags = {level: np.zeros(len(data), bool) for level in TARGETS}

    for year in sorted(set(years)):
        test = years == year
        train = ~test
        scores[test], _ = fit_predict(kind, x[train], y[train], x[test], rounds)

        # Out-of-fold scores on the training years, grouped by year.
        oof = np.zeros(train.sum())
        train_years = years[train]
        for group in year_groups(set(train_years), INNER_GROUPS):
            held = np.isin(train_years, group)
            oof[held], _ = fit_predict(kind, x[train][~held], y[train][~held], x[train][held], rounds)

        dry = y[train] == 0
        for level, target in TARGETS.items():
            cut = float(np.quantile(oof[dry], 1.0 - target))
            flags[level][test] = scores[test] > cut

    return scores, flags


def rule_leave_one_year_out(data):
    """The live rule's held-out scores and flags, as calibrate_thresholds.py computes them."""
    relative = [w + "_rel" for w in C.WINDOWS]
    score = C.any_window_score(data, relative)
    years = data["year"].to_numpy()

    scores = np.zeros(len(data))
    for year in sorted(set(years)):
        test = years == year
        scores[test] = score(data[~test], data[test])

    flags = {level: C.fixed_false_alarm_rule(data, relative, target) for level, target in TARGETS.items()}
    return scores, flags


# ------------------------------------------------------------------
# Comparison
# ------------------------------------------------------------------

def pod_difference_interval(model_flags, rule_flags, truth, rng):
    """Bootstrap 95% interval for POD(model) - POD(rule), resampling flood rows."""
    positives = np.flatnonzero(truth == 1)
    diffs = np.empty(BOOTSTRAP)
    for i in range(BOOTSTRAP):
        sample = rng.choice(positives, size=len(positives), replace=True)
        diffs[i] = model_flags[sample].mean() - rule_flags[sample].mean()
    low, high = np.quantile(diffs, [0.025, 0.975])
    return [round(float(low), 3), round(float(high), 3)]


def verdict(diff, interval):
    points = round(diff * 100, 1)
    if interval[0] > 0:
        return f"catches {points} points more floods; the gain is larger than the sampling noise"
    if interval[1] < 0:
        return f"catches {-points} points fewer floods; the loss is larger than the sampling noise"
    return f"within the sampling noise of the rule ({points:+} points)"


def compare(data, rounds=None, models=MODELS):
    truth = data["label"].to_numpy()
    features = feature_table(data)
    rng = np.random.default_rng(SEED)

    results = {}
    rule_scores, rule_flags = rule_leave_one_year_out(data)
    results["rule"] = {
        "description": "Calibrated rainfall rule used live: rarity against local climate, any of 1h/3h/24h",
        "roc_auc": round(C.roc_auc(rule_scores, truth), 3),
        **{level: C.scores(rule_flags[level], truth) for level in TARGETS},
    }

    for name, (kind, pick) in models.items():
        columns = pick(features.columns)
        scores, flags = model_leave_one_year_out(data, features[columns], kind, rounds)
        entry = {
            "description": f"{'Gradient-boosted trees' if kind == 'tree' else 'Linear logistic'} on {len(columns)} features",
            "features": columns if len(columns) <= 12 else columns[:8] + ["district one-hot ..."],
            "roc_auc": round(C.roc_auc(scores, truth), 3),
        }
        for level in TARGETS:
            level_scores = C.scores(flags[level], truth)
            interval = pod_difference_interval(flags[level], rule_flags[level], truth, rng)
            diff = level_scores["POD"] - results["rule"][level]["POD"]
            entry[level] = dict(level_scores, POD_minus_rule=round(diff, 3),
                                POD_minus_rule_95ci=interval, verdict=verdict(diff, interval))
        results[name] = entry

    # What the full model leans on, fitted on every year.
    _, booster = fit_predict("tree", features.to_numpy(dtype=float), truth, features.to_numpy(dtype=float)[:1], rounds)
    gain = booster.get_score(importance_type="gain")
    named = {features.columns[int(k[1:])]: v for k, v in gain.items()}
    total = sum(named.values()) or 1.0
    results["xgb_all_feature_importance_share"] = {
        k: round(v / total, 3) for k, v in sorted(named.items(), key=lambda kv: -kv[1])[:10]}

    return results


def main():
    events = C.load_events()
    rainfall = C.load_district_rainfall()
    data = C.label(C.add_relative_predictors(C.district_days(rainfall), rainfall), events)
    truth = data["label"].to_numpy()

    report = {
        "method": __doc__.strip().split("\n\n")[0],
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "validation": {
            "scheme": "leave-one-year-out; district-days; June-September 2000-2023",
            "flood_rows": int(truth.sum()),
            "dry_district_days": int((truth == 0).sum()),
            "operating_points_share_of_dry_days": TARGETS,
            "model_cut": f"from out-of-fold training scores, {INNER_GROUPS} year groups",
            "bootstrap_resamples": BOOTSTRAP,
        },
        "results": compare(data),
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"{'method':<16}{'AUC':>7}{'CRIT POD':>10}{'POFD':>7}{'WATCH POD':>11}{'POFD':>7}   vs rule at CRITICAL")
    for name, r in report["results"].items():
        if not isinstance(r, dict) or "roc_auc" not in r:
            continue
        note = r["critical"].get("verdict", "")
        print(f"{name:<16}{r['roc_auc']:>7.3f}{r['critical']['POD']:>10.3f}{r['critical']['POFD']:>7.3f}"
              f"{r['watch']['POD']:>11.3f}{r['watch']['POFD']:>7.3f}   {note}")
    print(f"\nWritten to {OUT}")


if __name__ == "__main__":
    main()
