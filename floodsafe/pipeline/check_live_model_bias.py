"""
Translate ERA5-fitted thresholds into the units the live system reads.

calibrate_thresholds.py fits thresholds on ERA5 (~25 km reanalysis),
because it is the only record long enough to hold two decades of
floods. The live FFGS reads Open-Meteo's forecast feed ("best_match",
finer operational models), which resolves convective peaks that ERA5
smooths away. A threshold fitted in one and applied to the other would
be systematically off.

So both are fetched for the same points and the same monsoon days --
2022 and 2023, where Open-Meteo archives the operational forecasts --
and each window's district-day maxima are quantile-matched: an ERA5
threshold at the p-th percentile of ERA5 maxima maps to the p-th
percentile of live-model maxima. That keeps a threshold's meaning ("as
rare as this") while changing its units.

A sample of 20 of the 81 grid points, spread across all 13 districts,
keeps this within the free API quota; each point-year costs ~9 calls.
"""

import json
import os
import random
import time

import numpy as np
import requests

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ERA5_DIR = os.path.join(REPO, "floodsafe", "data", "rainfall", "era5")
LIVE_DIR = os.path.join(REPO, "floodsafe", "data", "rainfall", "live_model")
OUT = os.path.join(REPO, "floodsafe", "models", "live_model_bias.json")

API = "https://historical-forecast-api.open-meteo.com/v1/forecast"
YEARS = (2022, 2023)
SAMPLE = 20
WINDOWS = {"1h": 1, "3h": 3, "24h": 24}


def sample_points():
    points = {}
    for name in os.listdir(ERA5_DIR):
        if name.endswith(".json") and name.endswith("_2022.json"):
            with open(os.path.join(ERA5_DIR, name), encoding="utf-8") as f:
                record = json.load(f)
            points[(record["lat"], record["lon"])] = record["district"]

    by_district = {}
    for (lat, lon), district in sorted(points.items()):
        by_district.setdefault(district, []).append((lat, lon))

    # At least one point per district, the rest drawn at random.
    random.seed(20)
    chosen = [random.choice(pts) for pts in by_district.values()]
    rest = [p for p in points if p not in chosen]
    chosen += random.sample(rest, max(0, SAMPLE - len(chosen)))
    return [(lat, lon, points[(lat, lon)]) for lat, lon in chosen]


def fetch_live(lat, lon, year):
    path = os.path.join(LIVE_DIR, f"{lat:.2f}_{lon:.2f}_{year}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    response = requests.get(API, params={
        "latitude": lat, "longitude": lon,
        "start_date": f"{year}-05-27", "end_date": f"{year}-09-30",
        "hourly": "precipitation", "timezone": "Asia/Kolkata",
    }, timeout=60)
    response.raise_for_status()
    data = response.json()
    record = {"lat": lat, "lon": lon, "year": year, "start": data["hourly"]["time"][0],
              "precipitation_mm": data["hourly"]["precipitation"]}
    os.makedirs(LIVE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f)
    time.sleep(7)
    return record


def daily_window_maxima(values, hours):
    arr = np.nan_to_num(np.array([np.nan if v is None else v for v in values], dtype=float))
    run = np.convolve(arr, np.ones(hours), mode="full")[:len(arr)] if hours > 1 else arr
    days = len(run) // 24
    return run[:days * 24].reshape(days, 24).max(axis=1)


def main():
    pairs = {w: ([], []) for w in WINDOWS}

    for lat, lon, district in sample_points():
        for year in YEARS:
            era5_path = os.path.join(ERA5_DIR, f"{lat:.2f}_{lon:.2f}_{year}.json")
            if not os.path.exists(era5_path):
                continue
            with open(era5_path, encoding="utf-8") as f:
                era5 = json.load(f)
            live = fetch_live(lat, lon, year)
            for window, hours in WINDOWS.items():
                a = daily_window_maxima(era5["precipitation_mm"], hours)
                b = daily_window_maxima(live["precipitation_mm"], hours)
                n = min(len(a), len(b))
                pairs[window][0].extend(a[:n])
                pairs[window][1].extend(b[:n])

    quantiles = np.round(np.arange(0.50, 0.9991, 0.001), 3)
    report = {"method": __doc__.strip().split("\n\n")[0], "years": YEARS, "points": SAMPLE, "windows": {}}

    for window, (era5, live) in pairs.items():
        era5, live = np.array(era5), np.array(live)
        report["windows"][window] = {
            "days": int(len(era5)),
            # Pearson r from sums: np.corrcoef goes through BLAS, which
            # crashes this environment's NumPy build (0xc06d007f).
            "correlation": round(float(
                ((era5 - era5.mean()) * (live - live.mean())).sum()
                / np.sqrt(((era5 - era5.mean()) ** 2).sum() * ((live - live.mean()) ** 2).sum())), 3),
            "era5_quantiles": np.quantile(era5, quantiles).round(2).tolist(),
            "live_quantiles": np.quantile(live, quantiles).round(2).tolist(),
            "quantile_levels": quantiles.tolist(),
            "p99_ratio_live_over_era5": round(float(np.quantile(live, 0.99) / max(1e-6, np.quantile(era5, 0.99))), 2),
        }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1)
    for window, info in report["windows"].items():
        print(window, {k: info[k] for k in ("days", "correlation", "p99_ratio_live_over_era5")})


if __name__ == "__main__":
    main()
