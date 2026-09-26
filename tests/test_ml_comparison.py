"""
The ML comparison (floodsafe/pipeline/compare_ml_model.py) on synthetic
district-days where the answer is known: it must find no gain when the
rule already has all the signal, a real gain when a feature the rule
ignores carries signal, and hold the rule's false-alarm levels.
"""

import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("xgboost")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "floodsafe", "pipeline"))
import compare_ml_model as M  # noqa: E402


def synthetic(antecedent_signal, seed=1):
    rng = np.random.default_rng(seed)
    rows = []
    for year in range(2000, 2010):
        for district in ("A", "B", "C"):
            day = date(year, 6, 1)
            while day <= date(year, 9, 30):
                rain = rng.normal()
                antecedent = rng.gamma(2, 10)
                logit = -5.5 + 1.6 * rain + antecedent_signal * (antecedent - 20) / 10
                flood = int(rng.random() < 1 / (1 + np.exp(-logit)))
                mm = max(0.0, 3 + 2 * rain + rng.normal())
                rarity = lambda noise: 1 / (1 + np.exp(-(rain + noise * rng.normal())))
                rows.append({"district": district, "day": day, "year": year,
                             "1h": mm, "3h": mm * 2.2, "24h": mm * 6, "antecedent_5d": antecedent,
                             "1h_rel": rarity(0.3), "3h_rel": rarity(0.3), "24h_rel": rarity(0.5),
                             "label": flood})
                day += timedelta(days=1)
    return pd.DataFrame(rows)


def run(data):
    models = {name: M.MODELS[name] for name in ("xgb_all",)}
    return M.compare(data, rounds=40, models=models)


def test_no_gain_is_claimed_when_the_rule_has_all_the_signal():
    result = run(synthetic(antecedent_signal=0.0))
    low, high = result["xgb_all"]["critical"]["POD_minus_rule_95ci"]
    assert low <= 0 <= high
    assert "sampling noise" in result["xgb_all"]["critical"]["verdict"]


def test_a_real_gain_is_found_and_the_false_alarm_levels_hold():
    result = run(synthetic(antecedent_signal=1.5))
    critical = result["xgb_all"]["critical"]
    assert critical["POD_minus_rule_95ci"][0] > 0
    assert result["xgb_all"]["roc_auc"] > result["rule"]["roc_auc"]
    for level, target in M.TARGETS.items():
        assert abs(result["xgb_all"][level]["POFD"] - target) < 0.03
        assert abs(result["rule"][level]["POFD"] - target) < 0.03
    assert next(iter(result["xgb_all_feature_importance_share"])) == "antecedent_5d"
