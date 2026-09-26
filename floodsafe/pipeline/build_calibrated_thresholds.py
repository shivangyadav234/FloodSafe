"""
Turn the calibration into the thresholds the live FFGS applies.

calibrate_thresholds.py tested rainfall rules against 24 monsoons of
IMD-recorded floods (India Flood Inventory, 2000-2023) and found that
rain measured against each place's own climate -- how rare it is *there*
-- separates flood days from ordinary monsoon days better than fixed
millimetres, and better than the old hand-set, hazard-class thresholds
at every false-alarm rate.

The operating point was chosen by the project owner: "balanced" --
CRITICAL fires on about 10% of dry monsoon district-days, WATCH on about
20%. Validated leave-one-year-out, that catches 43% (CRITICAL) and 57%
(WATCH) of recorded floods.

This builds, for each of the 81 ERA5 grid points:

  1. the rarity levels those targets correspond to, fitted on all 24
     years (per window: 1h, 3h, 24h);
  2. that point's own rainfall at those rarity levels, in ERA5 mm;
  3. the same thresholds in the units of the live forecast feed, via the
     quantile mapping in live_model_bias.json (ERA5 smooths short bursts
     and the live model does not).

and writes data/flood/uttarakhand/data/calibrated_thresholds.json, which
the server loads; each FFGS zone takes the thresholds of its nearest
grid point.
"""

import json
import os
import sys
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import calibrate_thresholds as C  # noqa: E402

REPO = C.REPO
BIAS = os.path.join(REPO, "floodsafe", "models", "live_model_bias.json")
CURVE = os.path.join(REPO, "floodsafe", "models", "rainfall_operating_curve.json")
OUT = os.path.join(REPO, "data", "flood", "uttarakhand", "data", "calibrated_thresholds.json")

LEVELS = {"critical": 0.10, "watch": 0.20}


def to_live_units(era5_mm, window, bias):
    """Map an ERA5 amount to the live feed's amount of the same rarity."""
    info = bias["windows"][window]
    era5_q = np.array(info["era5_quantiles"])
    live_q = np.array(info["live_quantiles"])
    levels = np.array(info["quantile_levels"])

    if era5_mm >= era5_q[-1]:
        # Beyond the rarest matched quantile: keep the tail's ratio.
        return float(era5_mm * live_q[-1] / era5_q[-1])
    level = np.interp(era5_mm, era5_q, levels)
    return float(np.interp(level, levels, live_q))


def point_thresholds(rarity, windows, levels, bias):
    """
    Each grid point's own rainfall at the fitted rarity levels, in the
    live feed's units: [{lat, lon, district, thresholds_mm: {window:
    {level: mm}}}]. Shared with build_landslide_thresholds.py.
    """
    points = []

    # Each point's full record: hourly running totals across all 24 years.
    by_point = {}
    for name in os.listdir(C.ERA5_DIR):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(C.ERA5_DIR, name), encoding="utf-8") as f:
            record = json.load(f)
        values = np.array([np.nan if v is None else v for v in record["precipitation_mm"]], dtype=float)
        key = (record["lat"], record["lon"])
        by_point.setdefault(key, {"district": record["district"], "series": []})["series"].append(values)

    for (lat, lon), info in sorted(by_point.items()):
        thresholds = {}
        for window, hours in windows.items():
            running = np.concatenate([C.rolling_max(v[None, :], hours)[0] for v in info["series"]])
            thresholds[window] = {}
            for level in levels:
                era5_mm = float(np.quantile(running, rarity[level][window + "_rel"]))
                thresholds[window][level] = round(to_live_units(era5_mm, window, bias), 1)
        points.append({"lat": lat, "lon": lon, "district": info["district"], "thresholds_mm": thresholds})

    # WATCH must sit below CRITICAL everywhere, or the levels would invert.
    for point in points:
        for window, pair in point["thresholds_mm"].items():
            assert pair["watch"] < pair["critical"], (point, window)

    return points


def main():
    events = C.load_events()
    rainfall = C.load_district_rainfall()
    data = C.label(C.add_relative_predictors(C.district_days(rainfall), rainfall), events)
    truth = data["label"].to_numpy()
    relative = [w + "_rel" for w in C.WINDOWS]

    with open(BIAS, encoding="utf-8") as f:
        bias = json.load(f)

    rarity = {level: C.thresholds_at(data, relative, target) for level, target in LEVELS.items()}
    validation = {level: C.scores(C.fixed_false_alarm_rule(data, relative, target), truth)
                  for level, target in LEVELS.items()}

    points = point_thresholds(rarity, C.WINDOWS, LEVELS, bias)

    out = {
        "method": (
            "Rainfall rarity against each place's own climate, calibrated on 24 monsoons "
            "(2000-2023) of IMD-recorded floods from the India Flood Inventory and hourly ERA5 "
            "rainfall, then mapped to the live forecast feed's units."
        ),
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "operating_point": {"critical_target_share_of_dry_days": LEVELS["critical"],
                            "watch_target_share_of_dry_days": LEVELS["watch"]},
        "validation": {
            "scheme": "leave-one-year-out; district-days; June-September 2000-2023",
            "flood_events_scored": int(truth.sum()),
            "dry_district_days": int((truth == 0).sum()),
            **{level: {k: validation[level][k] for k in ("POD", "POFD", "TSS", "hits", "misses")}
               for level in LEVELS},
        },
        "rarity_levels": rarity,
        "sources": {
            "events": "India Flood Inventory v3 (Saharia et al. 2021, Nat Hazards; IIT Delhi with IMD), "
                      "doi:10.5281/zenodo.4742142, CC BY-NC 4.0",
            "rainfall": "ERA5 hourly precipitation via Open-Meteo historical archive",
            "live_units": "Open-Meteo historical forecast (best_match), 2022-2023, quantile-mapped",
        },
        "limits": [
            "ERA5 is ~25 km and smooths local cloudbursts; flood records are dated by day and located by district.",
            "Recorded floods are under-reported in remote areas, so true detection rates are uncertain.",
            "Landslide-only events are not predictable from district rainfall this way and were excluded.",
            "The flood inventory is licensed for non-commercial use only.",
        ],
        "points": points,
    }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)

    print(f"Wrote {len(points)} points to {OUT}")
    print("validation:", json.dumps(out["validation"]))
    sample = [p for p in points if p["district"] in ("Dehradun", "Chamoli", "Haridwar")][:3]
    for p in sample:
        print(p["district"], p["lat"], p["lon"], p["thresholds_mm"])


if __name__ == "__main__":
    main()
