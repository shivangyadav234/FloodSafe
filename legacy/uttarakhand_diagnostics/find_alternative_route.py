import os

PROJ_DATA = r"D:\miniconda\envs\Floodsafe\Library\share\proj"
os.environ["PROJ_DATA"] = PROJ_DATA
os.environ["PROJ_LIB"] = PROJ_DATA

import pyproj
pyproj.datadir.set_data_dir(PROJ_DATA)

import numpy as np
from scipy.sparse import load_npz

from .routing_engine import calculate_route, coordinates


GRAPH_FILE = r"D:\FloodSafe\data\routing\uttarakhand\roads.npz"
RISK_FILE = r"D:\FloodSafe\data\routing\uttarakhand\road_flood_risk.npz"

graph = load_npz(GRAPH_FILE)
risk = np.load(RISK_FILE)["risk"]

extreme_edges = np.where(risk == 8.0)[0]

print("\n========================================")
print("FINDING FLOOD-RISK ROUTE")
print("========================================")
print(f"Extreme edges: {len(extreme_edges):,}")


# ---------------------------------------------------------
# Build source node for each extreme edge
# ---------------------------------------------------------
sources = np.searchsorted(
    graph.indptr,
    extreme_edges,
    side="right"
) - 1

targets = graph.indices[extreme_edges]


# ---------------------------------------------------------
# Degree of nodes
# ---------------------------------------------------------
degrees = np.diff(graph.indptr)


# ---------------------------------------------------------
# Find an Extreme edge whose source/target nodes have
# several outgoing roads.
#
# This gives us a better chance of finding an alternative.
# ---------------------------------------------------------
candidates = []

for edge_index, source, target in zip(
    extreme_edges,
    sources,
    targets
):

    source_degree = degrees[source]
    target_degree = degrees[target]

    if source_degree >= 3 and target_degree >= 3:

        candidates.append(
            (edge_index, source, target)
        )


print(
    f"Extreme edges with useful junctions: "
    f"{len(candidates):,}"
)


if not candidates:
    print("\nNo suitable junction found.")
    raise SystemExit


# ---------------------------------------------------------
# Try only a small number of candidates
# ---------------------------------------------------------
MAX_TESTS = min(20, len(candidates))

print(
    f"Testing at most {MAX_TESTS} candidate locations..."
)


# ---------------------------------------------------------
# For each candidate, find nearby nodes connected to the
# source/target.
# ---------------------------------------------------------
for test_number, (edge_index, source, target) in enumerate(
    candidates[:MAX_TESTS],
    1
):

    print("\n" + "=" * 60)
    print(
        f"Candidate {test_number}/{MAX_TESTS}"
    )
    print("=" * 60)

    source_coord = coordinates[source]
    target_coord = coordinates[target]

    print(
        "Extreme edge:"
    )

    print(
        f"  Source: "
        f"{source_coord[1]:.6f}, "
        f"{source_coord[0]:.6f}"
    )

    print(
        f"  Target: "
        f"{target_coord[1]:.6f}, "
        f"{target_coord[0]:.6f}"
    )


    # -----------------------------------------------------
    # Immediate outgoing neighbours
    # -----------------------------------------------------
    source_start = graph.indptr[source]
    source_end = graph.indptr[source + 1]

    source_neighbors = graph.indices[
        source_start:source_end
    ]


    target_start = graph.indptr[target]
    target_end = graph.indptr[target + 1]

    target_neighbors = graph.indices[
        target_start:target_end
    ]


    print(
        f"Source neighbors: {len(source_neighbors)}"
    )

    print(
        f"Target neighbors: {len(target_neighbors)}"
    )


    # -----------------------------------------------------
    # Pick neighbors that are NOT the extreme edge itself
    # -----------------------------------------------------
    start_candidates = [
        int(n)
        for n in source_neighbors
        if int(n) != int(target)
    ]

    end_candidates = [
        int(n)
        for n in target_neighbors
        if int(n) != int(source)
    ]


    if not start_candidates or not end_candidates:
        continue


    # -----------------------------------------------------
    # Test combinations, but keep it small
    # -----------------------------------------------------
    for start_node in start_candidates[:3]:

        for end_node in end_candidates[:3]:

            start_lon, start_lat = coordinates[start_node]
            end_lon, end_lat = coordinates[end_node]


            print(
                "\nTesting:"
            )

            print(
                f"START "
                f"{start_lat:.6f}, "
                f"{start_lon:.6f}"
            )

            print(
                f"END   "
                f"{end_lat:.6f}, "
                f"{end_lon:.6f}"
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
                # Risk counts
                # -------------------------------------------------
                fr = fastest.get(
                    "risk_counts",
                    {}
                )

                sr = safest.get(
                    "risk_counts",
                    {}
                )

                br = balanced.get(
                    "risk_counts",
                    {}
                )


                print("\nRESULT")

                print(
                    "FASTEST  :",
                    fr
                )

                print(
                    "SAFEST   :",
                    sr
                )

                print(
                    "BALANCED :",
                    br
                )


                # -------------------------------------------------
                # Compare paths
                # -------------------------------------------------
                fast_path = tuple(
                    fastest["path"]
                )

                safe_path = tuple(
                    safest["path"]
                )

                balanced_path = tuple(
                    balanced["path"]
                )


                different = (
                    fast_path != safe_path
                    or
                    fast_path != balanced_path
                    or
                    safe_path != balanced_path
                )


                # -------------------------------------------------
                # Success
                # -------------------------------------------------
                if different:

                    print("\n")
                    print(
                        "########################################"
                    )
                    print(
                        "SUCCESS!"
                    )
                    print(
                        "########################################"
                    )

                    print(
                        "\nUse this test location:"
                    )

                    print(
                        f"START LAT: {start_lat}"
                    )

                    print(
                        f"START LON: {start_lon}"
                    )

                    print(
                        f"END LAT: {end_lat}"
                    )

                    print(
                        f"END LON: {end_lon}"
                    )

                    print(
                        "\nFASTEST risk:",
                        fr
                    )

                    print(
                        "SAFEST risk:",
                        sr
                    )

                    print(
                        "BALANCED risk:",
                        br
                    )

                    raise SystemExit


            except Exception as e:

                print(
                    "Route test failed:",
                    e
                )


print("\n========================================")
print("NO DIFFERENT ROUTE FOUND")
print("========================================")
print(
    "The tested junctions did not produce "
    "different routing paths."
)