import os

PROJ_DATA = r"D:\miniconda\envs\FloodSafe\Library\share\proj"
os.environ["PROJ_DATA"] = PROJ_DATA
os.environ["PROJ_LIB"] = PROJ_DATA

import pyproj
pyproj.datadir.set_data_dir(PROJ_DATA)

from pyrosm import OSM

PBF = r"D:\FloodSafe\data\osm\uttarakhand\uttarakhand.osm.pbf"
OUTPUT = r"D:\FloodSafe\data\flood\uttarakhand\uttarakhand_boundary.geojson"

print("Reading Uttarakhand OSM...")

osm = OSM(PBF)

print("Extracting administrative boundaries...")

boundaries = osm.get_boundaries()

print("Columns:")
print(boundaries.columns.tolist())

print("\nBoundary count:", len(boundaries))

if "admin_level" in boundaries.columns:
    print("\nAdmin levels:")
    print(boundaries["admin_level"].value_counts(dropna=False))

boundaries.to_file(
    OUTPUT,
    driver="GeoJSON"
)

print("\nSaved:")
print(OUTPUT)