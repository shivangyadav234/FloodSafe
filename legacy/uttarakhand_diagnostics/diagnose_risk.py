import os

# ---------------------------------------------------------
# PROJ FIX
# ---------------------------------------------------------
PROJ_DATA = r"D:\miniconda\envs\Floodsafe\Library\share\proj"

os.environ["PROJ_DATA"] = PROJ_DATA
os.environ["PROJ_LIB"] = PROJ_DATA

import pyproj
pyproj.datadir.set_data_dir(PROJ_DATA)

# ---------------------------------------------------------
# IMPORTS
# ---------------------------------------------------------
import numpy as np
import geopandas as gpd
from scipy.sparse import load_npz


# ---------------------------------------------------------
# FILES
# ---------------------------------------------------------
GRAPH_FILE = r"D:\FloodSafe\data\routing\uttarakhand\roads.npz"
COORD_FILE = r"D:\FloodSafe\data\routing\uttarakhand\coordinates.npy"

RISK_FILE = (
    r"D:\FloodSafe\data\routing\uttarakhand"
    r"\road_flood_risk.npz"
)

HAZARD_FILE = (
    r"D:\FloodSafe\data\flood\uttarakhand"
    r"\uttarakhand_flash_flood_hazard_clean.geojson"
)


# ---------------------------------------------------------
# LOAD DATA
# ---------------------------------------------------------
print("\nLoading data...")

graph = load_npz(GRAPH_FILE)
coordinates = np.load(COORD_FILE)

risk_data = np.load(RISK_FILE)
risk_values = risk_data["risk"]

hazard = gpd.read_file(HAZARD_FILE)

print("\n========== BASIC DATA ==========")

print(f"Graph nodes : {graph.shape[0]:,}")
print(f"Graph edges : {graph.nnz:,}")
print(f"Coordinates : {len(coordinates):,}")
print(f"Risk values : {len(risk_values):,}")
print(f"Hazard polygons : {len(hazard):,}")

print("\nCoordinate CRS:")
print(hazard.crs)

# ---------------------------------------------------------
# COORDINATE RANGE
# ---------------------------------------------------------
print("\n========== ROAD COORDINATES ==========")

print(
    f"Road longitude : "
    f"{coordinates[:, 0].min():.6f} → "
    f"{coordinates[:, 0].max():.6f}"
)

print(
    f"Road latitude  : "
    f"{coordinates[:, 1].min():.6f} → "
    f"{coordinates[:, 1].max():.6f}"
)

# ---------------------------------------------------------
# HAZARD BBOX
# ---------------------------------------------------------
print("\n========== HAZARD BOUNDING BOX ==========")

minx, miny, maxx, maxy = hazard.total_bounds

print(f"Hazard longitude : {minx:.6f} → {maxx:.6f}")
print(f"Hazard latitude  : {miny:.6f} → {maxy:.6f}")

# ---------------------------------------------------------
# RISK DISTRIBUTION
# ---------------------------------------------------------
print("\n========== RISK DISTRIBUTION ==========")

unique, counts = np.unique(risk_values, return_counts=True)

for value, count in zip(unique, counts):
    print(
        f"Risk {value:4.1f} : "
        f"{count:,} edges"
    )

# ---------------------------------------------------------
# EXTREME EDGES
# ---------------------------------------------------------
print("\n========== EXTREME EDGE LOCATIONS ==========")

extreme_indices = np.where(risk_values == 8.0)[0]

print(f"Extreme edges: {len(extreme_indices):,}")

if len(extreme_indices) > 0:

    print("\nFirst 20 Extreme edges:")

    rows = []

    for edge_index in extreme_indices[:20]:

        source = np.searchsorted(
            graph.indptr,
            edge_index,
            side="right"
        ) - 1

        target = graph.indices[edge_index]

        lon1, lat1 = coordinates[source]
        lon2, lat2 = coordinates[target]

        rows.append(
            (
                edge_index,
                source,
                target,
                lon1,
                lat1,
                lon2,
                lat2
            )
        )

    print(
        "\n"
        "EDGE       SOURCE     TARGET       "
        "LON1        LAT1        LON2        LAT2"
    )

    for row in rows:
        print(
            f"{row[0]:8d} "
            f"{row[1]:9d} "
            f"{row[2]:9d} "
            f"{row[3]:10.6f} "
            f"{row[4]:10.6f} "
            f"{row[5]:10.6f} "
            f"{row[6]:10.6f}"
        )

# ---------------------------------------------------------
# TEST ROAD/Hazard OVERLAP
# ---------------------------------------------------------
print("\n========== ROAD / HAZARD OVERLAP TEST ==========")

# Make sure hazard is WGS84
if hazard.crs is not None:
    hazard_wgs84 = hazard.to_crs("EPSG:4326")
else:
    hazard_wgs84 = hazard.copy()

hazard_union = hazard_wgs84.geometry.union_all()

# Check Extreme edge midpoints
inside_count = 0
outside_count = 0

checked = min(len(extreme_indices), 5000)

for edge_index in extreme_indices[:checked]:

    source = np.searchsorted(
        graph.indptr,
        edge_index,
        side="right"
    ) - 1

    target = graph.indices[edge_index]

    lon1, lat1 = coordinates[source]
    lon2, lat2 = coordinates[target]

    midpoint_lon = (lon1 + lon2) / 2
    midpoint_lat = (lat1 + lat2) / 2

    from shapely.geometry import Point

    point = Point(midpoint_lon, midpoint_lat)

    if hazard_union.covers(point):
        inside_count += 1
    else:
        outside_count += 1

print(f"Extreme edges checked : {checked:,}")
print(f"Inside hazard         : {inside_count:,}")
print(f"Outside hazard        : {outside_count:,}")

# ---------------------------------------------------------
# FINAL DIAGNOSIS
# ---------------------------------------------------------
print("\n========== DIAGNOSIS ==========")

if inside_count > 0:
    print(
        "PASS: Extreme road edges overlap "
        "the hazard polygons."
    )

    print(
        "The flood-risk layer is spatially aligned "
        "with the road network."
    )

else:
    print(
        "WARNING: Extreme edges do NOT overlap "
        "the hazard polygons."
    )

    print(
        "This indicates a possible coordinate, "
        "CRS, or risk-assignment problem."
    )

print("\nDiagnostic complete.")