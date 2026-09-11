import os

# =========================================================
# PROJ FIX
# =========================================================
PROJ_DATA = r"D:\miniconda\envs\FloodSafe\Library\share\proj"

os.environ["PROJ_DATA"] = PROJ_DATA
os.environ["PROJ_LIB"] = PROJ_DATA

import pyproj
pyproj.datadir.set_data_dir(PROJ_DATA)

# =========================================================
# IMPORTS
# =========================================================
import numpy as np
import geopandas as gpd
from shapely.geometry import LineString


# =========================================================
# FILES
# =========================================================
BASE_DIR = r"D:\FloodSafe"

GRAPH_FILE = os.path.join(
    BASE_DIR,
    r"data\routing\uttarakhand\roads.npz"
)

COORD_FILE = os.path.join(
    BASE_DIR,
    r"data\routing\uttarakhand\coordinates.npy"
)

RISK_FILE = os.path.join(
    BASE_DIR,
    r"data\routing\uttarakhand\road_flood_risk.npz"
)

OUTPUT_FILE = os.path.join(
    BASE_DIR,
    r"data\flood\uttarakhand\uttarakhand_road_hazard_light.geojson"
)


# =========================================================
# HAZARD CLASSES
# =========================================================
HAZARD_NAME = {
    0: "NORMAL",
    1: "LOW",
    2: "MODERATE",
    3: "SIGNIFICANT",
    4: "EXTREME"
}


# =========================================================
# ONLY EXPORT THESE CLASSES
# =========================================================
EXPORT_CLASSES = {
    2,   # MODERATE
    3,   # SIGNIFICANT
    4    # EXTREME
}


# =========================================================
# LOAD GRAPH
# =========================================================
print("Loading road graph...")

graph = np.load(GRAPH_FILE)

indices = graph["indices"]
indptr = graph["indptr"]

coordinates = np.load(COORD_FILE)

risk_graph = np.load(RISK_FILE)

risk = risk_graph["risk"]
hazard_class = risk_graph["hazard_class"]

print("Nodes :", len(coordinates))
print("Edges :", len(indices))


# =========================================================
# VERIFY
# =========================================================
print("\nVerifying arrays...")

if len(indices) != len(risk):
    raise ValueError("Edge count and risk count do not match!")

if len(indices) != len(hazard_class):
    raise ValueError(
        "Edge count and hazard_class count do not match!"
    )

print("Arrays aligned correctly.")


# =========================================================
# COUNT SELECTED EDGES
# =========================================================
selected_mask = np.isin(
    hazard_class,
    list(EXPORT_CLASSES)
)

selected_count = int(
    np.count_nonzero(selected_mask)
)

print(
    "\nSelected hazardous road segments:",
    f"{selected_count:,}"
)


# =========================================================
# CREATE FEATURES
# =========================================================
print("\nCreating lightweight road layer...")

features = []

processed = 0

for source in range(len(indptr) - 1):

    start = indptr[source]
    end = indptr[source + 1]

    if start == end:
        continue

    # Source coordinates
    lon1 = coordinates[source, 0]
    lat1 = coordinates[source, 1]

    for edge_pos in range(start, end):

        code = int(hazard_class[edge_pos])

        # -----------------------------------------------
        # Only Moderate / Significant / Extreme
        # -----------------------------------------------
        if code not in EXPORT_CLASSES:
            continue

        target = indices[edge_pos]

        lon2 = coordinates[target, 0]
        lat2 = coordinates[target, 1]

        line = LineString([
            (lon1, lat1),
            (lon2, lat2)
        ])

        features.append({
            "geometry": line,
            "hazard": HAZARD_NAME[code],
            "hazard_code": code,
            "risk": float(risk[edge_pos])
        })

        processed += 1

    if source % 100000 == 0:

        print(
            f"Processed nodes: "
            f"{source:,} / "
            f"{len(indptr)-1:,} | "
            f"Selected roads: "
            f"{processed:,}"
        )


# =========================================================
# GEODATAFRAME
# =========================================================
print("\nCreating GeoDataFrame...")

roads = gpd.GeoDataFrame(
    features,
    crs="EPSG:4326"
)


# =========================================================
# SORT BY SEVERITY
# =========================================================
hazard_order = {
    "MODERATE": 2,
    "SIGNIFICANT": 3,
    "EXTREME": 4
}

roads["_sort"] = roads["hazard"].map(
    hazard_order
)

roads = (
    roads
    .sort_values("_sort")
    .drop(columns="_sort")
    .reset_index(drop=True)
)


# =========================================================
# SAVE
# =========================================================
print("\nSaving lightweight GeoJSON...")

roads.to_file(
    OUTPUT_FILE,
    driver="GeoJSON"
)


# =========================================================
# SUMMARY
# =========================================================
print("\n========================================")
print(" LIGHTWEIGHT ROAD HAZARD SUMMARY")
print("========================================")

summary = (
    roads["hazard"]
    .value_counts()
    .reindex(
        [
            "MODERATE",
            "SIGNIFICANT",
            "EXTREME"
        ],
        fill_value=0
    )
)

for name, count in summary.items():

    print(
        f"{name:12s}: "
        f"{count:,} road segments"
    )

print("----------------------------------------")

print(
    f"TOTAL        : "
    f"{len(roads):,} road segments"
)

print("\nOutput CRS:", roads.crs)

print("\nSaved:")
print(OUTPUT_FILE)

print("\n========================================")
print("              DONE")
print("========================================")