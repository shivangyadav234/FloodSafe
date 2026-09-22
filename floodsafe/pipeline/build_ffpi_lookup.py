"""
Resample the FFPI raster onto a regular lat/lon grid the Flask app can
read with numpy alone.

server.py runs on a free Render instance whose dependency list is
deliberately small; adding rasterio and GDAL there just to sample one
raster would be a heavy build for a single point lookup. Instead the
index is baked into a small uint8 array with a plain affine mapping from
lat/lon to row/col.

Encoding: value = round(FFPI * 25), so 0-250 covers the 0-10 scale in
0.04 steps; 255 means no data.

Grid spacing is 0.001 deg (~110 m), deliberately close to the 90 m
source. A coarser 0.0025 deg grid was tried first and smoothed local
peaks away -- steep sites like Mussoorie lost more than a full FFPI unit
to bilinear averaging, which would have understated exactly the terrain
the index exists to flag. Compresses to a few MB.

Usage:
    python build_ffpi_lookup.py --res 90
"""

import argparse
import json
import os

import numpy as np
import rasterio
from rasterio.warp import reproject, Resampling
from rasterio.transform import from_origin


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_ROOT = os.path.join(REPO_ROOT, "floodsafe", "data", "terrain")
APP_DATA = os.path.join(REPO_ROOT, "data", "flood", "uttarakhand", "data")

# Uttarakhand, padded slightly so edge lookups don't fall outside.
LON_MIN, LON_MAX = 77.50, 81.10
LAT_MIN, LAT_MAX = 28.65, 31.50
STEP_DEG = 0.001  # ~110 m in latitude, near the 90 m source grid

SCALE = 25.0
NODATA = 255


def log(msg):
    print(f"[lookup] {msg}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--res", type=int, default=90)
    args = ap.parse_args()

    src_path = os.path.join(OUT_ROOT, f"{args.res}m", "ffpi.tif")
    if not os.path.exists(src_path):
        raise SystemExit(f"missing {src_path}\nRun build_ffpi.py first.")

    width = int(round((LON_MAX - LON_MIN) / STEP_DEG))
    height = int(round((LAT_MAX - LAT_MIN) / STEP_DEG))
    transform = from_origin(LON_MIN, LAT_MAX, STEP_DEG, STEP_DEG)

    log(f"lookup grid {width} x {height} @ {STEP_DEG} deg")

    dest = np.full((height, width), np.nan, dtype="float32")

    with rasterio.open(src_path) as src:
        reproject(
            source=rasterio.band(src, 1),
            destination=dest,
            src_transform=src.transform,
            src_crs=src.crs,
            src_nodata=src.nodata,
            dst_transform=transform,
            dst_crs="EPSG:4326",
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )

    valid = np.isfinite(dest)
    log(f"populated {int(valid.sum()):,} of {dest.size:,} cells "
        f"({100 * valid.sum() / dest.size:.1f}%)")
    log(f"  range {np.nanmin(dest):.2f} .. {np.nanmax(dest):.2f}")

    encoded = np.full(dest.shape, NODATA, dtype="uint8")
    scaled = np.clip(np.round(dest * SCALE), 0, 250)
    encoded[valid] = scaled[valid].astype("uint8")

    os.makedirs(APP_DATA, exist_ok=True)
    out_path = os.path.join(APP_DATA, "ffpi_lookup.npz")
    np.savez_compressed(
        out_path,
        ffpi=encoded,
        bounds=np.array([LON_MIN, LAT_MIN, LON_MAX, LAT_MAX], dtype="float64"),
        step=np.float64(STEP_DEG),
        scale=np.float64(SCALE),
        nodata=np.uint8(NODATA),
    )
    log(f"wrote {out_path}  ({os.path.getsize(out_path) / 1e6:.2f} MB)")

    # Round-trip check: decode a few known cells and confirm they match.
    with np.load(out_path) as z:
        back = z["ffpi"].astype("float32")
    back[back == NODATA] = np.nan
    back /= SCALE
    diff = np.abs(back[valid] - dest[valid])
    log(f"round-trip max error {np.nanmax(diff):.4f} FFPI units")

    meta_src = os.path.join(OUT_ROOT, f"{args.res}m", "ffpi.meta.json")
    if os.path.exists(meta_src):
        with open(meta_src, "r", encoding="utf-8") as f:
            meta = json.load(f)
        meta["lookup_grid"] = {
            "bounds": [LON_MIN, LAT_MIN, LON_MAX, LAT_MAX],
            "step_deg": STEP_DEG, "scale": SCALE, "nodata": NODATA,
        }
        with open(os.path.join(APP_DATA, "ffpi.meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        log("wrote app-side ffpi.meta.json")


if __name__ == "__main__":
    main()
