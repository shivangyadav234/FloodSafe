"""
SoilGrids v2.0 (ISRIC) surface texture, warped onto the terrain grid.

build_terrain.py produces the topographic predictors; this adds the
soil ones, written to the same grid (same CRS, transform and shape) so
build_training_set.py can sample every predictor at one row/col without
per-point reprojection.

Outputs sand/clay/silt percent plus a derived NRCS Hydrologic Soil
Group raster (A-D encoded 1-4), the same simplified texture-triangle
rule server.py already uses for FFGS.

SoilGrids is read once over the state bbox via GDAL /vsicurl/ range
requests -- ISRIC's own recommendation for anything beyond a handful of
point lookups. Its point-query API takes 2-20+ seconds per point, so it
is never used here.

Source is the 1 km aggregated single-file COG rather than the 250 m
global .vrt that extract_watershed_soil.py uses: the .vrt stitches
thousands of remote tiles and GDAL crashes reading a multi-tile window
from it over HTTP. 1 km is ample here -- soil texture varies at
landscape scale, well above the 90 m terrain grid.

This module deliberately does NOT import _proj. SoilGrids is in
Interrupted Goode Homolosine, and the PROJ override that _proj applies
for pyproj's benefit makes rasterio's bundled GDAL abort on that
projection. Nothing here needs pyproj, so the override stays off.

Usage:
    python build_soil.py --res 90
"""

import argparse
import os
import time

import numpy as np
import rasterio
from rasterio.warp import reproject, Resampling, transform_bounds
from rasterio.windows import Window


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_ROOT = os.path.join(REPO_ROOT, "floodsafe", "data", "terrain")

SOILGRIDS_URL = (
    "/vsicurl/https://files.isric.org/soilgrids/latest/data_aggregated/"
    "1000m/{prop}/{prop}_{depth}_mean_1000.tif"
)
DEPTH = "0-5cm"
PROPERTIES = ["sand", "clay", "silt"]
NODATA = -32768
SCALE = 10.0  # mapped units are g/kg*10 -> percent

HSG_CODES = {"A": 1, "B": 2, "C": 3, "D": 4}


def log(msg):
    print(f"[soil] {msg}", flush=True)


def window_for_bounds(src, bounds, pad=0.0):
    """Pixel window covering `bounds` (in src's CRS), clamped to the raster.

    Computed from the affine transform by hand rather than with
    rasterio.windows.from_bounds, which segfaults the interpreter in this
    environment (rasterio 1.4.4 / numpy 2.4).
    """
    minx, miny, maxx, maxy = bounds
    a, _, c, _, e, f = (src.transform.a, src.transform.b, src.transform.c,
                        src.transform.d, src.transform.e, src.transform.f)

    cols = [(minx - pad - c) / a, (maxx + pad - c) / a]
    rows = [(maxy + pad - f) / e, (miny - pad - f) / e]

    col_off = max(0, int(np.floor(min(cols))))
    row_off = max(0, int(np.floor(min(rows))))
    col_end = min(src.width, int(np.ceil(max(cols))))
    row_end = min(src.height, int(np.ceil(max(rows))))

    if col_end <= col_off or row_end <= row_off:
        raise ValueError("requested bounds fall outside the source raster")

    return Window(col_off, row_off, col_end - col_off, row_end - row_off)


def hydrologic_soil_group(sand, clay):
    """Simplified NRCS HSG from surface texture, as integer codes.

    Coarse approximation of the full USDA lookup -- real HSG assignment
    also weighs depth-to-restrictive-layer and saturated conductivity,
    neither practical to source here. Kept byte-identical in logic to
    server.py's classify_hydrologic_soil_group so model features and the
    live FFGS adjustment can't drift apart.
    """
    hsg = np.zeros(sand.shape, dtype="uint8")  # 0 = no data
    valid = np.isfinite(sand) & np.isfinite(clay)

    d = valid & (clay >= 40)
    c = valid & ~d & ((clay >= 27) | ((clay >= 20) & (sand < 45)))
    a = valid & ~d & ~c & (sand >= 50) & (clay < 20)
    b = valid & ~d & ~c & ~a

    hsg[a], hsg[b], hsg[c], hsg[d] = 1, 2, 3, 4
    return hsg


def sample_property(prop, dst_profile, dst_bounds):
    """Window-read one SoilGrids property over the state and warp it
    onto the terrain grid."""

    url = SOILGRIDS_URL.format(prop=prop, depth=DEPTH)
    log(f"opening {prop} ...")
    t0 = time.time()

    with rasterio.open(url) as src:
        log(f"  opened in {time.time() - t0:.1f}s (crs={src.crs.to_string()[:40]})")

        # Only read the state's footprint, not the global mosaic.
        src_bounds = transform_bounds(dst_profile["crs"], src.crs, *dst_bounds)
        window = window_for_bounds(src, src_bounds, pad=3000.0)

        t1 = time.time()
        arr = src.read(1, window=window)
        src_transform = src.window_transform(window)
        log(f"  read {arr.shape[0]}x{arr.shape[1]} window in {time.time() - t1:.1f}s")

        out = np.full((dst_profile["height"], dst_profile["width"]), np.nan, dtype="float32")
        reproject(
            source=arr,
            destination=out,
            src_transform=src_transform,
            src_crs=src.crs,
            src_nodata=NODATA,
            dst_transform=dst_profile["transform"],
            dst_crs=dst_profile["crs"],
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )

    out = out / SCALE
    covered = np.isfinite(out).sum()
    log(f"  {prop}: {covered:,} cells with data "
        f"({100 * covered / out.size:.1f}% of grid)")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--res", type=int, default=90)
    args = ap.parse_args()

    out_dir = os.path.join(OUT_ROOT, f"{args.res}m")
    dem_path = os.path.join(out_dir, "dem.tif")

    if not os.path.exists(dem_path):
        raise SystemExit(f"terrain grid missing: {dem_path}\nRun build_terrain.py first.")

    with rasterio.open(dem_path) as dem:
        profile = dem.profile.copy()
        dst_bounds = dem.bounds
        dem_valid = dem.read(1) != dem.nodata

    log(f"target grid {profile['width']} x {profile['height']} @ {args.res} m")

    bands = {p: sample_property(p, profile, dst_bounds) for p in PROPERTIES}

    hsg = hydrologic_soil_group(bands["sand"], bands["clay"])

    # Report coverage where it actually matters -- inside the state.
    inside = dem_valid & (hsg > 0)
    log(f"HSG assigned for {inside.sum():,} of {dem_valid.sum():,} in-state cells "
        f"({100 * inside.sum() / max(dem_valid.sum(), 1):.1f}%)")
    for name, code in HSG_CODES.items():
        n = int(((hsg == code) & dem_valid).sum())
        log(f"  group {name}: {n:,} cells")

    float_profile = {**profile, "dtype": "float32", "nodata": -9999.0}
    for prop, arr in bands.items():
        path = os.path.join(out_dir, f"soil_{prop}.tif")
        with rasterio.open(path, "w", **float_profile) as dst:
            dst.write(np.where(np.isfinite(arr), arr, -9999.0).astype("float32"), 1)
        log(f"wrote {path}")

    hsg_profile = {**profile, "dtype": "uint8", "nodata": 0}
    hsg_path = os.path.join(out_dir, "soil_hsg.tif")
    with rasterio.open(hsg_path, "w", **hsg_profile) as dst:
        dst.write(hsg, 1)
    log(f"wrote {hsg_path}")


if __name__ == "__main__":
    main()
