import rasterio
import numpy as np
import geopandas as gpd

from rasterio.features import shapes
from shapely.geometry import shape


INPUT = r"data\flood\real\ghaziabad_hazard_map.tif"

OUTPUT = r"data\flood\real\ghaziabad_hazard.geojson"


print("Loading georeferenced hazard map...")

with rasterio.open(INPUT) as src:

    rgb = np.moveaxis(
        src.read(),
        0,
        -1
    )

    transform = src.transform
    crs = src.crs


# ---------------------------------------------------------
# HAZARD COLORS
# ---------------------------------------------------------

VERY_LOW = np.array([250, 230, 170])
LOW = np.array([250, 210, 120])

TOLERANCE = 35


def color_distance(image, target):

    return np.sqrt(
        np.sum(
            (image.astype(float) - target) ** 2,
            axis=2
        )
    )


very_low_mask = (
    color_distance(rgb, VERY_LOW)
    < TOLERANCE
)

low_mask = (
    color_distance(rgb, LOW)
    < TOLERANCE
)


# ---------------------------------------------------------
# POLYGONIZE
# ---------------------------------------------------------

features = []


def polygonize_mask(mask, hazard):

    results = shapes(
        mask.astype(np.uint8),
        mask=mask,
        transform=transform
    )

    count = 0

    for geom, value in results:

        if value != 1:
            continue

        polygon = shape(geom)

        if polygon.is_empty:
            continue

        if not polygon.is_valid:
            polygon = polygon.buffer(0)

        features.append({
            "geometry": polygon,
            "hazard": hazard
        })

        count += 1

    return count


print()
print("Converting Very Low areas...")

very_low_count = polygonize_mask(
    very_low_mask,
    "VERY_LOW"
)

print(
    "Very Low polygons:",
    very_low_count
)


print()
print("Converting Low areas...")

low_count = polygonize_mask(
    low_mask,
    "LOW"
)

print(
    "Low polygons:",
    low_count
)


# ---------------------------------------------------------
# CREATE GEODATAFRAME
# ---------------------------------------------------------

gdf = gpd.GeoDataFrame(
    features,
    crs=crs
)


# ---------------------------------------------------------
# REMOVE TINY PIXEL NOISE
# ---------------------------------------------------------

print()
print("Removing tiny polygons...")

# Project to meters for area calculation
gdf_meter = gdf.to_crs("EPSG:32644")

gdf_meter["area_m2"] = (
    gdf_meter.geometry.area
)

# Keep polygons >= 100 m²
gdf_meter = gdf_meter[
    gdf_meter["area_m2"] >= 100
].copy()


# Convert hectares
gdf_meter["area_ha"] = (
    gdf_meter["area_m2"] / 10000
)


# Back to WGS84
gdf = gdf_meter.to_crs("EPSG:4326")


# ---------------------------------------------------------
# SAVE
# ---------------------------------------------------------

gdf.to_file(
    OUTPUT,
    driver="GeoJSON"
)


print()
print("================================")
print("SUCCESS!")
print("================================")

print(
    "Total polygons:",
    len(gdf)
)

print()
print("Area by hazard class:")

print(
    gdf.groupby("hazard")["area_ha"]
    .sum()
)

print()
print("Total extracted area:",
      round(gdf["area_ha"].sum(), 2),
      "hectares"
)

print()
print("Saved:")
print(OUTPUT)