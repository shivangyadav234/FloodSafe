"""
Pick Uttarakhand's rain-triggered landslides out of NASA's Global
Landslide Catalog, for calibrating landslide rainfall thresholds
(calibrate_landslide_thresholds.py).

Source: NASA Global Landslide Catalog (Kirschbaum et al. 2010, 2015),
compiled by NASA Goddard from news, official and scientific reports;
each event has a date, a location with a stated accuracy, and a trigger.
It is published on NASA's open data portal as "Global Landslide Catalog
Export". The portal has moved files before, so if the download below
fails, download the CSV by hand from https://data.nasa.gov (search
"Global Landslide Catalog") and pass it with --csv.

Kept:
  - inside one of Uttarakhand's 13 district polygons (the OpenStreetMap
    boundaries the app already uses), which also assigns the district;
  - located to 25 km or better -- coarser than a district is too coarse
    to say which district's rain caused it;
  - triggered by rain (rain, downpour, monsoon, continuous rain, storms,
    flooding); earthquakes, construction, snowmelt, mining and unknown
    triggers are dropped, since a rainfall threshold can't catch them.

Written to data/flood/uttarakhand/data/landslides_uttarakhand.geojson,
which is committed: it is small, and the app can show past landslides
from it.

Usage:
    python fetch_landslide_catalog.py                # download, then filter
    python fetch_landslide_catalog.py --csv glc.csv  # filter a file you downloaded
"""

import argparse
import io
import json
import os
import sys

import pandas as pd
import requests
from shapely.geometry import Point, shape

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BOUNDARY = os.path.join(REPO, "data", "flood", "uttarakhand", "data", "uttarakhand_boundary.geojson")
OUT = os.path.join(REPO, "data", "flood", "uttarakhand", "data", "landslides_uttarakhand.geojson")
RAW = os.path.join(REPO, "floodsafe", "data", "events", "glc", "global_landslide_catalog.csv")

URLS = [
    "https://data.nasa.gov/api/views/dd9e-wu2v/rows.csv?accessType=DOWNLOAD",
]

# The catalogue has been exported with two sets of column names: the
# current one (event_date, landslide_trigger, ...) and the 2007-2015
# release (date, trigger, ...). Each field lists the names it has had.
COLUMNS = {
    "id": ("event_id", "id"),
    "date": ("event_date", "date"),
    "lat": ("latitude",),
    "lon": ("longitude",),
    "accuracy": ("location_accuracy",),
    "trigger": ("landslide_trigger", "trigger"),
    "category": ("landslide_category", "landslide_type"),
    "size": ("landslide_size",),
    "fatalities": ("fatality_count", "fatalities"),
    "title": ("event_title", "location_description", "nearest_places"),
    "source": ("source_name",),
}
REQUIRED = ("date", "lat", "lon", "accuracy", "trigger")

RAIN_TRIGGERS = {
    "rain", "downpour", "monsoon", "continuous_rain", "continuous rain",
    "tropical_cyclone", "tropical cyclone", "tropical_storm", "flooding", "flood",
}

# Stated accuracies; anything coarser, or unknown, is dropped.
MAX_ACCURACY_KM = 25


def accuracy_km(value):
    text = str(value).strip().lower()
    if text in ("exact", "0km"):
        return 0.0
    if text.endswith("km"):
        try:
            return float(text[:-2])
        except ValueError:
            return None
    return None


def district_polygons():
    with open(BOUNDARY, encoding="utf-8") as f:
        features = json.load(f)["features"]
    return [
        (feat["properties"]["name"].replace(" district", "").strip(), shape(feat["geometry"]))
        for feat in features
        if str(feat["properties"].get("admin_level")) == "5"
        and feat["properties"].get("name")
        and feat["geometry"]["type"] in ("Polygon", "MultiPolygon")
    ]


def download():
    if os.path.exists(RAW):
        print("Using cached", RAW)
        return RAW

    for url in URLS:
        try:
            response = requests.get(url, timeout=120)
            response.raise_for_status()
            pd.read_csv(io.BytesIO(response.content), nrows=5)
        except Exception as e:
            print(f"Download failed from {url}: {e}", file=sys.stderr)
            continue
        os.makedirs(os.path.dirname(RAW), exist_ok=True)
        with open(RAW, "wb") as f:
            f.write(response.content)
        return RAW

    sys.exit("Couldn't download the catalogue. Download it from https://data.nasa.gov "
             "(search 'Global Landslide Catalog') and run again with --csv <file>.")


def normalise(raw):
    """The catalogue's columns under this script's names, whichever release it is."""
    lower = {c.lower().strip(): c for c in raw.columns}
    frame = pd.DataFrame(index=raw.index)
    for field, names in COLUMNS.items():
        found = next((lower[n] for n in names if n in lower), None)
        frame[field] = raw[found] if found else None

    missing = [f for f in REQUIRED if frame[f].isna().all()]
    if missing:
        sys.exit(f"Catalogue is missing {missing}. Columns found: {list(raw.columns)}")
    return frame


def select(frame, districts):
    frame = frame.copy()
    frame["day"] = pd.to_datetime(frame["date"], errors="coerce", format="mixed").dt.date
    frame["lat"] = pd.to_numeric(frame["lat"], errors="coerce")
    frame["lon"] = pd.to_numeric(frame["lon"], errors="coerce")
    frame = frame.dropna(subset=["day", "lat", "lon"])

    # Cheap box first; the catalogue has ~11,000 events worldwide.
    frame = frame[frame["lat"].between(28.6, 31.6) & frame["lon"].between(77.4, 81.2)]

    counts = {"in_box": len(frame)}
    kept = []
    for _, row in frame.iterrows():
        district = next((name for name, poly in districts if poly.contains(Point(row["lon"], row["lat"]))), None)
        if district is None:
            continue
        counts["in_uttarakhand"] = counts.get("in_uttarakhand", 0) + 1

        km = accuracy_km(row["accuracy"])
        if km is None or km > MAX_ACCURACY_KM:
            counts["dropped_location_too_coarse"] = counts.get("dropped_location_too_coarse", 0) + 1
            continue
        trigger = str(row["trigger"]).strip().lower()
        if trigger not in RAIN_TRIGGERS:
            key = "dropped_trigger_" + (trigger or "blank")
            counts[key] = counts.get(key, 0) + 1
            continue

        kept.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(float(row["lon"]), 5), round(float(row["lat"]), 5)]},
            "properties": {
                "id": None if pd.isna(row["id"]) else str(row["id"]),
                "date": row["day"].isoformat(),
                "district": district,
                "trigger": trigger,
                "category": None if pd.isna(row["category"]) else str(row["category"]),
                "size": None if pd.isna(row["size"]) else str(row["size"]),
                "location_accuracy_km": km,
                "fatalities": None if pd.isna(pd.to_numeric(row["fatalities"], errors="coerce"))
                else int(pd.to_numeric(row["fatalities"], errors="coerce")),
                "title": None if pd.isna(row["title"]) else str(row["title"])[:200],
                "source": None if pd.isna(row["source"]) else str(row["source"])[:100],
            },
        })

    kept.sort(key=lambda f: f["properties"]["date"])
    counts["kept"] = len(kept)
    return kept, counts


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--csv", help="a Global Landslide Catalog CSV downloaded by hand")
    args = parser.parse_args()

    path = args.csv or download()
    raw = pd.read_csv(path, dtype=str, encoding_errors="replace")
    features, counts = select(normalise(raw), district_polygons())

    years = sorted({f["properties"]["date"][:4] for f in features})
    out = {
        "type": "FeatureCollection",
        "source": "NASA Global Landslide Catalog (Kirschbaum et al. 2010, 2015), NASA Goddard Space Flight Center",
        "selection": f"Uttarakhand, located to {MAX_ACCURACY_KM} km or better, rain-triggered",
        "counts": counts,
        "features": features,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)

    print(json.dumps(counts, indent=1))
    print(f"Kept {len(features)} landslides ({years[0] if years else '-'}-{years[-1] if years else '-'}) -> {OUT}")


if __name__ == "__main__":
    main()
