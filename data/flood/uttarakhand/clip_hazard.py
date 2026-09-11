import os

PROJ_DATA = r"D:\miniconda\envs\FloodSafe\Library\share\proj"
os.environ["PROJ_DATA"] = PROJ_DATA
os.environ["PROJ_LIB"] = PROJ_DATA

import pyproj
pyproj.datadir.set_data_dir(PROJ_DATA)

import geopandas as gpd


HAZARD = r"D:\FloodSafe\data\flood\uttarakhand\uttarakhand_flash_flood_hazard.geojson"
BOUNDARY = r"D:\FloodSafe\data\flood\uttarakhand\uttarakhand_boundary.geojson"

OUTPUT = r"D:\FloodSafe\data\flood\uttarakhand\uttarakhand_flash_flood_hazard_clean.geojson"


print("Reading hazard polygons...")
hazard = gpd.read_file(HAZARD)

print("Reading Uttarakhand boundary...")
boundary = gpd.read_file(BOUNDARY)

# Select state-level boundary
state = boundary[
    boundary["admin_level"].astype(str) == "4"
].copy()

# Keep only the Uttarakhand state
state = state[state["name"] == "Uttarakhand"].copy()

print("Hazard polygons before clipping:", len(hazard))
print("State boundary:", len(state))


# Make sure CRS matches
state = state.to_crs(hazard.crs)


print("\nClipping hazard polygons to Uttarakhand...")

clean = gpd.clip(hazard, state)


# Remove empty geometries
clean = clean[
    clean.geometry.notna() &
    ~clean.geometry.is_empty
].copy()


# Recalculate areas
metric = clean.to_crs("EPSG:32644")

clean["area_m2"] = metric.geometry.area
clean["area_ha"] = clean["area_m2"] / 10000


# Remove anything below 100 m²
clean = clean[clean["area_m2"] >= 100].copy()


# Fix geometries
clean["geometry"] = clean.geometry.buffer(0)

clean = clean[
    clean.geometry.notna() &
    ~clean.geometry.is_empty
].copy()


# Save
clean.to_file(
    OUTPUT,
    driver="GeoJSON"
)


print("\n========== RESULT ==========")

print("Polygons before:", len(hazard))
print("Polygons after:", len(clean))

print("\nHazard classes:")
print(clean["hazard"].value_counts())

print("\nArea by hazard:")
print(
    clean.groupby("hazard")["area_m2"].sum()
)

print(
    "\nTOTAL AREA:",
    f"{clean['area_m2'].sum():,.2f} m²"
)

print(
    "TOTAL AREA:",
    f"{clean['area_ha'].sum():,.2f} ha"
)

print("\nSaved:")
print(OUTPUT)