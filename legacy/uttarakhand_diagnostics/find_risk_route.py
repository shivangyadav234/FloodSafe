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
# IMPORT
# ---------------------------------------------------------
from .routing_engine import calculate_route, coordinates
import numpy as np


# ---------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------
RISK_VALUES = {
    1.0: "NORMAL",
    2.0: "LOW",
    4.0: "SIGNIFICANT",
    8.0: "EXTREME",
}

TEST_EDGES = 852


# ---------------------------------------------------------
# LOAD RISK
# ---------------------------------------------------------
RISK_FILE = (
    r"D:\FloodSafe\data\routing\uttarakhand"
    r"\road_flood_risk.npz"
)

risk_data = np.load(RISK_FILE)
risk = risk_data["risk"]


# ---------------------------------------------------------
# LOAD GRAPH
# ---------------------------------------------------------
GRAPH_FILE = (
    r"D:\FloodSafe\data\routing\uttarakhand"
    r"\roads.npz"
)

from scipy.sparse import load_npz

graph = load_npz(GRAPH_FILE)


# ---------------------------------------------------------
# FIND EXTREME EDGES
# ---------------------------------------------------------
extreme_edges = np.where(risk == 8.0)[0]

print("\n========================================")
print("FLOOD RISK ROUTE SEARCH")
print("========================================")

print(f"Extreme edges available: {len(extreme_edges)}")


# ---------------------------------------------------------
# TEST EXTREME LOCATIONS
# ---------------------------------------------------------
found = False

for count, edge_index in enumerate(extreme_edges[:TEST_EDGES], 1):

    # Find source node for CSR edge
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

    # -----------------------------------------------------
    # Use nodes on both sides of the Extreme road
    # -----------------------------------------------------
    start_node = source
    end_node = target

    start_lon, start_lat = coordinates[start_node]
    end_lon, end_lat = coordinates[end_node]

    print(
        f"\nTesting Extreme edge "
        f"{count}/{len(extreme_edges)}"
    )

    print(
        f"Location: "
        f"{start_lat:.6f}, {start_lon:.6f}"
    )

    try:

        fastest = calculate_route(
            start_lat=float(start_lat),
            start_lon=float(start_lon),
            end_lat=float(end_lat),
            end_lon=float(end_lon),
            mode="FASTEST"
        )

        safest = calculate_route(
            start_lat=float(start_lat),
            start_lon=float(start_lon),
            end_lat=float(end_lat),
            end_lon=float(end_lon),
            mode="SAFEST"
        )

        balanced = calculate_route(
            start_lat=float(start_lat),
            start_lon=float(start_lon),
            end_lat=float(end_lat),
            end_lon=float(end_lon),
            mode="BALANCED"
        )

        # -------------------------------------------------
        # Extract risk counts
        # -------------------------------------------------
        fr = fastest.get("risk_counts", {})
        sr = safest.get("risk_counts", {})
        br = balanced.get("risk_counts", {})

        extreme_fast = fr.get(8.0, 0)
        extreme_safe = sr.get(8.0, 0)
        extreme_bal = br.get(8.0, 0)

        print("\nRESULT")

        print(
            f"FASTEST  : "
            f"{len(fastest['path'])} nodes | "
            f"Extreme = {extreme_fast}"
        )

        print(
            f"SAFEST   : "
            f"{len(safest['path'])} nodes | "
            f"Extreme = {extreme_safe}"
        )

        print(
            f"BALANCED : "
            f"{len(balanced['path'])} nodes | "
            f"Extreme = {extreme_bal}"
        )

        # -------------------------------------------------
        # Check whether modes differ
        # -------------------------------------------------
        fast_path = tuple(fastest["path"])
        safe_path = tuple(safest["path"])
        bal_path = tuple(balanced["path"])

        if (
            fast_path != safe_path
            or fast_path != bal_path
            or safe_path != bal_path
        ):

            print("\n========================================")
            print("SUCCESS!")
            print("Different routing behaviour detected.")
            print("========================================")

            print(
                f"Test location:"
                f"\nLatitude  : {start_lat}"
                f"\nLongitude : {start_lon}"
            )

            print("\nFASTEST risk:", fr)
            print("SAFEST risk :", sr)
            print("BALANCED risk:", br)

            found = True
            break

    except Exception as e:

        print(f"ERROR: {e}")

# ---------------------------------------------------------
# FINAL
# ---------------------------------------------------------
if not found:

    print("\n========================================")
    print("NO DIFFERENT ROUTE FOUND")
    print("========================================")

    print(
        "The tested Extreme roads did not have "
        "a usable alternative route."
    )

print("\nTest complete.")