import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

GRAPH = r"D:\FloodSafe\data\routing\uttarakhand\roads.npz"
RISK_GRAPH = r"D:\FloodSafe\data\routing\uttarakhand\road_flood_risk.npz"

g = np.load(GRAPH)
r = np.load(RISK_GRAPH)

indices = g["indices"]
indptr = g["indptr"]
distance = g["data"].astype(np.float64)
risk = r["risk"].astype(np.float64)

n = len(indptr) - 1

print("Loaded graph")
print("Nodes:", n)
print("Edges:", len(distance))
print("Extreme edges:", np.sum(risk == 8))

# ---------------------------------------------------------
# Test Extreme edges one by one
# ---------------------------------------------------------

print("\nSearching for Extreme edges with an alternative path...")

found = False

extreme_edges = np.where(risk == 8)[0]

for edge_index in extreme_edges:

    # Find source node of this CSR edge
    source = np.searchsorted(
        indptr,
        edge_index,
        side="right"
    ) - 1

    target = int(indices[edge_index])

    # Original graph
    original = csr_matrix(
        (distance, indices, indptr),
        shape=(n, n)
    )

    # Remove ONLY this Extreme edge
    modified_distance = distance.copy()
    modified_distance[edge_index] = np.inf

    modified = csr_matrix(
        (modified_distance, indices, indptr),
        shape=(n, n)
    )

    # Route from source to target
    result_original = dijkstra(
        original,
        directed=True,
        indices=source
    )[target]

    result_modified = dijkstra(
        modified,
        directed=True,
        indices=source
    )[target]

    # If target is still reachable, an alternative exists
    if np.isfinite(result_modified):

        print("\nFOUND ALTERNATIVE")
        print("----------------------------")
        print("Extreme edge index:", edge_index)
        print("Source node:", source)
        print("Target node:", target)

        print(
            "Extreme edge distance:",
            distance[edge_index],
            "m"
        )

        print(
            "Original route:",
            result_original,
            "m"
        )

        print(
            "Alternative route:",
            result_modified,
            "m"
        )

        print(
            "Extra distance:",
            result_modified - result_original,
            "m"
        )

        found = True
        break


if not found:
    print("\nNo alternative path found.")