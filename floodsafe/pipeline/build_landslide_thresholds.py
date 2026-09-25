"""
Turn the landslide calibration into thresholds the live system can apply.

calibrate_landslide_thresholds.py measures how well 1-, 3- and 7-day
rainfall, against each place's own climate, separates the days
Uttarakhand districts had rain-triggered landslides from dry monsoon
days. This fits the any-window rule at a chosen operating point and,
like build_calibrated_thresholds.py for floods, writes each of the 81
ERA5 grid points' own rainfall at those rarity levels in the live feed's
units (via the 72h and 168h mappings in live_model_bias.json -- run
check_live_model_bias.py first).

It refuses to write anything if the landslide rule doesn't catch more
landslides than the flood rule already does at the same false-alarm
rate: then a separate layer would add warnings without adding warning.
--force writes it anyway.

Usage:
    python build_landslide_thresholds.py                        # balanced, as for floods
    python build_landslide_thresholds.py --critical 0.05 --watch 0.15
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_calibrated_thresholds as B  # noqa: E402
import calibrate_landslide_thresholds as L  # noqa: E402
import calibrate_thresholds as C  # noqa: E402

OUT = os.path.join(C.REPO, "data", "flood", "uttarakhand", "data", "landslide_thresholds.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--critical", type=float, default=0.10, help="share of dry district-days CRITICAL may flag")
    parser.add_argument("--watch", type=float, default=0.20, help="share of dry district-days WATCH may flag")
    parser.add_argument("--force", action="store_true", help="write even if it adds nothing over the flood rule")
    args = parser.parse_args()
    if not 0 < args.critical < args.watch < 1:
        sys.exit("Need 0 < --critical < --watch < 1.")
    levels = {"critical": args.critical, "watch": args.watch}

    with open(B.BIAS, encoding="utf-8") as f:
        bias = json.load(f)
    missing = [w for w in L.WINDOWS if w not in bias["windows"]]
    if missing:
        sys.exit(f"live_model_bias.json has no {missing} mapping. Run check_live_model_bias.py first.")

    events = L.load_landslide_events()
    rainfall = C.load_district_rainfall()
    frame, windows = L.build_frame(rainfall)
    data = C.label(frame, events, positive_kinds=("landslide",), windows=windows)
    truth = data["label"].to_numpy()

    relative = [w + "_rel" for w in L.WINDOWS]
    flood_relative = [w + "_rel" for w in L.FLOOD_WINDOWS]

    rarity = {level: C.thresholds_at(data, relative, target) for level, target in levels.items()}
    validation = {level: C.scores(C.fixed_false_alarm_rule(data, relative, target), truth)
                  for level, target in levels.items()}
    flood_rule = {level: C.scores(C.fixed_false_alarm_rule(data, flood_relative, target), truth)
                  for level, target in levels.items()}

    for level in levels:
        print(f"{level}: landslide rule catches {validation[level]['POD']:.0%} of landslide episodes, "
              f"the flood rule {flood_rule[level]['POD']:.0%}, both at {levels[level]:.0%} of dry days")

    adds_warning = validation["critical"]["TSS"] > flood_rule["critical"]["TSS"]
    if not adds_warning and not args.force:
        sys.exit("The landslide rule doesn't beat the flood rule at CRITICAL, so a separate "
                 "layer would add nothing. Nothing written (use --force to write anyway).")

    points = B.point_thresholds(rarity, L.WINDOWS, levels, bias)

    out = {
        "method": (
            "Rainfall over 1, 3 and 7 days against each place's own climate, calibrated on "
            "rain-triggered landslides from NASA's Global Landslide Catalog and hourly ERA5 "
            "rainfall (June-September), then mapped to the live forecast feed's units."
        ),
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "operating_point": {"critical_target_share_of_dry_days": levels["critical"],
                            "watch_target_share_of_dry_days": levels["watch"]},
        "validation": {
            "scheme": "leave-one-year-out; district-days; 4 June - 30 September",
            "landslide_episodes_scored": int(truth.sum()),
            "dry_district_days": int((truth == 0).sum()),
            **{level: {k: validation[level][k] for k in ("POD", "POFD", "TSS", "hits", "misses")}
               for level in levels},
            "flood_rule_on_the_same_landslides": {
                level: {k: flood_rule[level][k] for k in ("POD", "POFD", "TSS")} for level in levels},
            "adds_warning_over_flood_rule": bool(adds_warning),
        },
        "rarity_levels": rarity,
        "sources": {
            "events": "NASA Global Landslide Catalog (Kirschbaum et al. 2010, 2015), NASA Goddard",
            "rainfall": "ERA5 hourly precipitation via Open-Meteo historical archive",
            "live_units": "Open-Meteo historical forecast (best_match), 2022-2023, quantile-mapped",
        },
        "limits": [
            "Landslides are dated by report and located to 25 km or better; the catalogue is compiled "
            "from news and misses many remote slides.",
            "ERA5 is ~25 km; rain on one slope can differ widely from the grid cell's.",
            "Rainfall is only one cause: slope, geology, road cutting and earthquakes are not in this rule.",
        ],
        "points": points,
    }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print(f"Wrote {len(points)} points to {OUT}")


if __name__ == "__main__":
    main()
