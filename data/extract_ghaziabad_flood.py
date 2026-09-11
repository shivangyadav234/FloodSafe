import geopandas as gpd
from shapely.geometry import box
import os

INPUT_FILE = (
    "data/flood/real/"
    "NDEM_All_India_Flood_Innundation_1998_to_2022.geojson"
)

OUTPUT_FILE = (
    "data/flood/real/"
    "ncr_flood_1998_2022.geojson"
)

# Larger NCR region
NCR_BBOX = box(
    76.80,
    28.20,
    77.80,
    29.20
)

print("Loading India flood dataset...")
print("Please wait...")

flood = gpd.read_file(INPUT_FILE)

print()
print("Total flood features:", len(flood))
print("CRS:", flood.crs)

print()
print("Searching larger NCR region...")

flood = flood.to_crs("EPSG:4326")

ncr_flood = flood[
    flood.geometry.intersects(NCR_BBOX)
].copy()

print()
print("NCR flood features:", len(ncr_flood))

if len(ncr_flood) == 0:

    print()
    print("NO HISTORICAL FLOOD POLYGONS FOUND")
    print("in the NCR bounding box.")

else:

    os.makedirs(
        os.path.dirname(OUTPUT_FILE),
        exist_ok=True
    )

    ncr_flood.to_file(
        OUTPUT_FILE,
        driver="GeoJSON"
    )

    print()
    print("SUCCESS!")
    print("Saved:", OUTPUT_FILE)