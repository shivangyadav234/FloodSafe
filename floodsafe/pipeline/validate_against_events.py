"""
Validate FFPI against real recorded disasters.

Everything until now was validated against the hazard atlas -- a map of
where hazard is *believed* to be. This tests something harder: does FFPI
actually score higher where rainfall-triggered mass movements have
really happened?

Source is NASA's Global Landslide Catalog (11,033 global events,
1970-2019), via the HDX mirror. 206 fall inside Uttarakhand, 193 of them
rainfall-triggered, together accounting for 5,276 recorded deaths.
FFPI has never seen any of it, so this is a genuinely independent test
-- unlike the atlas comparison, where both describe the same belief.

Three things are handled honestly rather than quietly:

Location accuracy. The catalog records how precisely each event is
located, and it ranges from 1 km to 50 km. A 50 km radius covers
entirely different terrain at 90 m resolution, so validating against
those points would be measuring nothing. Results are reported per
accuracy cut so the effect is visible instead of assumed away.

Reporting bias. The catalog is largely news-sourced, so events cluster
near roads and settlements -- places people are present to report from.
Scoring them against background points drawn uniformly from wilderness
would partly measure accessibility rather than hazard. Both background
strategies are therefore run: uniform, and matched to settlement
proximity.

Landslides are not floods. In Himalayan terrain the two share a
mechanism -- cloudbursts triggering debris flows down steep channels --
but they are not the same phenomenon. This validates rainfall-triggered
mass-movement susceptibility, which overlaps flash flooding without
being identical to it.

Usage:
    python validate_against_events.py --res 90
"""

import argparse
import json
import os

import _proj  # noqa: F401  -- geopandas needs the PROJ override

import numpy as np
import pandas as pd
import rasterio
import geopandas as gpd
from shapely.geometry import shape, Point
from shapely.ops import unary_union
from sklearn.metrics import roc_auc_score
from scipy.stats import mannwhitneyu


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_DATA = os.path.join(REPO_ROOT, "data", "flood", "uttarakhand", "data")
EVENTS_SHP = os.path.join(REPO_ROOT, "floodsafe", "data", "events", "glc",
                          "global_landslide_catalog_NASA.shp")
TERRAIN_ROOT = os.path.join(REPO_ROOT, "floodsafe", "data", "terrain")
OUT_DIR = os.path.join(REPO_ROOT, "floodsafe", "data", "events")

RAINFALL_TRIGGERS = {"downpour", "rain", "continuous_rain", "monsoon",
                     "tropical_cyclone", "flooding"}

# Ordered, so "<= 5km" means these two.
ACCURACY_ORDER = ["1km", "5km", "10km", "25km", "50km"]

BACKGROUND_PER_POSITIVE = 20
MIN_BACKGROUND_DIST_KM = 5.0
SETTLEMENT_MATCH_KM = 10.0
SEED = 42


def log(msg):
    print(f"[events] {msg}", flush=True)


def state_geometry():
    with open(os.path.join(APP_DATA, "uttarakhand_boundary.geojson"),
              "r", encoding="utf-8") as f:
        gj = json.load(f)
    return unary_union([shape(f["geometry"]) for f in gj["features"]])


def load_events(state):
    g = gpd.read_file(EVENTS_SHP)
    uk = g.cx[77.5:81.1, 28.7:31.5].copy()
    uk = uk[uk.geometry.within(state)].copy()

    uk["trigger"] = uk["landslid_1"].fillna("unknown")
    uk["accuracy"] = uk["location_a"].fillna("unknown")
    rain = uk[uk["trigger"].isin(RAINFALL_TRIGGERS)].copy()

    log(f"events inside state: {len(uk)}  (rainfall-triggered: {len(rain)})")
    log(f"  fatalities recorded: {int(uk['fatality_c'].fillna(0).sum()):,}")
    log(f"  accuracy: {rain['accuracy'].value_counts().to_dict()}")
    return rain


def sample_background(state, events, localities, n, rng, settlement_matched):
    """Background points, either uniform over the state or matched to
    settlement proximity so accessibility bias is comparable."""
    minx, miny, maxx, maxy = state.bounds
    ev_coords = np.radians(np.column_stack(
        [events.geometry.y.to_numpy(), events.geometry.x.to_numpy()]))

    loc_coords = None
    if settlement_matched and len(localities):
        loc_coords = np.radians(np.column_stack(
            [localities["lat"].to_numpy(), localities["lon"].to_numpy()]))

    def min_km(lat, lon, ref):
        p = np.radians([lat, lon])
        dphi = ref[:, 0] - p[0]
        dlam = ref[:, 1] - p[1]
        a = (np.sin(dphi / 2) ** 2
             + np.cos(p[0]) * np.cos(ref[:, 0]) * np.sin(dlam / 2) ** 2)
        return float(np.min(2 * 6371.0 * np.arcsin(np.sqrt(a))))

    out, guard = [], 0
    while len(out) < n and guard < n * 400:
        guard += 1
        lon = rng.uniform(minx, maxx)
        lat = rng.uniform(miny, maxy)
        if not state.contains(Point(lon, lat)):
            continue
        # Never place a "no event here" point next to a recorded event.
        if min_km(lat, lon, ev_coords) < MIN_BACKGROUND_DIST_KM:
            continue
        if loc_coords is not None and min_km(lat, lon, loc_coords) > SETTLEMENT_MATCH_KM:
            continue
        out.append((lat, lon))

    return pd.DataFrame(out, columns=["lat", "lon"])


def sample_raster(df, out_dir, name, fname):
    """Index a raster at lat/lon points via the reference grid."""
    with rasterio.open(os.path.join(out_dir, "dem.tif")) as ref:
        transform, height, width, crs = ref.transform, ref.height, ref.width, ref.crs

    from rasterio.warp import transform as warp_transform
    xs, ys = warp_transform("EPSG:4326", crs,
                            df["lon"].tolist(), df["lat"].tolist())
    cols = ((np.array(xs) - transform.c) / transform.a).astype("int64")
    rows = ((np.array(ys) - transform.f) / transform.e).astype("int64")
    inside = (rows >= 0) & (rows < height) & (cols >= 0) & (cols < width)

    path = os.path.join(out_dir, fname)
    with rasterio.open(path) as src:
        band = src.read(1)
        nodata = src.nodata

    vals = band[np.clip(rows, 0, height - 1), np.clip(cols, 0, width - 1)].astype("float64")
    vals[~inside] = np.nan
    if nodata is not None:
        vals = np.where(vals == nodata, np.nan, vals)
    vals = np.where(vals < -1e30, np.nan, vals)
    df[name] = vals
    return df


def score(pos, neg, column):
    a = pos[column].dropna()
    b = neg[column].dropna()
    if len(a) < 5 or len(b) < 5:
        return None
    y = np.r_[np.ones(len(a)), np.zeros(len(b))]
    v = np.r_[a.to_numpy(), b.to_numpy()]
    auc = roc_auc_score(y, v)
    _, p = mannwhitneyu(a, b)
    return {"auc": float(auc), "p": float(p),
            "median_event": float(a.median()), "median_background": float(b.median()),
            "n_event": int(len(a)), "n_background": int(len(b))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--res", type=int, default=90)
    args = ap.parse_args()

    out_dir = os.path.join(TERRAIN_ROOT, f"{args.res}m")
    rng = np.random.default_rng(SEED)

    state = state_geometry()
    events = load_events(state)

    with open(os.path.join(APP_DATA, "localities.json"), "r", encoding="utf-8") as f:
        localities = pd.DataFrame(json.load(f))

    layers = {"ffpi": "ffpi.tif", "slope_deg": "slope.tif", "twi": "twi.tif",
              "hand_m": "hand.tif", "dist_to_stream_m": "dist_to_stream.tif"}

    results = {}

    for cut in ["1km", "5km", "10km", "25km", "50km"]:
        keep = ACCURACY_ORDER[:ACCURACY_ORDER.index(cut) + 1]
        pos = events[events["accuracy"].isin(keep)].copy()
        if len(pos) < 15:
            log(f"<= {cut}: only {len(pos)} events, skipping")
            continue

        pos = pos.assign(lat=pos.geometry.y, lon=pos.geometry.x)[["lat", "lon"]]

        for matched in (False, True):
            tag = "settlement-matched" if matched else "uniform"
            neg = sample_background(state, events, localities,
                                    len(pos) * BACKGROUND_PER_POSITIVE, rng, matched)

            p_df, n_df = pos.copy(), neg.copy()
            for name, fname in layers.items():
                p_df = sample_raster(p_df, out_dir, name, fname)
                n_df = sample_raster(n_df, out_dir, name, fname)

            s = score(p_df, n_df, "ffpi")
            if s is None:
                continue
            results[f"{cut}|{tag}"] = s
            log(f"<= {cut:4s} [{tag:18s}]  n={s['n_event']:3d} events vs "
                f"{s['n_background']:4d} background   FFPI AUC {s['auc'] * 100:5.1f}%   "
                f"p={s['p']:.2e}   median {s['median_event']:.2f} vs {s['median_background']:.2f}")

            if cut == "5km" and matched:
                log("    per-layer on this cut:")
                for name in layers:
                    ss = score(p_df, n_df, name)
                    if ss:
                        log(f"      {name:18s} AUC {ss['auc'] * 100:5.1f}%  p={ss['p']:.1e}")

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "ffpi_event_validation.json"), "w",
              encoding="utf-8") as f:
        json.dump({
            "source": "NASA Global Landslide Catalog (1970-2019) via HDX",
            "note": "FFPI never saw these events; independent validation. "
                    "Landslides overlap but are not identical to flash floods.",
            "background_per_positive": BACKGROUND_PER_POSITIVE,
            "min_background_distance_km": MIN_BACKGROUND_DIST_KM,
            "results": results,
        }, f, indent=2)
    log(f"wrote {os.path.join(OUT_DIR, 'ffpi_event_validation.json')}")


if __name__ == "__main__":
    main()
