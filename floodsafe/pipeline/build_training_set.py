"""
Build the training table for the flash-flood susceptibility model.

Samples every raster predictor from build_terrain.py and build_soil.py
at points drawn inside the labelled hazard-atlas polygons, joins the
HydroBASINS catchment attributes, and writes one CSV.

Labelling
---------
The atlas gives four classes but the counts are badly skewed:
LOW 119 polygons, EXTREME 65, MODERATE 5, SIGNIFICANT 2. Two polygons
cannot support a class in a real model, so the target is collapsed to a
binary one:

    1 = elevated hazard (EXTREME / SIGNIFICANT / MODERATE)
    0 = low hazard      (LOW)

Unlike most susceptibility mapping, the negatives here are genuine
surveyed LOW polygons rather than sampled pseudo-absences.

Spatial autocorrelation
-----------------------
Points inside one polygon are near-duplicates of each other. A random
train/test split would leak them across the split and report a wildly
optimistic score, so every row carries its source polygon id in
`group_id`; train_model.py splits on that with GroupKFold.

Per-polygon sampling is capped because the LOW polygons cover ~531,000
ha against EXTREME's ~2,000 ha -- area-proportional sampling would let a
handful of huge LOW polygons dominate the fit.

Usage:
    python build_training_set.py --res 90
"""

import argparse
import os
import json

import _proj  # noqa: F401  -- must precede pyproj/geopandas imports

import numpy as np
import pandas as pd
import rasterio
import geopandas as gpd
from shapely.geometry import shape, Point


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LEGACY_DATA = os.path.join(REPO_ROOT, "data", "flood", "uttarakhand", "data")
HAZARD_FILE = os.path.join(LEGACY_DATA, "uttarakhand_flash_flood_hazard_clean.geojson")
WATERSHED_FILE = os.path.join(LEGACY_DATA, "uttarakhand_watersheds.geojson")

OUT_ROOT = os.path.join(REPO_ROOT, "floodsafe", "data", "terrain")
TABLE_ROOT = os.path.join(REPO_ROOT, "floodsafe", "data", "tables")

TARGET_CRS = "EPSG:32644"
ELEVATED_CLASSES = {"EXTREME", "SIGNIFICANT", "MODERATE"}

# Raster predictors, as {column: filename}. Anything missing on disk is
# skipped with a warning rather than failing the whole build.
RASTER_FEATURES = {
    "elevation_m": "dem.tif",
    "slope_deg": "slope.tif",
    "plan_curv": "plan_curv.tif",
    "prof_curv": "prof_curv.tif",
    "twi": "twi.tif",
    "spi": "spi.tif",
    "sca": "sca.tif",
    "flowacc_cells": "flowacc_cells.tif",
    "dist_to_stream_m": "dist_to_stream.tif",
    "ruggedness": "ruggedness.tif",
    # Height Above Nearest Drainage plus multi-scale relief -- the
    # standard flood-susceptibility predictors the first pass lacked.
    "hand_m": "hand.tif",
    "dev_elev_small": "dev_elev_small.tif",
    "dev_elev_large": "dev_elev_large.tif",
    "elev_percentile": "elev_percentile.tif",
    "rel_topo_position": "rel_topo_position.tif",
    "soil_sand_pct": "soil_sand.tif",
    "soil_clay_pct": "soil_clay.tif",
    "soil_silt_pct": "soil_silt.tif",
    "soil_hsg": "soil_hsg.tif",
}

MAX_POINTS_PER_POLYGON = 250
MIN_POINTS_PER_POLYGON = 3


def log(msg):
    print(f"[trainset] {msg}", flush=True)


def load_hazard_polygons():
    with open(HAZARD_FILE, "r", encoding="utf-8") as f:
        gj = json.load(f)

    rows = []
    for i, feat in enumerate(gj["features"]):
        hazard = feat["properties"]["hazard"]
        rows.append({
            "group_id": i,
            "hazard_class": hazard,
            "label": 1 if hazard in ELEVATED_CLASSES else 0,
            "geometry": shape(feat["geometry"]),
        })

    gdf = gpd.GeoDataFrame(rows, crs="EPSG:4326")
    log(f"hazard polygons: {len(gdf)}")
    log(f"  class counts: {gdf['hazard_class'].value_counts().to_dict()}")
    log(f"  binary labels: {gdf['label'].value_counts().to_dict()}")
    return gdf


def sample_points_in_polygon(geom, res_m, rng):
    """Regular grid of candidate points at raster spacing, clipped to the
    polygon, then capped. Falls back to the centroid / representative
    point for polygons smaller than one cell."""

    minx, miny, maxx, maxy = geom.bounds
    step = res_m

    xs = np.arange(minx + step / 2, maxx, step)
    ys = np.arange(miny + step / 2, maxy, step)

    pts = []
    if xs.size and ys.size:
        gx, gy = np.meshgrid(xs, ys)
        candidates = np.column_stack([gx.ravel(), gy.ravel()])
        # Cap before the (expensive) contains test when a polygon is huge.
        if len(candidates) > 20000:
            idx = rng.choice(len(candidates), 20000, replace=False)
            candidates = candidates[idx]
        pts = [(x, y) for x, y in candidates if geom.contains(Point(x, y))]

    if len(pts) < MIN_POINTS_PER_POLYGON:
        rp = geom.representative_point()
        pts = list({(rp.x, rp.y), *pts})

    if len(pts) > MAX_POINTS_PER_POLYGON:
        idx = rng.choice(len(pts), MAX_POINTS_PER_POLYGON, replace=False)
        pts = [pts[i] for i in idx]

    return pts


def build_sample_frame(hazard_gdf, res_m, seed=42):
    rng = np.random.default_rng(seed)
    utm = hazard_gdf.to_crs(TARGET_CRS)

    records = []
    for row in utm.itertuples():
        for x, y in sample_points_in_polygon(row.geometry, res_m, rng):
            records.append({
                "group_id": row.group_id,
                "hazard_class": row.hazard_class,
                "label": row.label,
                "x": x,
                "y": y,
            })

    df = pd.DataFrame(records)
    log(f"sampled points: {len(df):,}")
    log(f"  by label: {df['label'].value_counts().to_dict()}")
    log(f"  by class: {df['hazard_class'].value_counts().to_dict()}")
    return df


def sample_rasters(df, out_dir):
    """Read each predictor and index it at the sample points.

    Every raster in the stack shares one grid, so row/col are computed
    once from the affine transform and reused. Done by hand rather than
    with src.sample(), which segfaults the interpreter in this
    environment (same rasterio 1.4.4 / numpy 2.4 issue as
    rasterio.windows.from_bounds; see build_soil.window_for_bounds).
    """

    reference = os.path.join(out_dir, "dem.tif")
    with rasterio.open(reference) as src:
        transform, height, width = src.transform, src.height, src.width

    # Inverse affine: grid is north-up and unrotated, so this is exact.
    cols = ((df["x"].to_numpy() - transform.c) / transform.a).astype("int64")
    rows = ((df["y"].to_numpy() - transform.f) / transform.e).astype("int64")

    inside = (rows >= 0) & (rows < height) & (cols >= 0) & (cols < width)
    log(f"  {inside.sum():,} of {len(df):,} points fall inside the raster grid")

    safe_rows = np.clip(rows, 0, height - 1)
    safe_cols = np.clip(cols, 0, width - 1)

    for col, fname in RASTER_FEATURES.items():
        path = os.path.join(out_dir, fname)
        if not os.path.exists(path):
            log(f"  WARNING: missing {fname}, skipping {col}")
            continue

        with rasterio.open(path) as src:
            band = src.read(1)
            nodata = src.nodata

        vals = band[safe_rows, safe_cols].astype("float64")
        vals[~inside] = np.nan

        if nodata is not None:
            vals = np.where(vals == nodata, np.nan, vals)
        # WhiteboxTools writes its own nodata sentinel on some outputs.
        vals = np.where(vals < -1e30, np.nan, vals)

        df[col] = vals
        pct = 100 * np.isfinite(vals).mean()
        lo = np.nanmin(vals) if pct else float("nan")
        hi = np.nanmax(vals) if pct else float("nan")
        log(f"  {col:20s} {pct:5.1f}% populated  [{lo:.2f} .. {hi:.2f}]")

    return df


def join_watersheds(df):
    if not os.path.exists(WATERSHED_FILE):
        log("  WARNING: watershed file missing, skipping catchment features")
        return df

    basins = gpd.read_file(WATERSHED_FILE).to_crs(TARGET_CRS)
    pts = gpd.GeoDataFrame(
        df[["group_id"]].copy(),
        geometry=gpd.points_from_xy(df["x"], df["y"]),
        crs=TARGET_CRS,
    )

    joined = gpd.sjoin(pts, basins[["UP_AREA", "SUB_AREA", "geometry"]],
                       how="left", predicate="within")
    joined = joined[~joined.index.duplicated(keep="first")]

    df["catchment_up_km2"] = joined["UP_AREA"].to_numpy()
    df["catchment_sub_km2"] = joined["SUB_AREA"].to_numpy()

    pct = 100 * df["catchment_up_km2"].notna().mean()
    log(f"  catchment features {pct:5.1f}% populated")
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--res", type=int, default=90)
    args = ap.parse_args()

    out_dir = os.path.join(OUT_ROOT, f"{args.res}m")
    if not os.path.isdir(out_dir):
        raise SystemExit(f"terrain outputs missing: {out_dir}\nRun build_terrain.py first.")

    hazard_gdf = load_hazard_polygons()
    df = build_sample_frame(hazard_gdf, args.res)

    log("sampling rasters ...")
    df = sample_rasters(df, out_dir)

    log("joining watersheds ...")
    df = join_watersheds(df)

    feature_cols = [c for c in df.columns
                    if c not in ("group_id", "hazard_class", "label", "x", "y")]

    before = len(df)
    df = df.dropna(subset=["elevation_m", "slope_deg", "twi"])
    log(f"dropped {before - len(df):,} rows with no terrain data "
        f"(outside DEM coverage); {len(df):,} remain")

    log(f"final label balance: {df['label'].value_counts().to_dict()}")
    log(f"polygons represented: {df['group_id'].nunique()}")

    os.makedirs(TABLE_ROOT, exist_ok=True)
    out_csv = os.path.join(TABLE_ROOT, f"training_{args.res}m.csv")
    df.to_csv(out_csv, index=False)
    log(f"wrote {out_csv}  ({len(df):,} rows x {len(feature_cols)} features)")
    log(f"features: {feature_cols}")


if __name__ == "__main__":
    main()
