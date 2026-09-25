"""
Download hourly ERA5 rainfall for calibrating the FFGS thresholds.

The thresholds that turn live rainfall into WATCH/CRITICAL were set by
hand. Calibrating them needs the rain that actually fell before real
flood events, and on all the monsoon days that passed without one, at
the same places. This fetches that record:

  - Points: every ERA5 grid-cell centre (0.25 deg) inside each of
    Uttarakhand's 13 districts -- 81 points, 2 to 13 per district --
    from the OpenStreetMap district polygons the app already uses.
  - Period: the monsoon, 1 June - 30 September, 2000-2023 (the years the
    India Flood Inventory covers), starting 27 May so June has its
    antecedent rainfall.
  - Source: Open-Meteo's historical archive, ERA5 model, hourly
    precipitation, in IST so days line up with the inventory's dates.

The full set is ~17,600 Open-Meteo calls against a free limit of 10,000
per day, so the download is resumable: every point-year is cached as its
own file, requests are paced under the per-minute and per-hour limits,
and the run stops cleanly before the daily one. Run it again the next
day to finish. It never works around the limits.

Usage:
    python fetch_era5_rainfall.py            # fetch what's missing, stop near the daily cap
    python fetch_era5_rainfall.py --status   # how much is cached
"""

import argparse
import json
import os
import sys
import time

import numpy as np
import requests
from shapely.geometry import Point, shape

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BOUNDARY = os.path.join(REPO, "data", "flood", "uttarakhand", "data", "uttarakhand_boundary.geojson")
OUT_DIR = os.path.join(REPO, "floodsafe", "data", "rainfall", "era5")

YEARS = range(2000, 2024)
SEASON_START, SEASON_END = "05-27", "09-30"
GRID = 0.25

API = "https://archive-api.open-meteo.com/v1/archive"
# Open-Meteo bills a location by the fortnight: >14 days counts as
# several calls. 127 days = 9.07 calls per point-year.
CALLS_PER_REQUEST = 127 / 14
DAILY_BUDGET = 9500          # stop short of the 10,000/day limit
SECONDS_BETWEEN_REQUESTS = 7.0   # ~4,700 calls/hour, under the 5,000 limit


def district_points():
    with open(BOUNDARY, encoding="utf-8") as f:
        features = json.load(f)["features"]

    districts = [
        (feat["properties"]["name"].replace(" district", "").strip(), shape(feat["geometry"]))
        for feat in features
        if str(feat["properties"].get("admin_level")) == "5"
        and feat["properties"].get("name")
        and feat["geometry"]["type"] in ("Polygon", "MultiPolygon")
    ]

    points = []
    for lat in np.arange(28.75, 31.51, GRID):
        for lon in np.arange(77.5, 81.26, GRID):
            for name, geometry in districts:
                if geometry.contains(Point(lon, lat)):
                    points.append({"district": name, "lat": round(float(lat), 2), "lon": round(float(lon), 2)})
    return points


def cache_path(point, year):
    return os.path.join(OUT_DIR, f"{point['lat']:.2f}_{point['lon']:.2f}_{year}.json")


# One kept-alive connection instead of a fresh TLS handshake per request.
SESSION = requests.Session()


def fetch(point, year):
    response = SESSION.get(API, params={
        "latitude": point["lat"],
        "longitude": point["lon"],
        "start_date": f"{year}-{SEASON_START}",
        "end_date": f"{year}-{SEASON_END}",
        "hourly": "precipitation",
        "models": "era5",
        "timezone": "Asia/Kolkata",
    }, timeout=60)

    if response.status_code == 429:
        raise RuntimeError("rate limited: " + response.text[:200])
    response.raise_for_status()
    data = response.json()

    return {
        "district": point["district"],
        "lat": point["lat"],
        "lon": point["lon"],
        "grid_lat": data.get("latitude"),
        "grid_lon": data.get("longitude"),
        "year": year,
        "model": "era5",
        "timezone": "Asia/Kolkata",
        "start": data["hourly"]["time"][0],
        "precipitation_mm": data["hourly"]["precipitation"],
    }


def calls_spent_today():
    """
    Calls already made today, from the files this and the bias check
    saved today, so a restarted run can't overspend the daily quota by
    starting its count from zero.
    """
    today = time.strftime("%Y-%m-%d")
    spent = 0.0
    for folder in (OUT_DIR, os.path.join(os.path.dirname(OUT_DIR), "live_model")):
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if name.endswith(".json") and time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(path))) == today:
                spent += CALLS_PER_REQUEST
    return spent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    points = district_points()
    # Newest years first: they hold most of the recorded events, and the
    # live-model bias check needs 2022-2023.
    todo = [(p, y) for y in sorted(YEARS, reverse=True) for p in points
            if not os.path.exists(cache_path(p, y))]
    total = len(points) * len(YEARS)

    print(f"{len(points)} points x {len(YEARS)} years = {total} point-years; "
          f"{total - len(todo)} cached, {len(todo)} to fetch", flush=True)
    if args.status or not todo:
        return 0

    spent = calls_spent_today()
    print(f"~{spent:.0f} calls already used today", flush=True)
    for i, (point, year) in enumerate(todo, 1):
        if spent + CALLS_PER_REQUEST > DAILY_BUDGET:
            print(f"Stopping at the daily budget ({spent:.0f} calls). Run again tomorrow; "
                  f"{len(todo) - i + 1} point-years remain.", flush=True)
            return 0
        record = None
        # Most drops are single connection resets that succeed on the very
        # next try, so retry fast first; some last minutes, so keep backing
        # off to ~30 minutes in total before giving up.
        for wait in (10, 60, 120, 240, 480, 900, None):
            try:
                record = fetch(point, year)
                break
            except requests.exceptions.RequestException as e:
                # Transient network trouble (a dropped connection, or a
                # certificate check failing while something on the local
                # network interferes) -- wait and retry, verification on.
                cause = repr(getattr(e, "__context__", None) or e)
                if wait is None:
                    print(f"  network error, giving up: {cause[:300]}", flush=True)
                    break
                print(f"  network error, retrying in {wait} s: {cause[:300]}", flush=True)
                time.sleep(wait)
            except Exception as e:
                print(f"Stopped: {e}. {len(todo) - i + 1} point-years remain; run again later.", flush=True)
                return 1
        if record is None:
            print(f"Stopped after retries. {len(todo) - i + 1} point-years remain; run again later.", flush=True)
            return 1

        tmp = cache_path(point, year) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.replace(tmp, cache_path(point, year))

        spent += CALLS_PER_REQUEST
        if i % 50 == 0:
            print(f"  {i}/{len(todo)} fetched, ~{spent:.0f} calls used this run", flush=True)
        time.sleep(SECONDS_BETWEEN_REQUESTS)

    print("All point-years cached.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
