"""
Flash Flood Potential Index (FFPI) for Uttarakhand.

A physically-based susceptibility surface built from four real datasets,
with no training labels involved:

    slope        Copernicus GLO-30 DEM via WhiteboxTools
    soil         SoilGrids v2.0 -> NRCS Hydrologic Soil Group
    land cover   ESA WorldCover 2021 v200
    convergence  topographic wetness index (upslope area / slope)

Why this exists alongside the XGBoost model
-------------------------------------------
The hazard atlas covers only ~9.7% of the state, so the live system
reports UNMAPPED almost everywhere. The obvious fix -- train a model on
the atlas and extrapolate -- turns out to be weak: at polygon level the
atlas classes separate on elevation (AUC 0.74) and on essentially
nothing else (slope p=0.62, TWI p=0.11, distance-to-stream p=0.54), and
honest spatial cross-validation caps the model near ROC-AUC 0.66.

FFPI sidesteps the label problem entirely. It is the standard approach
used in operational flash flood guidance (Smith 2003, adopted by NWS
river forecast centres): reindex each physical driver onto a common
1-10 scale and take a weighted mean. Slope carries double weight because
runoff concentration time is the dominant control on flash flooding.

It is a susceptibility index, not a calibrated probability, and is
labelled as such everywhere it surfaces.

Usage:
    python build_ffpi.py --res 90
"""

import argparse
import json
import os
from datetime import datetime, timezone

import numpy as np
import rasterio


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_ROOT = os.path.join(REPO_ROOT, "floodsafe", "data", "terrain")

# Each driver is reindexed to 1-10, higher = more flash-flood potential.
# Breakpoints are (upper_bound, index), evaluated in order.
SLOPE_BREAKS = [
    (2, 1), (5, 2), (10, 3), (15, 4), (20, 5),
    (25, 6), (30, 7), (35, 8), (45, 9), (1e9, 10),
]

# Hydrologic Soil Group A-D (1-4 in the raster) by infiltration capacity:
# A drains freely, D barely at all.
SOIL_INDEX = {1: 2.0, 2: 4.0, 3: 7.0, 4: 10.0}

# ESA WorldCover class -> runoff potential. Forest intercepts and
# infiltrates; built-up and bare ground shed water. Snow and ice scores
# high because Uttarakhand's severe events are glacier-linked
# (rain-on-snow, GLOF), which the hazard atlas itself reflects -- its
# EXTREME polygons sit at a median 3,554 m.
LANDCOVER_INDEX = {
    10: 2.0,    # tree cover
    20: 4.0,    # shrubland
    30: 5.0,    # grassland
    40: 6.0,    # cropland
    50: 10.0,   # built-up
    60: 8.0,    # bare / sparse vegetation
    70: 9.0,    # snow and ice
    80: 1.0,    # permanent water
    90: 3.0,    # herbaceous wetland
    95: 2.0,    # mangroves
    100: 7.0,   # moss and lichen
}

# TWI marks where flow converges. Breakpoints follow the observed
# in-state distribution (median ~6.1, long right tail to ~54).
TWI_BREAKS = [
    (4.5, 1), (5.5, 2), (6.0, 3), (6.5, 4), (7.0, 5),
    (8.0, 6), (9.5, 7), (12.0, 8), (16.0, 9), (1e9, 10),
]

WEIGHTS = {"slope": 2.0, "soil": 1.0, "landcover": 1.0, "convergence": 1.0}

# Display bands over the 1-10 FFPI scale.
FFPI_BANDS = [
    (3.5, "VERY LOW"),
    (4.5, "LOW"),
    (5.5, "MODERATE"),
    (6.5, "HIGH"),
    (99.0, "VERY HIGH"),
]


def log(msg):
    print(f"[ffpi] {msg}", flush=True)


def read_band(out_dir, name):
    path = os.path.join(out_dir, name)
    if not os.path.exists(path):
        return None, None
    with rasterio.open(path) as src:
        arr = src.read(1).astype("float64")
        nodata = src.nodata
        profile = src.profile.copy()
    if nodata is not None:
        arr = np.where(arr == nodata, np.nan, arr)
    arr = np.where(arr < -1e30, np.nan, arr)
    return arr, profile


def reindex_continuous(arr, breaks):
    """Map a continuous raster onto the 1-10 index via upper-bound breakpoints."""
    out = np.full(arr.shape, np.nan)
    remaining = np.isfinite(arr)
    for upper, index in breaks:
        hit = remaining & (arr < upper)
        out[hit] = index
        remaining &= ~hit
    return out


def reindex_categorical(arr, mapping):
    out = np.full(arr.shape, np.nan)
    for code, index in mapping.items():
        out[arr == code] = index
    return out


def band_for(value):
    for upper, name in FFPI_BANDS:
        if value < upper:
            return name
    return FFPI_BANDS[-1][1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--res", type=int, default=90)
    args = ap.parse_args()

    out_dir = os.path.join(OUT_ROOT, f"{args.res}m")

    dem, profile = read_band(out_dir, "dem.tif")
    if dem is None:
        raise SystemExit(f"terrain grid missing in {out_dir}\nRun build_terrain.py first.")
    in_state = np.isfinite(dem)
    log(f"in-state cells: {int(in_state.sum()):,}")

    slope, _ = read_band(out_dir, "slope.tif")
    twi, _ = read_band(out_dir, "twi.tif")
    hsg, _ = read_band(out_dir, "soil_hsg.tif")
    landcover, _ = read_band(out_dir, "landcover.tif")

    missing = [n for n, a in [("slope", slope), ("twi", twi),
                              ("soil_hsg", hsg), ("landcover", landcover)] if a is None]
    if missing:
        raise SystemExit(f"missing inputs: {missing}\n"
                         "Run build_terrain.py, build_soil.py and build_landcover.py first.")

    components = {
        "slope": reindex_continuous(slope, SLOPE_BREAKS),
        "convergence": reindex_continuous(twi, TWI_BREAKS),
        "soil": reindex_categorical(hsg, SOIL_INDEX),
        "landcover": reindex_categorical(landcover, LANDCOVER_INDEX),
    }

    for name, arr in components.items():
        cov = np.isfinite(arr) & in_state
        log(f"  {name:12s} {100 * cov.sum() / max(in_state.sum(), 1):5.1f}% of in-state cells  "
            f"mean index {np.nanmean(np.where(in_state, arr, np.nan)):.2f}")

    # Weighted mean over whichever components a cell actually has, so a
    # gap in one layer degrades the score rather than voiding the cell.
    total = np.zeros(dem.shape)
    weight = np.zeros(dem.shape)
    for name, arr in components.items():
        ok = np.isfinite(arr)
        w = WEIGHTS[name]
        total[ok] += arr[ok] * w
        weight[ok] += w

    ffpi = np.where(weight > 0, total / np.maximum(weight, 1e-9), np.nan)
    ffpi = np.where(in_state, ffpi, np.nan)

    # Require at least slope plus one other driver before trusting a cell.
    enough = weight >= (WEIGHTS["slope"] + 1.0)
    ffpi = np.where(enough, ffpi, np.nan)

    valid = np.isfinite(ffpi)
    log(f"FFPI computed for {int(valid.sum()):,} cells "
        f"({100 * valid.sum() / max(in_state.sum(), 1):.1f}% of state)")
    log(f"  range {np.nanmin(ffpi):.2f} .. {np.nanmax(ffpi):.2f}  "
        f"mean {np.nanmean(ffpi):.2f}  median {np.nanmedian(ffpi):.2f}")

    prev = -np.inf
    for upper, name in FFPI_BANDS:
        n = int((valid & (ffpi >= prev) & (ffpi < upper)).sum())
        log(f"  {name:10s} {n:10,d} cells ({100 * n / max(valid.sum(), 1):5.1f}%)")
        prev = upper

    out_profile = {**profile, "dtype": "float32", "nodata": -9999.0}
    ffpi_path = os.path.join(out_dir, "ffpi.tif")
    with rasterio.open(ffpi_path, "w", **out_profile) as dst:
        dst.write(np.where(valid, ffpi, -9999.0).astype("float32"), 1)
    log(f"wrote {ffpi_path}")

    for name, arr in components.items():
        p = os.path.join(out_dir, f"ffpi_{name}.tif")
        with rasterio.open(p, "w", **out_profile) as dst:
            dst.write(np.where(np.isfinite(arr) & in_state, arr, -9999.0).astype("float32"), 1)
    log("wrote per-component index rasters")

    meta = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "resolution_m": args.res,
        "method": "Flash Flood Potential Index (Smith 2003), weighted mean of "
                  "1-10 reindexed physical drivers",
        "weights": WEIGHTS,
        "sources": {
            "slope": "Copernicus DEM GLO-30 (ESA) via WhiteboxTools",
            "convergence": "topographic wetness index from the same DEM",
            "soil": "SoilGrids v2.0 (ISRIC) -> simplified NRCS Hydrologic Soil Group",
            "landcover": "ESA WorldCover 2021 v200",
        },
        "scale": "1-10, higher = greater flash-flood potential",
        "is_probability": False,
        "coverage_cells": int(valid.sum()),
        "statistics": {
            "min": float(np.nanmin(ffpi)), "max": float(np.nanmax(ffpi)),
            "mean": float(np.nanmean(ffpi)), "median": float(np.nanmedian(ffpi)),
        },
        "bands": [{"upper": u, "name": n} for u, n in FFPI_BANDS],
    }
    meta_path = os.path.join(out_dir, "ffpi.meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    log(f"wrote {meta_path}")


if __name__ == "__main__":
    main()
