import os

# ============================================================
# PROJ
# ============================================================

PROJ_DATA = r"D:\miniconda\envs\FloodSafe\Library\share\proj"

os.environ["PROJ_DATA"] = PROJ_DATA
os.environ["PROJ_LIB"] = PROJ_DATA

import pyproj
pyproj.datadir.set_data_dir(PROJ_DATA)

# ============================================================
# IMPORTS
# ============================================================

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra


# ============================================================
# FILES
# ============================================================

GRAPH_FILE = r"D:\FloodSafe\data\routing\uttarakhand\roads.npz"
RISK_FILE = r"D:\FloodSafe\data\routing\uttarakhand\road_flood_risk.npz"
COORD_FILE = r"D:\FloodSafe\data\routing\uttarakhand\coordinates.npy"


# ============================================================
# LOAD
# ============================================================

print("Loading graph...")

g = np.load(GRAPH_FILE)
r = np.load(RISK_FILE)
coordinates = np.load(COORD_FILE)

indices = g["indices"]
indptr = g["indptr"]
distance = g["data"].astype(np.float64)
risk = r["risk"].astype(np.float64)

n = len(indptr) - 1

print("Nodes :", n)
print("Edges :", len(indices))


# ============================================================
# FIND EXTREME EDGES
# ============================================================

extreme_edges = np.where(risk == 8.0)[0]

print(
    "Extreme edges:",
    len(extreme_edges)
)


# ============================================================
# ROUTE FUNCTION
# ============================================================

def route(start, end, weights):

    graph = csr_matrix(
        (
            weights,
            indices,
            indptr
        ),
        shape=(n, n)
    )

    dist, pred = dijkstra(
        graph,
        directed=True,
        indices=start,
        return_predecessors=True
    )

    if not np.isfinite(dist[end]):
        return None

    path = []

    current = end

    while current != start:

        path.append(current)

        current = pred[current]

        if current < 0:
            return None

    path.append(start)
    path.reverse()

    return path


# ============================================================
# ANALYZE PATH
# ============================================================

def analyze(path):

    if path is None:
        return None

    counts = {
        1.0: 0,
        2.0: 0,
        4.0: 0,
        8.0: 0
    }

    total = 0.0

    edge_set = set()

    for i in range(len(path) - 1):

        u = path[i]
        v = path[i + 1]

        a = indptr[u]
        b = indptr[u + 1]

        row = indices[a:b]

        match = np.where(row == v)[0]

        if len(match) == 0:
            continue

        e = a + match[0]

        edge_set.add(int(e))

        total += distance[e]

        rsk = risk[e]

        if rsk in counts:
            counts[rsk] += 1

    return {
        "distance": total,
        "counts": counts,
        "edges": edge_set
    }


# ============================================================
# SEARCH FOR A GOOD TEST
# ============================================================

print("\nSearching for route alternatives...")
print("This may take a little while.\n")


# We will inspect Extreme edges one by one.
#
# For each Extreme edge:
#
#   A = source of Extreme edge
#   B = target of Extreme edge
#
# We find nearby nodes on both sides and test routes.
#
# The goal is to find a pair where:
#
# FASTEST  -> uses flood-risk edge
# SAFEST   -> avoids it
#
# ============================================================

found = False


# Don't scan all 852 immediately.
# Start with the first 100 Extreme edges.

test_edges = extreme_edges[:100]


for counter, extreme_edge in enumerate(test_edges):

    source = np.searchsorted(
        indptr,
        extreme_edge,
        side="right"
    ) - 1

    target = indices[extreme_edge]

    # --------------------------------------------------------
    # Get neighbors around source and target
    # --------------------------------------------------------

    source_neighbors = indices[
        indptr[source]:
        indptr[source + 1]
    ]

    target_neighbors = indices[
        indptr[target]:
        indptr[target + 1]
    ]

    # Need actual neighboring roads
    if len(source_neighbors) < 2:
        continue

    if len(target_neighbors) < 2:
        continue

    # --------------------------------------------------------
    # Candidate start/end nodes
    # --------------------------------------------------------

    starts = list(source_neighbors[:10])
    starts.append(source)

    ends = list(target_neighbors[:10])
    ends.append(target)

    # --------------------------------------------------------
    # Test combinations
    # --------------------------------------------------------

    for start in starts:

        for end in ends:

            if start == end:
                continue

            # FASTEST
            fastest_weights = distance

            # SAFEST
            safest_weights = (
                distance * (risk ** 2)
            )

            fastest_path = route(
                start,
                end,
                fastest_weights
            )

            safest_path = route(
                start,
                end,
                safest_weights
            )

            if fastest_path is None:
                continue

            if safest_path is None:
                continue

            fastest = analyze(
                fastest_path
            )

            safest = analyze(
                safest_path
            )

            if fastest is None or safest is None:
                continue

            # ------------------------------------------------
            # We want FASTEST to actually use hazard
            # ------------------------------------------------

            fastest_hazard = (
                fastest["counts"][4.0] +
                fastest["counts"][8.0]
            )

            safest_hazard = (
                safest["counts"][4.0] +
                safest["counts"][8.0]
            )

            # ------------------------------------------------
            # We want SAFEST to avoid it
            # ------------------------------------------------

            if (
                fastest_hazard > 0
                and safest_hazard == 0
                and fastest["edges"] != safest["edges"]
            ):

                print(
                    "\n\n========================================"
                )

                print(
                    "SUCCESS: ROUTE ALTERNATIVE FOUND"
                )

                print(
                    "========================================"
                )

                print(
                    "Extreme edge:",
                    extreme_edge
                )

                print(
                    "Extreme source:",
                    source,
                    coordinates[source]
                )

                print(
                    "Extreme target:",
                    target,
                    coordinates[target]
                )

                print(
                    "\nSTART"
                )

                print(
                    start,
                    coordinates[start]
                )

                print(
                    "\nEND"
                )

                print(
                    end,
                    coordinates[end]
                )

                print(
                    "\n----------------------------------------"
                )

                print(
                    "FASTEST"
                )

                print(
                    "Distance:",
                    f"{fastest['distance']/1000:.3f} km"
                )

                print(
                    "Normal:",
                    fastest["counts"][1.0]
                )

                print(
                    "Low:",
                    fastest["counts"][2.0]
                )

                print(
                    "Significant:",
                    fastest["counts"][4.0]
                )

                print(
                    "Extreme:",
                    fastest["counts"][8.0]
                )

                print(
                    "\n----------------------------------------"
                )

                print(
                    "SAFEST"
                )

                print(
                    "Distance:",
                    f"{safest['distance']/1000:.3f} km"
                )

                print(
                    "Normal:",
                    safest["counts"][1.0]
                )

                print(
                    "Low:",
                    safest["counts"][2.0]
                )

                print(
                    "Significant:",
                    safest["counts"][4.0]
                )

                print(
                    "Extreme:",
                    safest["counts"][8.0]
                )

                print(
                    "\n========================================"
                )

                print(
                    "TEST COORDINATES"
                )

                print(
                    "========================================"
                )

                print(
                    f"START = {coordinates[start][0]}, "
                    f"{coordinates[start][1]}"
                )

                print(
                    f"END   = {coordinates[end][0]}, "
                    f"{coordinates[end][1]}"
                )

                print(
                    "\nUse these coordinates for the final"
                    " FASTEST/SAFEST/BALANCED test."
                )

                found = True

                break

        if found:
            break

    if found:
        break

    if counter % 10 == 0:
        print(
            "Checked Extreme edges:",
            counter + 1,
            "/",
            len(test_edges)
        )


# ============================================================
# RESULT
# ============================================================

if not found:

    print("\n========================================")

    print(
        "No suitable alternative found in the"
        " first 100 Extreme edges."
    )

    print(
        "The graph may have limited alternative"
        " roads around these hazard locations."
    )

    print(
        "We can expand the search to all"
        " Extreme edges next."
    )

    print("========================================")