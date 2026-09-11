import os

PROJ_DATA = r"D:\miniconda\envs\FloodSafe\Library\share\proj"
os.environ["PROJ_DATA"] = PROJ_DATA
os.environ["PROJ_LIB"] = PROJ_DATA

import pyproj
pyproj.datadir.set_data_dir(PROJ_DATA)

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

graph = csr_matrix(
    (distance, indices, indptr),
    shape=(n, n)
)

print("Nodes:", n)
print("Edges:", graph.nnz)


# ============================================================
# EXTREME EDGE
# ============================================================

extreme_edges = np.where(risk == 8.0)[0]

edge = extreme_edges[0]

source = np.searchsorted(
    indptr,
    edge,
    side="right"
) - 1

target = indices[edge]

print("\n========================================")
print("EXTREME EDGE")
print("========================================")

print("Edge:", edge)
print("Source:", source, coordinates[source])
print("Target:", target, coordinates[target])
print("Distance:", distance[edge], "m")


# ============================================================
# FIND NODES REACHABLE FROM EXTREME SOURCE
# ============================================================

print("\nFinding connected road area...")

# Unweighted graph.
# We only need connectivity here.

ones = np.ones_like(distance)

connectivity_graph = csr_matrix(
    (
        ones,
        indices,
        indptr
    ),
    shape=(n, n)
)

# Dijkstra from the Extreme source.
# Limit distance to 5 km.

distances = dijkstra(
    connectivity_graph,
    directed=True,
    indices=source,
    limit=5000
)

reachable = np.where(
    np.isfinite(distances)
)[0]

print(
    "Reachable nodes within 5 km:",
    len(reachable)
)


# ============================================================
# FIND A FARTHER CONNECTED NODE
# ============================================================

valid = reachable[
    distances[reachable] > 1000
]

if len(valid) == 0:

    raise RuntimeError(
        "Could not find a connected node >1 km away."
    )


# Choose the farthest reachable node
end_node = valid[
    np.argmax(
        distances[valid]
    )
]

start_node = source


print("\n========================================")
print("CONNECTED TEST")
print("========================================")

print(
    "Start node:",
    start_node
)

print(
    "Start coordinates:",
    coordinates[start_node]
)

print(
    "End node:",
    end_node
)

print(
    "End coordinates:",
    coordinates[end_node]
)

print(
    "Graph distance:",
    distances[end_node] / 1000,
    "km"
)


# ============================================================
# ROUTING
# ============================================================

def run_route(mode):

    print("\n----------------------------------------")
    print(mode)
    print("----------------------------------------")

    if mode == "FASTEST":

        weights = distance

    elif mode == "SAFEST":

        weights = distance * (
            risk ** 2
        )

    elif mode == "BALANCED":

        weights = distance * (
            1 + 0.75 * (risk - 1)
        )

    weighted_graph = csr_matrix(
        (
            weights,
            indices,
            indptr
        ),
        shape=(n, n)
    )

    dist, predecessors = dijkstra(
        weighted_graph,
        directed=True,
        indices=start_node,
        return_predecessors=True
    )

    if not np.isfinite(
        dist[end_node]
    ):

        print("NO ROUTE")

        return

    # --------------------------------------------------------
    # RECONSTRUCT
    # --------------------------------------------------------

    path = []

    current = end_node

    while current != start_node:

        path.append(current)

        current = predecessors[current]

        if current < 0:

            print("RECONSTRUCTION FAILED")
            return

    path.append(start_node)

    path.reverse()

    # --------------------------------------------------------
    # ANALYZE
    # --------------------------------------------------------

    physical_distance = 0.0

    counts = {
        1.0: 0,
        2.0: 0,
        4.0: 0,
        8.0: 0
    }

    for i in range(
        len(path) - 1
    ):

        u = path[i]
        v = path[i + 1]

        a = indptr[u]
        b = indptr[u + 1]

        row = indices[a:b]

        matches = np.where(
            row == v
        )[0]

        if len(matches) == 0:
            continue

        edge_index = a + matches[0]

        physical_distance += (
            distance[edge_index]
        )

        edge_risk = risk[edge_index]

        if edge_risk in counts:

            counts[edge_risk] += 1

    print(
        "Distance:",
        f"{physical_distance / 1000:.3f} km"
    )

    print(
        "Nodes:",
        len(path)
    )

    print(
        "Normal:",
        counts[1.0]
    )

    print(
        "Low:",
        counts[2.0]
    )

    print(
        "Significant:",
        counts[4.0]
    )

    print(
        "Extreme:",
        counts[8.0]
    )


# ============================================================
# RUN
# ============================================================

for mode in [
    "FASTEST",
    "SAFEST",
    "BALANCED"
]:

    run_route(mode)


print("\n========================================")
print("TEST COMPLETE")
print("========================================")