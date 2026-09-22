"""
ESA WorldCover 2021 v200 (10 m) land cover, warped onto the terrain grid.

The fourth real dataset behind the Flash Flood Potential Index, after
terrain (Copernicus DEM), soil (SoilGrids) and catchments (HydroBASINS).
Land cover drives how much rain becomes runoff rather than infiltrating
or being intercepted by canopy.

Public AWS bucket, no credentials. Tiles are on a 3-degree grid, so
Uttarakhand needs six of them (~540 MB), cached on disk.

Resampled 10 m -> 90 m by majority class: land cover is categorical, so
averaging it would be meaningless.

Usage:
    python build_landcover.py --res 90
"""

import argparse
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import requests
import rasterio
from rasterio.warp import reproject, Resampling


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_ROOT = os.path.join(REPO_ROOT, "floodsafe", "data", "terrain")
LC_CACHE = os.path.join(REPO_ROOT, "floodsafe", "data", "landcover_tiles")

WORLDCOVER_URL = (
    "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
    "ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
)

# Uttarakhand spans lat 28.7-31.5, lon 77.6-81.0; WorldCover tiles are
# 3x3 degrees anchored on multiples of 3.
TILE_LATS = [27, 30]
TILE_LONS = [75, 78, 81]

CLASS_NAMES = {
    10: "tree cover", 20: "shrubland", 30: "grassland", 40: "cropland",
    50: "built-up", 60: "bare / sparse", 70: "snow and ice",
    80: "permanent water", 90: "herbaceous wetland", 95: "mangroves",
    100: "moss and lichen",
}


def log(msg):
    print(f"[landcover] {msg}", flush=True)


def tile_names():
    return [f"N{lat:02d}E{lon:03d}" for lat in TILE_LATS for lon in TILE_LONS]


def download_tile(tile):
    path = os.path.join(LC_CACHE, f"{tile}.tif")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path

    url = WORLDCOVER_URL.format(tile=tile)
    tmp = path + ".part"

    for attempt in range(3):
        try:
            with requests.get(url, stream=True, timeout=300) as r:
                if r.status_code == 404:
                    log(f"  {tile}: no tile published (outside coverage)")
                    return None
                r.raise_for_status()
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        fh.write(chunk)
            os.replace(tmp, path)
            return path
        except Exception as exc:
            if attempt == 2:
                log(f"  FAILED {tile}: {exc}")
                return None
            time.sleep(3 * (attempt + 1))
    return None


def fetch_tiles():
    os.makedirs(LC_CACHE, exist_ok=True)
    tiles = tile_names()
    log(f"land cover tiles needed: {len(tiles)}")

    paths = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(download_tile, t): t for t in tiles}
        for i, fut in enumerate(as_completed(futures), 1):
            p = fut.result()
            if p:
                paths.append(p)
            log(f"  {i}/{len(tiles)} tiles resolved")

    if not paths:
        raise RuntimeError("no WorldCover tiles downloaded")
    return sorted(paths)


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
        dem_valid = dem.read(1) != dem.nodata

    log(f"target grid {profile['width']} x {profile['height']} @ {args.res} m")

    tiles = fetch_tiles()
    dest = np.zeros((profile["height"], profile["width"]), dtype="uint8")

    for i, path in enumerate(tiles, 1):
        with rasterio.open(path) as src:
            block = np.zeros((profile["height"], profile["width"]), dtype="uint8")
            reproject(
                source=rasterio.band(src, 1),
                destination=block,
                src_transform=src.transform,
                src_crs=src.crs,
                src_nodata=0,
                dst_transform=profile["transform"],
                dst_crs=profile["crs"],
                dst_nodata=0,
                resampling=Resampling.mode,
            )
            filled = block > 0
            dest[filled] = block[filled]
        log(f"  warped {i}/{len(tiles)}")

    inside = dem_valid & (dest > 0)
    log(f"land cover assigned for {inside.sum():,} of {dem_valid.sum():,} "
        f"in-state cells ({100 * inside.sum() / max(dem_valid.sum(), 1):.1f}%)")

    total = max(int(inside.sum()), 1)
    for code, name in CLASS_NAMES.items():
        n = int(((dest == code) & dem_valid).sum())
        if n:
            log(f"  {code:4d} {name:20s} {n:10,d} cells ({100 * n / total:5.1f}%)")

    lc_profile = {**profile, "dtype": "uint8", "nodata": 0}
    out_path = os.path.join(out_dir, "landcover.tif")
    with rasterio.open(out_path, "w", **lc_profile) as dst:
        dst.write(dest, 1)
    log(f"wrote {out_path}")


if __name__ == "__main__":
    main()
