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
from scipy.sparse import load_npz

from .routing_engine import calculate_route, coordinates


# ---------------------------------------------------------
# FILES
# ---------------------------------------------------------
GRAPH_FILE = (
    r"D:\FloodSafe\data\routing\uttarakhand"
    r"\roads.npz"
)

RISK_FILE = (
    r"D:\FloodSafe\data\routing\uttarakhand"
    r"\road_flood_risk.npz"
)


# ---------------------------------------------------------
# LOAD
# ---------------------------------------------------------
print("\nLoading graph and risk data...")

graph = load_npz(GRAPH_FILE)
risk = np.load(RISK_FILE)["risk"]

print(f"Nodes : {graph.shape[0]:,}")
print(f"Edges : {graph.nnz:,}")


# ---------------------------------------------------------
# EXTREME EDGES
# ---------------------------------------------------------
extreme_edges = np.where(risk == 8.0)[0]

print(f"Extreme edges : {len(extreme_edges):,}")


# ---------------------------------------------------------
# GET EXTREME EDGE LOCATIONS
# ---------------------------------------------------------
print("\nFinding flood-risk regions...")

points = []

for edge_index in extreme_edges:

    source = (
        np.searchsorted(
            graph.indptr,
            edge_index,
            side="right"
        ) - 1
    )

    target = graph.indices[edge_index]

    lon1, lat1 = coordinates[source]
    lon2, lat2 = coordinates[target]

    points.append(
        [
            (lon1 + lon2) / 2,
            (lat1 + lat2) / 2
        ]
    )

points = np.array(points)


# ---------------------------------------------------------
# REGION STATISTICS
# ---------------------------------------------------------
print("\n========== EXTREME ROAD REGION ==========")

print(
    f"Longitude : "
    f"{points[:,0].min():.6f} → "
    f"{points[:,0].max():.6f}"
)

print(
    f"Latitude  : "
    f"{points[:,1].min():.6f} → "
    f"{points[:,1].max():.6f}"
)


# ---------------------------------------------------------
# USE DENSE EXTREME CLUSTER
# ---------------------------------------------------------
# The previous diagnostic showed the main Extreme
# corridor around ~79.49E, 30.78N.

target_lon = 79.4900
target_lat = 30.7790


# ---------------------------------------------------------
# FIND EXTREME EDGE NEAREST TO TARGET
# ---------------------------------------------------------
dist = (
    (points[:, 0] - target_lon) ** 2
    + (points[:, 1] - target_lat) ** 2
)

best_index = np.argmin(dist)

edge_index = extreme_edges[best_index]

source = (
    np.searchsorted(
        graph.indptr,
        edge_index,
        side="right"
    ) - 1
)

target = graph.indices[edge_index]


print("\n========== SELECTED EXTREME ROAD ==========")

print(f"Edge   : {edge_index}")
print(f"Source : {source}")
print(f"Target : {target}")

print(
    "Source coordinates:",
    coordinates[source]
)

print(
    "Target coordinates:",
    coordinates[target]
)


# ---------------------------------------------------------
# CREATE ENDPOINTS AROUND THE EXTREME ROAD
# ---------------------------------------------------------
#
# Instead of routing only source -> target, move
# approximately 2 km before and after the risk area.
#
# We use nearby graph nodes by geographic distance.
# ---------------------------------------------------------

mid_lon = (
    coordinates[source][0]
    + coordinates[target][0]
) / 2

mid_lat = (
    coordinates[source][1]
    + coordinates[target][1]
) / 2


# Find nodes approximately within a local box
# around the extreme corridor.
box = (
    (coordinates[:, 0] > mid_lon - 0.025)
    & (coordinates[:, 0] < mid_lon + 0.025)
    & (coordinates[:, 1] > mid_lat - 0.025)
    & (coordinates[:, 1] < mid_lat + 0.025)
)

local_nodes = np.where(box)[0]

print(
    f"\nLocal road nodes: {len(local_nodes):,}"
)


# ---------------------------------------------------------
# SELECT TWO FAR APART LOCAL NODES
# ---------------------------------------------------------
# Use longitude as a simple corridor direction.
# Pick nodes west and east of the Extreme area.
# ---------------------------------------------------------

west_candidates = local_nodes[
    coordinates[local_nodes, 0] < mid_lon - 0.008
]

east_candidates = local_nodes[
    coordinates[local_nodes, 0] > mid_lon + 0.008
]


if len(west_candidates) == 0 or len(east_candidates) == 0:

    print("\nCould not find suitable local endpoints.")

    print(
        "Try another Extreme region or expand "
        "the local search box."
    )

    raise SystemExit


# Choose closest candidates to desired offsets
west_target_lon = mid_lon - 0.015
east_target_lon = mid_lon + 0.015

west_node = west_candidates[
    np.argmin(
        np.abs(
            coordinates[west_candidates, 0]
            - west_target_lon
        )
    )
]

east_node = east_candidates[
    np.argmin(
        np.abs(
            coordinates[east_candidates, 0]
            - east_target_lon
        )
    )
]


start_lon, start_lat = coordinates[west_node]
end_lon, end_lat = coordinates[east_node]


print("\n========== TEST ROUTE ==========")

print(
    f"START : "
    f"{start_lat:.6f}, {start_lon:.6f}"
)

print(
    f"END   : "
    f"{end_lat:.6f}, {end_lon:.6f}"
)


# ---------------------------------------------------------
# RUN ONLY THREE ROUTES
# ---------------------------------------------------------
results = {}

for mode in ["FASTEST", "SAFEST", "BALANCED"]:

    print("\n" + "=" * 50)
    print(mode)
    print("=" * 50)

    try:

        result = calculate_route(
            start_lat=float(start_lat),
            start_lon=float(start_lon),
            end_lat=float(end_lat),
            end_lon=float(end_lon),
            mode=mode
        )

        results[mode] = result

        print(
            f"\n{mode} completed."
        )

    except Exception as e:

        print(
            f"{mode} ERROR: {e}"
        )


# ---------------------------------------------------------
# COMPARE
# ---------------------------------------------------------
print("\n\n========================================")
print("FINAL COMPARISON")
print("========================================")


for mode, result in results.items():

    path = result["path"]
    risk_counts = result.get("risk_counts", {})

    print(
        f"\n{mode}"
    )

    print(
        f"Nodes   : {len(path)}"
    )

    print(
        f"Normal  : {risk_counts.get(1.0, 0)}"
    )

    print(
        f"Low     : {risk_counts.get(2.0, 0)}"
    )

    print(
        f"Significant : "
        f"{risk_counts.get(4.0, 0)}"
    )

    print(
        f"Extreme : "
        f"{risk_counts.get(8.0, 0)}"
    )


# ---------------------------------------------------------
# PATH DIFFERENCE
# ---------------------------------------------------------
if len(results) == 3:

    fast = tuple(results["FASTEST"]["path"])
    safe = tuple(results["SAFEST"]["path"])
    balanced = tuple(results["BALANCED"]["path"])

    print("\n========================================")
    print("ROUTE DIFFERENCE")
    print("========================================")

    print(
        "FASTEST == SAFEST   :",
        fast == safe
    )

    print(
        "FASTEST == BALANCED :",
        fast == balanced
    )

    print(
        "SAFEST == BALANCED   :",
        safe == balanced
    )

    if fast != safe or fast != balanced:

        print(
            "\nSUCCESS: "
            "Flood risk is affecting route selection."
        )

    else:

        print(
            "\nAll three modes selected the same route."
        )
