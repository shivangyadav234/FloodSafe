"""
Test whether rainfall can warn of landslides in Uttarakhand, and fit
the thresholds if it can.

Same method as the flood calibration (calibrate_thresholds.py), whose
functions this reuses, with two differences:

  Events: rain-triggered landslides from NASA's Global Landslide
    Catalog (fetch_landslide_catalog.py), each placed in its district.
    Landslides in one district within EXCLUSION_DAYS of each other are
    one episode, scored like a multi-day flood: caught if the rule fired
    on any of its days.

  Windows: 24 hours, 3 days and 7 days. Landslides follow soil
    saturation, which builds over days, more than the hour-scale bursts
    that drive flash floods. (The flood calibration already found that
    its 1h/3h/24h rule barely separated landslide-only events.)

Rain: the same hourly ERA5 cache as the flood calibration. It starts 27
May each year, so only days from 4 June on have a full 7-day history;
earlier days are left out.

The report says, for the same district-days:
  - how well each window separates landslide days from dry days
    (ROC AUC, in millimetres and against each place's own climate);
  - the any-window rule, cross-validated leave-one-year-out;
  - the operating curve: detection at several false-alarm rates, so the
    operating point can be chosen as it was for floods;
  - what the live flood rule (1h/3h/24h, balanced) already catches of
    the same landslides -- if it catches as many, a separate landslide
    layer adds nothing.

Writes floodsafe/models/landslide_rainfall_thresholds.json.
"""

import json
import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import calibrate_thresholds as C  # noqa: E402

LANDSLIDES = os.path.join(C.REPO, "data", "flood", "uttarakhand", "data", "landslides_uttarakhand.geojson")
OUT = os.path.join(C.REPO, "floodsafe", "models", "landslide_rainfall_thresholds.json")

WINDOWS = {"24h": 24, "72h": 72, "168h": 168}
# The live flood rule's windows, for the comparison.
FLOOD_WINDOWS = C.WINDOWS
FLOOD_LEVELS = {"critical": 0.10, "watch": 0.20}

# First day whose day-before-to-day span has 7 full days of rain before it.
FIRST_FULL_DAY = (6, 4)
CURVE_TARGETS = (0.05, 0.10, 0.15, 0.20, 0.30)


def load_landslide_events(path=LANDSLIDES):
    """Episodes: {id, start, end, kind, districts} in the flood inventory's shape."""
    with open(path, encoding="utf-8") as f:
        features = json.load(f)["features"]

    by_district = {}
    for feature in features:
        props = feature["properties"]
        by_district.setdefault(props["district"], []).append(date.fromisoformat(props["date"]))

    episodes = []
    for district, days in sorted(by_district.items()):
        days = sorted(set(days))
        start = end = days[0]
        for day in days[1:]:
            if (day - end).days <= C.EXCLUSION_DAYS:
                end = day
                continue
            episodes.append((district, start, end))
            start = end = day
        episodes.append((district, start, end))

    return [{
        "id": f"{district}-{start.isoformat()}",
        "start": start,
        # The same week cap the flood records get.
        "end": min(end, start + timedelta(days=6)),
        "kind": "landslide",
        "districts": [district],
    } for district, start, end in episodes]


def build_frame(rainfall):
    windows = {**FLOOD_WINDOWS, **WINDOWS}
    frame = C.district_days(rainfall, windows)
    frame = C.add_relative_predictors(frame, rainfall, windows)
    first = pd.to_datetime(frame["day"]).map(lambda d: (d.month, d.day) >= FIRST_FULL_DAY)
    return frame[first.to_numpy()].reset_index(drop=True), windows


def operating_curve(data, windows, truth):
    curve = []
    for target in CURVE_TARGETS:
        flags = C.fixed_false_alarm_rule(data, windows, target)
        curve.append(dict(C.scores(flags, truth), target_POFD=target,
                          thresholds_all_years=C.thresholds_at(data, windows, target)))
    return curve


def main():
    events = load_landslide_events()
    rainfall = C.load_district_rainfall()
    frame, windows = build_frame(rainfall)

    data = C.label(frame, events, positive_kinds=("landslide",), windows=windows)
    truth = data["label"].to_numpy()
    if truth.sum() < 10:
        sys.exit(f"Only {int(truth.sum())} landslide episodes fall in the rainfall record -- too few to calibrate.")

    in_record = [e for e in events if e["start"].year in C.YEARS]
    relative = [w + "_rel" for w in WINDOWS]

    report = {
        "method": __doc__.strip().split("\n\n")[0],
        "events": {
            "source": "NASA Global Landslide Catalog, rain-triggered, located to 25 km or better",
            "episodes": len(events),
            "episodes_in_rainfall_years": len(in_record),
            "episodes_in_season_from_4_june": sum(
                1 for e in in_record if C.in_season(e["start"]) and (e["start"].month, e["start"].day) >= FIRST_FULL_DAY),
            "positive_district_days": int(truth.sum()),
            "negative_district_days": int((truth == 0).sum()),
            "by_year": pd.Series([e["start"].year for e in in_record]).value_counts().sort_index().to_dict(),
        },
        "roc_auc_mm": {w: round(C.roc_auc(data[w].to_numpy(), truth), 3) for w in windows},
        "roc_auc_relative_to_local_climate": {
            w: round(C.roc_auc(data[w + "_rel"].to_numpy(), truth), 3) for w in windows},
    }

    flags, per_year = C.leave_one_year_out(data, list(WINDOWS))
    report["fitted_any_window_mm"] = C.scores(flags, truth)
    flags, _ = C.leave_one_year_out(data, relative)
    report["fitted_any_window_relative"] = C.scores(flags, truth)

    report["operating_curve_relative"] = operating_curve(data, relative, truth)
    report["operating_curve_mm"] = operating_curve(data, list(WINDOWS), truth)

    # What the live flood rule already catches of the same landslides.
    flood_relative = [w + "_rel" for w in FLOOD_WINDOWS]
    report["flood_rule_on_landslides"] = {
        level: C.scores(C.fixed_false_alarm_rule(data, flood_relative, target), truth)
        for level, target in FLOOD_LEVELS.items()}

    # The landslide rule at the same false-alarm rates. A separate layer
    # is only worth having if this catches clearly more than the above.
    report["landslide_rule_at_flood_levels"] = {
        level: C.scores(C.fixed_false_alarm_rule(data, relative, target), truth)
        for level, target in FLOOD_LEVELS.items()}

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    print(json.dumps(report["events"], indent=1, default=str))
    print("ROC AUC (mm):", report["roc_auc_mm"])
    print("ROC AUC (own climate):", report["roc_auc_relative_to_local_climate"])
    for level in FLOOD_LEVELS:
        a = report["landslide_rule_at_flood_levels"][level]
        b = report["flood_rule_on_landslides"][level]
        print(f"{level}: landslide rule catches {a['POD']:.0%} at POFD {a['POFD']:.0%}; "
              f"flood rule catches {b['POD']:.0%} at POFD {b['POFD']:.0%}")
    print("Wrote", OUT)


if __name__ == "__main__":
    main()
