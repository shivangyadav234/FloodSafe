"""
Terrain analysis for Uttarakhand: Copernicus GLO-30 DEM -> WhiteboxTools
hydrological derivatives.

One-time build step. Produces the raster predictors that
build_training_set.py samples to train the flash-flood susceptibility
model:

    elevation, slope, plan/profile curvature, flow accumulation (as
    specific catchment area), topographic wetness index, stream power
    index, distance to the nearest stream, and terrain ruggedness.

DEM source is Copernicus DEM GLO-30 (ESA, 30 m), read from its public
AWS bucket -- no credentials, unlike SRTM via NASA Earthdata. Tiles are
cached on disk so re-runs at a different --res don't re-download ~800 MB.

Flow routing runs on a metric projection (UTM 44N), not lat/lon: slope
and contributing area are meaningless in degrees.

Usage:
    python build_terrain.py                 # 90 m (default, ~2 min of routing)
    python build_terrain.py --res 30        # native 30 m, much slower
"""

import argparse
import os
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import requests
import rasterio
from rasterio.warp import calculate_default_transform, reproject, Resampling, transform_geom
from rasterio.features import geometry_mask
from shapely.geometry import shape
from shapely.ops import unary_union


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LEGACY_DATA = os.path.join(REPO_ROOT, "data", "flood", "uttarakhand", "data")
BOUNDARY_FILE = os.path.join(LEGACY_DATA, "uttarakhand_boundary.geojson")

OUT_ROOT = os.path.join(REPO_ROOT, "floodsafe", "data", "terrain")
DEM_CACHE = os.path.join(REPO_ROOT, "floodsafe", "data", "dem_tiles")

# UTM 44N. Uttarakhand's west edge sits ~3.5 deg off the 81E central
# meridian, so scale error stays about 0.2% -- immaterial for slope and
# contributing area, and far better than routing flow in degrees.
TARGET_CRS = "EPSG:32644"

COP30_URL = (
    "https://copernicus-dem-30m.s3.amazonaws.com/"
    "Copernicus_DSM_COG_10_{tile}_DEM/Copernicus_DSM_COG_10_{tile}_DEM.tif"
)

# Cells of upslope contributing area required before a cell counts as a
# channel. At 90 m a cell is 0.81 ha, so 500 cells is ~4 km2 -- roughly
# the scale at which Himalayan headwater streams become mapped channels.
STREAM_THRESHOLD_CELLS = 500


def log(msg):
    print(f"[terrain] {msg}", flush=True)


def state_geometry():
    with open(BOUNDARY_FILE, "r", encoding="utf-8") as f:
        gj = json.load(f)
    return unary_union([shape(f["geometry"]) for f in gj["features"]])


def tiles_for_bounds(bounds):
    """Copernicus tile names covering a lon/lat bbox (1 deg tiles)."""
    minx, miny, maxx, maxy = bounds
    names = []
    for lat in range(math.floor(miny), math.floor(maxy) + 1):
        for lon in range(math.floor(minx), math.floor(maxx) + 1):
            ns = "N" if lat >= 0 else "S"
            ew = "E" if lon >= 0 else "W"
            names.append(f"{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00")
    return names


def download_tile(tile):
    """Fetch one DEM tile into the cache. Returns its path, or None if
    the tile doesn't exist (ocean / outside coverage)."""
    path = os.path.join(DEM_CACHE, f"{tile}.tif")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path

    url = COP30_URL.format(tile=tile)
    tmp = path + ".part"

    for attempt in range(3):
        try:
            with requests.get(url, stream=True, timeout=120) as r:
                if r.status_code == 404:
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
            time.sleep(2 * (attempt + 1))

    return None


def fetch_dem_tiles(bounds):
    os.makedirs(DEM_CACHE, exist_ok=True)
    tiles = tiles_for_bounds(bounds)
    log(f"DEM tiles needed: {len(tiles)}")

    paths = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(download_tile, t): t for t in tiles}
        for i, fut in enumerate(as_completed(futures), 1):
            p = fut.result()
            if p:
                paths.append(p)
            log(f"  {i}/{len(tiles)} tiles ready")

    if not paths:
        raise RuntimeError("no DEM tiles downloaded")

    log(f"DEM tiles on disk: {len(paths)}")
    return sorted(paths)


def build_clipped_dem(tile_paths, geom, res_m, out_path):
    """Reproject every tile into one UTM raster at res_m and mask to the
    state boundary."""

    # Destination grid: bbox of the state in UTM, snapped to res_m.
    with rasterio.open(tile_paths[0]) as src0:
        src_crs = src0.crs

    minx, miny, maxx, maxy = geom.bounds
    transform, width, height = calculate_default_transform(
        src_crs, TARGET_CRS, 1, 1,
        left=minx, bottom=miny, right=maxx, top=maxy,
        resolution=res_m,
    )

    log(f"destination grid: {width} x {height} @ {res_m} m ({TARGET_CRS})")
    dest = np.full((height, width), np.nan, dtype="float32")

    for i, path in enumerate(tile_paths, 1):
        with rasterio.open(path) as src:
            block = np.full((height, width), np.nan, dtype="float32")
            reproject(
                source=rasterio.band(src, 1),
                destination=block,
                src_transform=src.transform,
                src_crs=src.crs,
                src_nodata=src.nodata,
                dst_transform=transform,
                dst_crs=TARGET_CRS,
                dst_nodata=np.nan,
                resampling=Resampling.average if res_m > 30 else Resampling.bilinear,
            )
            filled = np.isfinite(block)
            dest[filled] = block[filled]
        log(f"  mosaicked {i}/{len(tile_paths)}")

    # Clip to the state so flow routing isn't driven by terrain outside
    # the study area. Reprojected through GDAL rather than pyproj: the two
    # resolve PROJ data differently in this environment (see _proj.py).
    geom_utm = shape(transform_geom("EPSG:4326", TARGET_CRS, geom.__geo_interface__))

    mask = geometry_mask(
        [geom_utm], out_shape=(height, width),
        transform=transform, invert=True,
    )
    dest[~mask] = np.nan

    valid = int(np.isfinite(dest).sum())
    log(f"valid DEM cells inside state: {valid:,} "
        f"({valid * res_m * res_m / 1e6:,.0f} km2)")

    profile = {
        "driver": "GTiff", "height": height, "width": width, "count": 1,
        "dtype": "float32", "crs": TARGET_CRS, "transform": transform,
        "nodata": -32768.0, "compress": "deflate", "tiled": True,
    }
    out = np.where(np.isfinite(dest), dest, -32768.0).astype("float32")
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(out, 1)

    log(f"wrote {out_path}")
    return out_path


def run_whitebox(dem_path, out_dir, res_m):
    """WhiteboxTools hydrological chain. Each output is a raster
    predictor for the susceptibility model."""

    import whitebox

    wbt = whitebox.WhiteboxTools()
    wbt.set_working_dir(out_dir)
    wbt.set_verbose_mode(False)

    dem = os.path.basename(dem_path)

    def step(name, fn):
        t0 = time.time()
        log(f"  {name} ...")
        rc = fn()
        if rc != 0:
            raise RuntimeError(f"WhiteboxTools step failed: {name} (rc={rc})")
        log(f"  {name} done in {time.time() - t0:.0f}s")

    # Breaching beats filling for flow routing: it cuts through blockages
    # instead of flooding whole valleys flat, which keeps the drainage
    # network realistic in steep terrain.
    step("breach depressions", lambda: wbt.breach_depressions_least_cost(
        dem, "dem_breached.tif", dist=100, fill=True))

    step("d8 pointer", lambda: wbt.d8_pointer(
        "dem_breached.tif", "d8_pointer.tif"))

    step("flow accumulation (SCA)", lambda: wbt.d8_flow_accumulation(
        "dem_breached.tif", "sca.tif", out_type="specific contributing area"))

    step("flow accumulation (cells)", lambda: wbt.d8_flow_accumulation(
        "dem_breached.tif", "flowacc_cells.tif", out_type="cells"))

    step("slope", lambda: wbt.slope("dem_breached.tif", "slope.tif", units="degrees"))

    step("plan curvature", lambda: wbt.plan_curvature("dem_breached.tif", "plan_curv.tif"))
    step("profile curvature", lambda: wbt.profile_curvature("dem_breached.tif", "prof_curv.tif"))

    step("topographic wetness index", lambda: wbt.wetness_index(
        "sca.tif", "slope.tif", "twi.tif"))

    step("stream power index", lambda: wbt.stream_power_index(
        "sca.tif", "slope.tif", "spi.tif", exponent=1.0))

    step("ruggedness", lambda: wbt.ruggedness_index("dem_breached.tif", "ruggedness.tif"))

    step("extract streams", lambda: wbt.extract_streams(
        "flowacc_cells.tif", "streams.tif", threshold=STREAM_THRESHOLD_CELLS))

    step("distance to stream", lambda: wbt.downslope_distance_to_stream(
        "dem_breached.tif", "streams.tif", "dist_to_stream.tif"))

    log("WhiteboxTools chain complete")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--res", type=int, default=90,
                    help="target resolution in metres (default 90)")
    ap.add_argument("--skip-dem", action="store_true",
                    help="reuse an existing clipped DEM")
    args = ap.parse_args()

    out_dir = os.path.join(OUT_ROOT, f"{args.res}m")
    os.makedirs(out_dir, exist_ok=True)
    dem_path = os.path.join(out_dir, "dem.tif")

    geom = state_geometry()
    log(f"state bounds: {[round(v, 3) for v in geom.bounds]}")

    if args.skip_dem and os.path.exists(dem_path):
        log("reusing existing clipped DEM")
    else:
        tiles = fetch_dem_tiles(geom.bounds)
        build_clipped_dem(tiles, geom, args.res, dem_path)

    run_whitebox(dem_path, out_dir, args.res)

    log("outputs:")
    for f in sorted(os.listdir(out_dir)):
        if f.endswith(".tif"):
            mb = os.path.getsize(os.path.join(out_dir, f)) / 1e6
            log(f"  {f:28s} {mb:8.1f} MB")


if __name__ == "__main__":
    main()
