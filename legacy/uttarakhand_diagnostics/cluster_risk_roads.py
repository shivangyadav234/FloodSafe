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
from scipy.spatial import cKDTree


# ---------------------------------------------------------
# FILES
# ---------------------------------------------------------
GRAPH_FILE = r"D:\FloodSafe\data\routing\uttarakhand\roads.npz"
COORD_FILE = r"D:\FloodSafe\data\routing\uttarakhand\coordinates.npy"
RISK_FILE = r"D:\FloodSafe\data\routing\uttarakhand\road_flood_risk.npz"


# ---------------------------------------------------------
# LOAD
# ---------------------------------------------------------
print("\nLoading graph...")

graph = load_npz(GRAPH_FILE)
coordinates = np.load(COORD_FILE)
risk = np.load(RISK_FILE)["risk"]

print(f"Nodes : {graph.shape[0]:,}")
print(f"Edges : {graph.nnz:,}")


# ---------------------------------------------------------
# EXTREME EDGES
# ---------------------------------------------------------
extreme_edges = np.where(risk == 8.0)[0]

print(f"Extreme edges : {len(extreme_edges):,}")


# ---------------------------------------------------------
# GET EXTREME EDGE MIDPOINTS
# ---------------------------------------------------------
sources = np.searchsorted(
    graph.indptr,
    extreme_edges,
    side="right"
) - 1

targets = graph.indices[extreme_edges]

midpoints = (
    coordinates[sources] +
    coordinates[targets]
) / 2.0


# ---------------------------------------------------------
# CLUSTER EXTREME ROADS
# ---------------------------------------------------------
#
# Roads within approximately 3 km are treated as belonging
# to the same flood-risk corridor.
#
# Coordinates are lon/lat, so this is an approximate
# geographic clustering step.
# ---------------------------------------------------------

TREE = cKDTree(midpoints)

visited = np.zeros(len(midpoints), dtype=bool)

clusters = []

for i in range(len(midpoints)):

    if visited[i]:
        continue

    # Start a new cluster
    cluster = []
    queue = [i]
    visited[i] = True

    while queue:

        current = queue.pop()
        cluster.append(current)

        neighbours = TREE.query_ball_point(
            midpoints[current],
            r=0.03
        )

        for n in neighbours:

            if not visited[n]:

                visited[n] = True
                queue.append(n)

    clusters.append(cluster)


# ---------------------------------------------------------
# SORT BY SIZE
# ---------------------------------------------------------
clusters.sort(
    key=len,
    reverse=True
)


# ---------------------------------------------------------
# RESULTS
# ---------------------------------------------------------
print("\n========================================")
print("FLOOD-RISK CORRIDORS")
print("========================================")

print(
    f"Total Extreme clusters: {len(clusters)}"
)


for number, cluster in enumerate(
    clusters[:20],
    1
):

    pts = midpoints[cluster]

    min_lon = pts[:, 0].min()
    max_lon = pts[:, 0].max()

    min_lat = pts[:, 1].min()
    max_lat = pts[:, 1].max()

    center_lon = pts[:, 0].mean()
    center_lat = pts[:, 1].mean()

    print("\n----------------------------------------")

    print(
        f"Cluster #{number}"
    )

    print(
        f"Extreme edges : {len(cluster)}"
    )

    print(
        f"Center        : "
        f"{center_lat:.6f}, "
        f"{center_lon:.6f}"
    )

    print(
        f"Longitude     : "
        f"{min_lon:.6f} → {max_lon:.6f}"
    )

    print(
        f"Latitude      : "
        f"{min_lat:.6f} → {max_lat:.6f}"
    )


# ---------------------------------------------------------
# ALSO SHOW SIGNIFICANT + MODERATE
# ---------------------------------------------------------
print("\n========================================")
print("ALL RISK DISTRIBUTION")
print("========================================")

for value in [1.0, 2.0, 4.0, 8.0]:

    count = np.sum(risk == value)

    print(
        f"{value:4.1f} : {count:,}"
    )


# ---------------------------------------------------------
# SAVE CLUSTER CENTERS
# ---------------------------------------------------------
output = []

for number, cluster in enumerate(
    clusters,
    1
):

    pts = midpoints[cluster]

    output.append(
        [
            number,
            len(cluster),
            float(pts[:, 0].mean()),
            float(pts[:, 1].mean()),
            float(pts[:, 0].min()),
            float(pts[:, 0].max()),
            float(pts[:, 1].min()),
            float(pts[:, 1].max())
        ]
    )


output = np.array(output)

OUTPUT_FILE = (
    r"D:\FloodSafe\data\flood\uttarakhand"
    r"\risk_clusters.npy"
)

np.save(
    OUTPUT_FILE,
    output
)

print(
    f"\nSaved cluster data to:"
    f"\n{OUTPUT_FILE}"
)

print("\nClustering complete.")