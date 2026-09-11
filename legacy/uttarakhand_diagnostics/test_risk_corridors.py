import os

# ---------------------------------------------------------
# PROJ FIX
# ---------------------------------------------------------
PROJ_DATA = r"D:\miniconda\envs\Floodsafe\Library\share\proj"

os.environ["PROJ_DATA"] = PROJ_DATA
os.environ["PROJ_LIB"] = PROJ_DATA

import pyproj
pyproj.datadir.set_data_dir(PROJ_DATA)

import numpy as np
from scipy.spatial import cKDTree

from .routing_engine import calculate_route, coordinates


# ---------------------------------------------------------
# FLOOD-RISK CORRIDOR CENTERS
# ---------------------------------------------------------
CORRIDORS = [
    ("Cluster 1", 80.036539, 29.731447),
    ("Cluster 2", 79.841864, 29.794282),
    ("Cluster 3", 78.603023, 30.808719),
    ("Cluster 4", 79.490375, 30.780347),
    ("Cluster 5", 79.097625, 30.322222),
    ("Cluster 6", 80.883724, 30.192155),
    ("Cluster 7", 79.814249, 30.827953),
]


# ---------------------------------------------------------
# SPATIAL INDEX
# ---------------------------------------------------------
print("\nBuilding road spatial index...")

tree = cKDTree(coordinates)

print("Spatial index ready.")


# ---------------------------------------------------------
# FIND ROAD NODES AROUND A CORRIDOR
# ---------------------------------------------------------
def get_local_nodes(lon, lat, radius_km=5):

    # Approximate degree radius
    lat_radius = radius_km / 111.0

    lon_radius = radius_km / (
        111.0 * max(np.cos(np.radians(lat)), 0.1)
    )

    idx = np.where(
        (coordinates[:, 0] >= lon - lon_radius) &
        (coordinates[:, 0] <= lon + lon_radius) &
        (coordinates[:, 1] >= lat - lat_radius) &
        (coordinates[:, 1] <= lat + lat_radius)
    )[0]

    return idx


# ---------------------------------------------------------
# CHOOSE TWO ROAD NODES FAR APART
# ---------------------------------------------------------
def choose_endpoints(nodes):

    if len(nodes) < 2:
        return None

    pts = coordinates[nodes]

    # First extreme point
    start_local = np.argmin(pts[:, 0])
    start = nodes[start_local]

    # Point farthest from first point
    d = np.sum(
        (pts - pts[start_local]) ** 2,
        axis=1
    )

    end_local = np.argmax(d)
    end = nodes[end_local]

    # Second pass for better diameter estimate
    d2 = np.sum(
        (pts - pts[end_local]) ** 2,
        axis=1
    )

    start_local = np.argmax(d2)
    start = nodes[start_local]

    return int(start), int(end)


# ---------------------------------------------------------
# ROUTE TEST
# ---------------------------------------------------------
def test_route(start_node, end_node):

    start_lon, start_lat = coordinates[start_node]
    end_lon, end_lat = coordinates[end_node]

    print("\nSTART")
    print(
        f"  {start_lat:.6f}, "
        f"{start_lon:.6f}"
    )

    print("END")
    print(
        f"  {end_lat:.6f}, "
        f"{end_lon:.6f}"
    )

    results = {}

    for mode in [
        "FASTEST",
        "SAFEST",
        "BALANCED"
    ]:

        print(f"\nRunning {mode}...")

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
                f"Distance: "
                f"{result.get('distance_km', 0):.2f} km"
            )

            print(
                "Risk:",
                result.get("risk_counts", {})
            )

        except Exception as e:

            print(
                f"{mode} failed:",
                e
            )

            results[mode] = None


    return results


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------
print("\n========================================")
print("TESTING FLOOD-RISK CORRIDORS")
print("========================================")


for corridor_name, lon, lat in CORRIDORS:

    print("\n")
    print("=" * 60)
    print(corridor_name)
    print("=" * 60)

    print(
        f"Flood corridor center:"
        f" {lat:.6f}, {lon:.6f}"
    )


    # -----------------------------------------------------
    # Try 5 km first
    # -----------------------------------------------------
    nodes = get_local_nodes(
        lon,
        lat,
        radius_km=5
    )

    print(
        f"Road nodes within 5 km: "
        f"{len(nodes):,}"
    )


    if len(nodes) < 2:

        print("Not enough road nodes.")
        continue


    endpoints = choose_endpoints(nodes)

    if endpoints is None:

        print("Could not choose endpoints.")
        continue


    start_node, end_node = endpoints


    results = test_route(
        start_node,
        end_node
    )


    # -----------------------------------------------------
    # Compare routes
    # -----------------------------------------------------
    valid = [
        r for r in results.values()
        if r is not None
    ]

    if len(valid) < 2:
        continue


    paths = {}

    for mode, result in results.items():

        if result is not None:

            paths[mode] = tuple(
                result["path"]
            )


    unique_paths = set(
        paths.values()
    )


    print("\n----------------------------------------")
    print("ROUTE COMPARISON")
    print("----------------------------------------")

    print(
        f"Different paths: "
        f"{len(unique_paths)}"
    )


    if len(unique_paths) > 1:

        print("\n")
        print("########################################")
        print("SUCCESS - ROUTE DIFFERENCE FOUND")
        print("########################################")

        print(
            "\nCorridor:",
            corridor_name
        )

        print(
            "\nUse these coordinates in the app:"
        )

        print(
            f"START LAT = {coordinates[start_node][1]}"
        )

        print(
            f"START LON = {coordinates[start_node][0]}"
        )

        print(
            f"END LAT = {coordinates[end_node][1]}"
        )

        print(
            f"END LON = {coordinates[end_node][0]}"
        )

        print("\nRisk comparison:")

        for mode, result in results.items():

            if result:

                print(
                    mode,
                    ":",
                    result.get(
                        "risk_counts",
                        {}
                    )
                )

        break

    else:

        print(
            "All three modes selected the same path."
        )


else:

    print("\n")
    print("=" * 60)
    print("NO ROUTE DIFFERENCE FOUND")
    print("=" * 60)

    print(
        "\nThe real Uttarakhand road/hazard data did not "
        "produce different paths for these corridor tests."
    )