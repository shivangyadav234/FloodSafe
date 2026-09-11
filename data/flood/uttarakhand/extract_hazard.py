import os

# ---------------------------------------------------------
# PROJ
# ---------------------------------------------------------
PROJ_DATA = r"D:\miniconda\envs\FloodSafe\Library\share\proj"
os.environ["PROJ_DATA"] = PROJ_DATA
os.environ["PROJ_LIB"] = PROJ_DATA

import pyproj
pyproj.datadir.set_data_dir(PROJ_DATA)

# ---------------------------------------------------------
# Imports
# ---------------------------------------------------------
import numpy as np
import rasterio
import geopandas as gpd
import matplotlib.pyplot as plt

from rasterio.features import shapes, sieve
from shapely.geometry import shape


# ---------------------------------------------------------
# Files
# ---------------------------------------------------------
INPUT_TIF = r"D:\FloodSafe\data\flood\uttarakhand\flash_flood_hazard_georef.tif"

OUTPUT_GEOJSON = (
    r"D:\FloodSafe\data\flood\uttarakhand"
    r"\uttarakhand_flash_flood_hazard.geojson"
)

OUTPUT_PREVIEW = (
    r"D:\FloodSafe\data\flood\uttarakhand"
    r"\hazard_preview_final.png"
)


# ---------------------------------------------------------
# Settings
# ---------------------------------------------------------
MIN_AREA_M2 = 100

# Map frame in the 3469 x 2296 TIFF
MAP_LEFT = 467
MAP_RIGHT = 2991
MAP_TOP = 456
MAP_BOTTOM = 2020


# ---------------------------------------------------------
# Read raster
# ---------------------------------------------------------
print("Reading TIFF...")

with rasterio.open(INPUT_TIF) as src:
    data = src.read([1, 2, 3])
    transform = src.transform
    crs = src.crs
    height = src.height
    width = src.width

rgb = np.transpose(data, (1, 2, 0)).astype(np.float32)

R = rgb[:, :, 0]
G = rgb[:, :, 1]
B = rgb[:, :, 2]

print(f"Raster: {width} x {height}")
print(f"CRS: {crs}")


# ---------------------------------------------------------
# Map-frame mask
# ---------------------------------------------------------
valid = np.zeros((height, width), dtype=bool)

valid[
    MAP_TOP:MAP_BOTTOM,
    MAP_LEFT:MAP_RIGHT
] = True


# ---------------------------------------------------------
# HSV conversion
# ---------------------------------------------------------
print("Converting RGB to HSV...")

mx = np.max(rgb, axis=2)
mn = np.min(rgb, axis=2)
diff = mx - mn

H = np.zeros_like(mx)

mask = diff != 0

# Red maximum
m = mask & (mx == R)
H[m] = (60 * ((G[m] - B[m]) / diff[m])) % 360

# Green maximum
m = mask & (mx == G)
H[m] = 60 * ((B[m] - R[m]) / diff[m]) + 120

# Blue maximum
m = mask & (mx == B)
H[m] = 60 * ((R[m] - G[m]) / diff[m]) + 240

S = np.zeros_like(mx)
S[mx != 0] = diff[mx != 0] / mx[mx != 0]

V = mx / 255.0


# ---------------------------------------------------------
# Hazard masks
# ---------------------------------------------------------

# LOW:
# Green composite colors.
#
# We require green to dominate red and blue.
LOW = (
    valid &
    (G > R * 1.15) &
    (G > B * 1.15) &
    (G > 100) &
    (S > 0.20) &
    (H >= 70) &
    (H <= 160)
)


# MODERATE:
# Yellow.
MODERATE = (
    valid &
    (R > 180) &
    (G > 170) &
    (B < 130) &
    (R > B * 1.5) &
    (G > B * 1.4) &
    (H >= 40) &
    (H <= 75) &
    (S > 0.30)
)


# SIGNIFICANT:
# Orange.
SIGNIFICANT = (
    valid &
    (R > 170) &
    (G >= 70) &
    (G < 190) &
    (B < 100) &
    (R > G * 1.15) &
    (R > B * 2.0) &
    (H >= 10) &
    (H < 40) &
    (S > 0.40)
)


# EXTREME:
# Red.
EXTREME = (
    valid &
    (R > 150) &
    (G < 120) &
    (B < 120) &
    (R > G * 1.5) &
    (R > B * 1.5) &
    ((H < 15) | (H > 345)) &
    (S > 0.40)
)


masks = {
    "LOW": LOW,
    "MODERATE": MODERATE,
    "SIGNIFICANT": SIGNIFICANT,
    "EXTREME": EXTREME,
}


# ---------------------------------------------------------
# Print pixel counts
# ---------------------------------------------------------
print("\nPIXEL COUNTS")

for name, mask in masks.items():
    print(f"{name:12s}: {int(mask.sum()):,}")


# ---------------------------------------------------------
# Remove small raster noise
# ---------------------------------------------------------
print("\nRemoving tiny raster components...")

for name in masks:
    masks[name] = sieve(
        masks[name].astype(np.uint8),
        size=8,
        connectivity=8
    ).astype(bool)


# ---------------------------------------------------------
# Polygonize
# ---------------------------------------------------------
print("\nPolygonizing...")

records = []

for name, mask in masks.items():

    count = 0

    for geom, value in shapes(
        mask.astype(np.uint8),
        mask=mask,
        transform=transform
    ):

        if value != 1:
            continue

        poly = shape(geom)

        if poly.is_empty:
            continue

        records.append({
            "hazard": name,
            "geometry": poly
        })

        count += 1

    print(f"{name:12s}: {count} raw polygons")


# ---------------------------------------------------------
# GeoDataFrame
# ---------------------------------------------------------
gdf = gpd.GeoDataFrame(
    records,
    geometry="geometry",
    crs=crs
)


# ---------------------------------------------------------
# Area
# ---------------------------------------------------------
print("\nCalculating areas...")

metric = gdf.to_crs("EPSG:32644")

gdf["area_m2"] = metric.geometry.area
gdf["area_ha"] = gdf["area_m2"] / 10000


# ---------------------------------------------------------
# Minimum area filter
# ---------------------------------------------------------
before = len(gdf)

gdf = gdf[gdf["area_m2"] >= MIN_AREA_M2].copy()

print(
    f"Removed {before - len(gdf)} polygons below "
    f"{MIN_AREA_M2} m²"
)

print(f"Remaining polygons: {len(gdf)}")


# ---------------------------------------------------------
# Fix geometry
# ---------------------------------------------------------
gdf["geometry"] = gdf.geometry.buffer(0)

gdf = gdf[
    gdf.geometry.notna() &
    ~gdf.geometry.is_empty
].copy()


# ---------------------------------------------------------
# Save
# ---------------------------------------------------------
gdf[
    ["hazard", "area_m2", "area_ha", "geometry"]
].to_file(
    OUTPUT_GEOJSON,
    driver="GeoJSON"
)


# ---------------------------------------------------------
# Summary
# ---------------------------------------------------------
print("\n========== SUMMARY ==========")

print(gdf["hazard"].value_counts())

print("\nAREA BY HAZARD:")

print(
    gdf.groupby("hazard")["area_m2"].sum()
)

print(
    f"\nTOTAL AREA: "
    f"{gdf['area_m2'].sum():,.2f} m²"
)

print(
    f"TOTAL AREA: "
    f"{gdf['area_ha'].sum():,.2f} ha"
)


# ---------------------------------------------------------
# Preview
# ---------------------------------------------------------
print("\nCreating preview...")

fig, ax = plt.subplots(figsize=(14, 10))

gdf.plot(
    ax=ax,
    column="hazard",
    legend=True,
    edgecolor="black",
    linewidth=0.3,
    alpha=0.75
)

ax.set_title(
    "Uttarakhand Flash Flood Hazard — Extracted Zones"
)

ax.set_xlabel("Longitude")
ax.set_ylabel("Latitude")

plt.savefig(
    OUTPUT_PREVIEW,
    dpi=200,
    bbox_inches="tight"
)

plt.close()

print("\nGeoJSON:")
print(OUTPUT_GEOJSON)

print("\nPreview:")
print(OUTPUT_PREVIEW)

print("\n========== DONE ==========")