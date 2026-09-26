"""
Test and calibrate the FFGS rainfall thresholds against real floods.

The WATCH/CRITICAL thresholds were set by hand. This measures how well
they -- and data-fitted alternatives -- separate the days Uttarakhand
districts actually flooded from the monsoon days they didn't.

Events: India Flood Inventory v3 (Saharia et al., Nat Hazards 2021;
IIT Delhi with IMD; Zenodo 10.5281/zenodo.4742142, CC BY-NC 4.0).
Uttarakhand rows are picked by LGD district code (45-56) and, for
Uttarkashi -- which the file carries without a code, under the name
"Uttar Kashi Kashi" -- by name. 2000-2023, 1 June - 30 September.

Rain: hourly ERA5 at every 0.25 deg grid cell inside each district
(fetch_era5_rainfall.py).

Unit of analysis: a district-day. For each, the predictor for a window
(1h, 3h, 24h) is the largest rainfall total over that window anywhere
in the district, during that day or the day before -- the inventory
dates events by day, sometimes by the day they were reported, so a
one-day allowance keeps real events from being scored as misses on a
technicality. Every district-day is treated the same way.

Labels:
  positive -- the start day of a rain-driven flood event in that
    district (heavy rain, cloudburst, flash flood).
  excluded -- the three days either side of any event (ambiguous
    timing), and events recorded as landslide-only or glacier-driven,
    which a rainfall threshold is not meant to catch. Those are tested
    separately as a sensitivity check.
  negative -- every other district-day of the monsoon.

Scores, all on the same district-days:
  POD   probability of detection: share of flood days flagged
  POFD  probability of false detection: share of dry days flagged
  FAR   false alarm ratio: share of flags that were not floods
  TSS   true skill statistic, POD - POFD (0 = no skill, 1 = perfect)

Fitted thresholds are validated leave-one-year-out: each year is scored
with thresholds fitted on the other 23, so the reported skill is what
the method achieves on years it has not seen.

Known limits, stated rather than hidden:
  - ERA5 is ~25 km and smooths convective cloudbursts, so its hourly
    peaks are far below gauge peaks. Thresholds fitted here are in ERA5
    units; the live system reads a different (finer) model, so they are
    bias-checked against it before use (see check_live_model_bias.py).
  - The inventory is compiled from reports, so small events in remote
    places are under-recorded, and some "negative" days were floods no
    one recorded. That depresses POD and inflates FAR for every method
    equally -- comparisons between methods remain fair.
"""

import json
import os
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
IFI = os.path.join(REPO, "floodsafe", "data", "events", "ifi", "India_Flood_Inventory_v3.csv")
ERA5_DIR = os.path.join(REPO, "floodsafe", "data", "rainfall", "era5")
ZONES = os.path.join(REPO, "floodsafe", "data", "tables", "ffgs_zones_snapshot.json")
OUT = os.path.join(REPO, "floodsafe", "models", "rainfall_thresholds.json")

YEARS = list(range(2000, 2024))
SEASON = ((6, 1), (9, 30))
WINDOWS = {"1h": 1, "3h": 3, "24h": 24}
EXCLUSION_DAYS = 3

LGD_DISTRICTS = {
    "45": "Almora", "46": "Bageshwar", "47": "Chamoli", "48": "Champawat",
    "49": "Dehradun", "50": "Haridwar", "51": "Nainital", "52": "Pauri Garhwal",
    "53": "Pithoragarh", "54": "Rudraprayag", "55": "Tehri Garhwal", "56": "Udham Singh Nagar",
}

RAIN_WORDS = ("rain", "cloud", "flash", "flood", "torrential")
NOT_RAIN_WORDS = ("glacier", "avalanche")


# ------------------------------------------------------------------
# Events
# ------------------------------------------------------------------

def classify_cause(cause):
    # Rain words decide first. The 2013 Kedarnath disaster's cause names
    # extreme rain and flash floods and adds that avalanches were
    # reported; checking glacier words first filed the worst flood on
    # record as non-rain and dropped it from calibration. Only a cause
    # with no rain in it at all -- "Nandadevi glacier breaks off", 2021
    # -- is glacier-driven.
    text = cause.lower() if isinstance(cause, str) else ""
    if any(word in text for word in RAIN_WORDS):
        return "rain"
    if any(word in text for word in NOT_RAIN_WORDS):
        return "glacier"
    if "landslide" in text:
        return "landslide"
    return "other"


def load_events():
    df = pd.read_csv(IFI, dtype=str, encoding_errors="replace")
    events = []

    for _, row in df.iterrows():
        names = [n.strip() for n in str(row["Districts"]).split(",")]
        codes = [c.strip() for c in str(row["District_LGD_Codes"]).split(",")]
        if len(codes) != len(names):
            codes = [""] * len(names)

        districts = set()
        for name, code in zip(names, codes):
            if code in LGD_DISTRICTS:
                districts.add(LGD_DISTRICTS[code])
            elif name.lower().startswith("uttar kashi kashi"):
                districts.add("Uttarkashi")

        if not districts:
            continue

        try:
            start = datetime.strptime(str(row["Start Date"]).strip(), "%d-%m-%Y %H:%M").date()
        except ValueError:
            continue
        try:
            end = datetime.strptime(str(row["End Date"]).strip(), "%d-%m-%Y %H:%M").date()
        except ValueError:
            end = start
        # A few records run for weeks (a whole monsoon's damage summed
        # up); the flood-producing rain is at the start, so cap at a week.
        end = min(max(end, start), start + timedelta(days=6))

        events.append({
            "id": row["UEI"],
            "start": start,
            "end": end,
            "cause": row["Main Cause"],
            "kind": classify_cause(row["Main Cause"]),
            "districts": sorted(districts),
            "deaths": row["Human fatality"],
        })

    return events


def in_season(day):
    return (day.month, day.day) >= SEASON[0] and (day.month, day.day) <= SEASON[1]


# ------------------------------------------------------------------
# Rainfall -> district-day predictors
# ------------------------------------------------------------------

def load_district_rainfall():
    """{district: {year: (times, array[points, hours])}}"""
    by_district = {}

    for name in os.listdir(ERA5_DIR):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(ERA5_DIR, name), encoding="utf-8") as f:
            record = json.load(f)
        values = np.array([np.nan if v is None else v for v in record["precipitation_mm"]], dtype=float)
        by_district.setdefault(record["district"], {}).setdefault(record["year"], []).append(
            (record["start"], values))

    out = {}
    for district, years in by_district.items():
        out[district] = {}
        # A district-year missing some of its grid points would read
        # drier than it was, so only complete ones are used -- the
        # download spans several days of API quota.
        full = max(len(series) for series in years.values())
        for year, series in years.items():
            if len(series) < full:
                continue
            start = series[0][0]
            length = min(len(v) for _, v in series)
            out[district][year] = (start, np.vstack([v[:length] for _, v in series]))
    return out


def rolling_max(values, hours):
    """Largest `hours`-long running total within each row, per hour index."""
    filled = np.nan_to_num(values)
    if hours == 1:
        return filled
    kernel = np.ones(hours)
    return np.array([np.convolve(row, kernel, mode="full")[:len(row)] for row in filled])


def district_days(rainfall, windows=WINDOWS):
    """One row per district-day in season, with the predictors."""
    rows = []

    for district, years in rainfall.items():
        for year, (start, grid) in years.items():
            t0 = datetime.fromisoformat(start)
            per_window = {w: rolling_max(grid, h).max(axis=0) for w, h in windows.items()}
            hours = grid.shape[1]

            day = date(year, *SEASON[0])
            last = date(year, *SEASON[1])
            while day <= last:
                # Hours covering the day before and the day itself.
                begin = int((datetime.combine(day - timedelta(days=1), datetime.min.time()) - t0).total_seconds() // 3600)
                end = begin + 48
                if begin < 0 or end > hours:
                    day += timedelta(days=1)
                    continue
                row = {"district": district, "day": day, "year": year}
                for window, series in per_window.items():
                    row[window] = float(series[begin:end].max())
                # Rain in the five days before that, averaged over the district.
                a_begin = max(0, begin - 5 * 24)
                row["antecedent_5d"] = float(np.nan_to_num(grid[:, a_begin:begin]).sum(axis=1).mean())
                rows.append(row)
                day += timedelta(days=1)

    return pd.DataFrame(rows)


def add_relative_predictors(frame, rainfall, windows=WINDOWS):
    """
    Add, per window, how rare the day's rain was *for that grid cell*:
    the fraction of that cell's own hourly window totals, in the other
    years, that it exceeded -- the largest across the district's cells.

    Some slopes get far more rain every monsoon than others, so a fixed
    millimetre threshold is routine in one place and extreme in the
    next. Measuring against each cell's own climate is how operational
    systems handle this (return-period thresholds). The climate for a
    day's year is built from the *other* years only, so a year is never
    scored against a distribution that includes itself.
    """
    frame = frame.copy()
    for window in windows:
        frame[window + "_rel"] = np.nan

    for district, years in rainfall.items():
        runs = {}
        for year, (start, grid) in years.items():
            runs[year] = (datetime.fromisoformat(start), {w: rolling_max(grid, h) for w, h in windows.items()})

        for year, (t0, arrays) in runs.items():
            others = [runs[y][1] for y in runs if y != year]
            if not others:
                continue
            climate = {w: np.sort(np.concatenate([o[w] for o in others], axis=1), axis=1) for w in windows}

            mask = (frame["district"] == district) & (frame["year"] == year)
            for index in frame.index[mask]:
                day = frame.at[index, "day"]
                begin = int((datetime.combine(day - timedelta(days=1), datetime.min.time()) - t0).total_seconds() // 3600)
                for window in windows:
                    peak = arrays[window][:, begin:begin + 48].max(axis=1)
                    clim = climate[window]
                    rarity = max(np.searchsorted(clim[i], peak[i], side="right") / clim.shape[1]
                                 for i in range(len(peak)))
                    frame.at[index, window + "_rel"] = rarity

    return frame


def label(frame, events, positive_kinds=("rain",), windows=WINDOWS):
    """
    One positive row per event-district, scored over the event's whole
    duration: caught if the rule would have fired on any of its days
    (Kedarnath 2013 began on the 14th; its heaviest rain fell on the
    16th and 17th). Days within EXCLUSION_DAYS of any event's span are
    left out as ambiguous. Everything else in season is a negative.
    """
    frame = frame.copy()
    frame["label"] = 0
    days = pd.to_datetime(frame["day"])
    # Antecedent rain stays the first day's: the max over the event would
    # count the event's own rain as "earlier" rain.
    predictors = [c for c in frame.columns if c in windows or c.endswith("_rel")]
    positives = []

    for event in events:
        if event["start"].year not in YEARS:
            continue
        start, end = pd.Timestamp(event["start"]), pd.Timestamp(event["end"])
        for district in event["districts"]:
            mask = frame["district"] == district
            near = mask & (days >= start - pd.Timedelta(days=EXCLUSION_DAYS))                         & (days <= end + pd.Timedelta(days=EXCLUSION_DAYS))
            if event["kind"] in positive_kinds and in_season(event["start"]):
                during = frame[mask & (days >= start) & (days <= end)]
                if len(during):
                    row = during.iloc[0].copy()
                    for column in predictors:
                        row[column] = during[column].max()
                    row["label"] = 1
                    positives.append(row)
            frame.loc[near, "label"] = -1

    negatives = frame[frame["label"] == 0]
    return pd.concat([negatives, pd.DataFrame(positives)], ignore_index=True) if positives else negatives


# ------------------------------------------------------------------
# Scoring
# ------------------------------------------------------------------

def scores(flag, truth):
    flag, truth = np.asarray(flag, bool), np.asarray(truth, bool)
    hits = int((flag & truth).sum())
    misses = int((~flag & truth).sum())
    false_alarms = int((flag & ~truth).sum())
    quiet = int((~flag & ~truth).sum())
    pod = hits / max(1, hits + misses)
    pofd = false_alarms / max(1, false_alarms + quiet)
    far = false_alarms / max(1, hits + false_alarms)
    return {"hits": hits, "misses": misses, "false_alarms": false_alarms, "correct_negatives": quiet,
            "POD": round(pod, 3), "POFD": round(pofd, 4), "FAR": round(far, 3), "TSS": round(pod - pofd, 3)}


def best_threshold(values, truth):
    candidates = np.unique(np.round(values[truth == 1], 5))
    best, best_tss = None, -1.0
    for t in candidates:
        flag = values >= t
        pod = flag[truth == 1].mean()
        pofd = flag[truth == 0].mean()
        if pod - pofd > best_tss:
            best, best_tss = float(t), pod - pofd
    return best


def roc_auc(values, truth):
    """Chance a random flood day out-ranks a random dry day (0.5 = no skill)."""
    from scipy.stats import rankdata

    ranks = rankdata(values)
    positives = truth == 1
    n_pos, n_neg = positives.sum(), (~positives).sum()
    return float((ranks[positives].sum() - n_pos * (n_pos + 1) / 2) / max(1, n_pos * n_neg))


def any_window_score(frame, windows):
    """
    One number per district-day for the any-window rule: how far over
    its own dry-day distribution the most extreme window was. Each
    window is ranked against the dry days it was fitted on, so a 99th
    percentile hour and a 99th percentile day count the same.
    """
    return lambda train, test: np.max([
        np.searchsorted(np.sort(train.loc[train["label"] == 0, w].to_numpy()), test[w].to_numpy(), side="right")
        / max(1, (train["label"] == 0).sum())
        for w in windows], axis=0)


def fixed_false_alarm_rule(frame, windows, pofd_target):
    """
    Alerting systems are tuned to a tolerable false-alarm rate, not to a
    skill score that rewards flagging most days when floods are rare.
    For each held-out year: on the other years, find the dry-day
    percentile at which the any-window rule flags `pofd_target` of dry
    days, and apply that to the held-out year.
    """
    flags = np.zeros(len(frame), bool)
    score = any_window_score(frame, windows)
    cut = 1.0 - pofd_target

    # Percentile p on each window taken separately flags more than
    # 1 - p of dry days once windows are combined, so p is tuned on the
    # training years until the combined rate matches the target.
    for year in sorted(frame["year"].unique()):
        train, test_mask = frame[frame["year"] != year], (frame["year"] == year).to_numpy()
        dry = train[train["label"] == 0]
        train_scores = score(train, dry)
        p = float(np.quantile(train_scores, cut))
        flags[test_mask] = score(train, frame[test_mask]) > p

    return flags


def thresholds_at(frame, windows, pofd_target):
    """The same rule fitted on all years, expressed in mm per window."""
    dry = frame[frame["label"] == 0]
    score = any_window_score(frame, windows)(frame, dry)
    p = float(np.quantile(score, 1.0 - pofd_target))
    # mm to 0.1; local-climate rarity (a fraction near 1) to 5 places.
    return {w: round(float(np.quantile(dry[w].to_numpy(), min(p, 1.0))), 5 if w.endswith("_rel") else 1)
            for w in windows}


def leave_one_year_out(frame, windows):
    """Fit the any-window rule on 23 years, flag the 24th, repeat."""
    flags = np.zeros(len(frame), bool)
    per_year = {}
    for year in sorted(frame["year"].unique()):
        train, test = frame[frame["year"] != year], frame["year"] == year
        thresholds = {w: best_threshold(train[w].to_numpy(), train["label"].to_numpy()) for w in windows}
        flags[test.to_numpy()] = np.any(
            [frame.loc[test, w].to_numpy() >= thresholds[w] for w in windows], axis=0)
        per_year[int(year)] = thresholds
    return flags, per_year


def current_system_flags(frame, level):
    """
    Would today's hand-set thresholds have flagged the district that day?
    Uses each zone's own class thresholds against the district's ERA5
    maxima -- generous to the current system, since it credits a zone
    with the district's wettest cell.
    """
    with open(ZONES, encoding="utf-8") as f:
        zones = json.load(f)["zones"]

    # The lowest threshold among a district's zones decides it.
    lowest = {}
    for zone in zones:
        for window in WINDOWS:
            limit = zone["thresholds_mm"][window][level]
            key = (zone["district"], window)
            lowest[key] = min(lowest.get(key, np.inf), limit)

    flags = np.zeros(len(frame), bool)
    for window in WINDOWS:
        limits = frame["district"].map(lambda d: lowest.get((d, window), np.inf)).to_numpy()
        flags |= frame[window].to_numpy() >= limits
    covered = frame["district"].map(lambda d: (d, "1h") in lowest).to_numpy()
    return flags, covered


def main():
    events = load_events()
    rainfall = load_district_rainfall()
    frame = add_relative_predictors(district_days(rainfall), rainfall)
    data = label(frame, events)
    truth = data["label"].to_numpy()

    in_scope = [e for e in events if e["start"].year in YEARS and in_season(e["start"])]
    report = {
        "method": __doc__.strip().split("\n\n")[0],
        "events": {
            "source": "India Flood Inventory v3 (Saharia et al. 2021), CC BY-NC 4.0",
            "uttarakhand_events_2000_2023_monsoon": len(in_scope),
            "by_kind": pd.Series([e["kind"] for e in in_scope]).value_counts().to_dict(),
            "positive_district_days": int(truth.sum()),
            "negative_district_days": int((truth == 0).sum()),
        },
        "rainfall": {"source": "ERA5 hourly via Open-Meteo archive", "district_years": int(sum(len(y) for y in rainfall.values()))},
    }

    # 1. The thresholds in use today.
    for level in ("watch", "critical"):
        flags, covered = current_system_flags(data, level)
        report[f"current_{level}"] = scores(flags, truth)
        report[f"current_{level}"]["floods_in_districts_without_zones"] = int(truth[~covered].sum())

    # 2. Each window alone, fitted and cross-validated.
    for window in WINDOWS:
        flags, per_year = leave_one_year_out(data, [window])
        report[f"fitted_{window}_only"] = dict(
            scores(flags, truth),
            threshold_all_years=best_threshold(data[window].to_numpy(), truth))

    # 3. The any-window rule, the shape the live system uses.
    flags, per_year = leave_one_year_out(data, list(WINDOWS))
    report["fitted_any_window"] = dict(
        scores(flags, truth),
        thresholds_all_years={w: best_threshold(data[w].to_numpy(), truth) for w in WINDOWS},
        threshold_spread_across_folds={
            w: [min(t[w] for t in per_year.values()), max(t[w] for t in per_year.values())] for w in WINDOWS})

    # 4. Discrimination, independent of any threshold.
    report["roc_auc"] = {w: round(roc_auc(data[w].to_numpy(), truth), 3) for w in WINDOWS}
    report["roc_auc"]["antecedent_5d"] = round(roc_auc(data["antecedent_5d"].to_numpy(), truth), 3)

    # 5. The operating points an alerting system would actually use:
    #    CRITICAL on ~5% of dry district-days, WATCH on ~15%.
    for level, target in (("critical", 0.05), ("watch", 0.15)):
        flags = fixed_false_alarm_rule(data, list(WINDOWS), target)
        report[f"calibrated_{level}"] = dict(
            scores(flags, truth),
            target_POFD=target,
            thresholds_all_years_mm=thresholds_at(data, list(WINDOWS), target))

    # 6. The same, measured against each cell's own climate.
    relative = [w + "_rel" for w in WINDOWS]
    report["roc_auc_relative_to_local_climate"] = {
        w: round(roc_auc(data[w + "_rel"].to_numpy(), truth), 3) for w in WINDOWS}
    for level, target in (("critical", 0.05), ("watch", 0.15)):
        flags = fixed_false_alarm_rule(data, relative, target)
        report[f"calibrated_relative_{level}"] = dict(
            scores(flags, truth),
            target_POFD=target,
            local_climate_quantile_all_years=thresholds_at(data, relative, target))
    flags, _ = leave_one_year_out(data, relative)
    report["fitted_relative_any_window"] = scores(flags, truth)

    # 7. Sensitivity: count landslide-only events as positives too.
    wide = label(frame, events, positive_kinds=("rain", "landslide"))
    flags, _ = leave_one_year_out(wide, list(WINDOWS))
    report["sensitivity_including_landslides"] = scores(flags, wide["label"].to_numpy())

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
