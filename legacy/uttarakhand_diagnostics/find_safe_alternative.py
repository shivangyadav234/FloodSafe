import os

PROJ_DATA = r"D:\miniconda\envs\FloodSafe\Library\share\proj"

os.environ["PROJ_DATA"] = PROJ_DATA
os.environ["PROJ_LIB"] = PROJ_DATA

import pyproj
pyproj.datadir.set_data_dir(PROJ_DATA)

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra


GRAPH_FILE = r"D:\FloodSafe\data\routing\uttarakhand\roads.npz"
RISK_FILE = r"D:\FloodSafe\data\routing\uttarakhand\road_flood_risk.npz"
COORD_FILE = r"D:\FloodSafe\data\routing\uttarakhand\coordinates.npy"


print("Loading graph...")

g = np.load(GRAPH_FILE)
r = np.load(RISK_FILE)
coordinates = np.load(COORD_FILE)

indices = g["indices"]
indptr = g["indptr"]
distance = g["data"].astype(np.float64)
risk = r["risk"].astype(np.float64)

n = len(indptr) - 1
m = len(indices)

print("Nodes :", n)
print("Edges :", m)

extreme_edges = np.where(risk == 8.0)[0]

print("Extreme edges:", len(extreme_edges))


# ============================================================
# ROUTE
# ============================================================

def get_route(start, end, weights):

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
# ANALYZE ROUTE
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

    total_distance = 0.0

    edge_set = set()

    for i in range(len(path) - 1):

        u = path[i]
        v = path[i + 1]

        begin = indptr[u]
        end = indptr[u + 1]

        row = indices[begin:end]

        matches = np.where(row == v)[0]

        if len(matches) == 0:
            continue

        edge = begin + matches[0]

        edge_set.add(int(edge))

        total_distance += distance[edge]

        rsk = risk[edge]

        if rsk in counts:
            counts[rsk] += 1

    return {
        "distance": total_distance,
        "counts": counts,
        "edges": edge_set
    }


# ============================================================
# EXTREME-REMOVED GRAPH
# ============================================================

print("\nCreating graph with Extreme roads blocked...")

blocked_distance = distance.copy()

blocked_distance[risk == 8.0] = np.inf

safe_graph = csr_matrix(
    (
        blocked_distance,
        indices,
        indptr
    ),
    shape=(n, n)
)

print("Extreme roads blocked.")


# ============================================================
# SEARCH
# ============================================================

print("\nSearching for a route that has an Extreme option")
print("and also has a completely Extreme-free option...\n")


found = False


# Use Extreme edges as possible route locations.
for counter, extreme_edge in enumerate(extreme_edges):

    source = np.searchsorted(
        indptr,
        extreme_edge,
        side="right"
    ) - 1

    target = indices[extreme_edge]

    # --------------------------------------------------------
    # Test nodes immediately around the Extreme edge.
    # --------------------------------------------------------

    starts = [source]

    if source > 0:
        starts.append(source - 1)

    starts.extend(
        indices[
            indptr[source]:
            indptr[source + 1]
        ][:5]
    )

    ends = [target]

    if target > 0:
        ends.append(target - 1)

    ends.extend(
        indices[
            indptr[target]:
            indptr[target + 1]
        ][:5]
    )

    starts = list(dict.fromkeys(starts))
    ends = list(dict.fromkeys(ends))

    # --------------------------------------------------------
    # Try combinations.
    # --------------------------------------------------------

    for start in starts:

        for destination in ends:

            if start == destination:
                continue

            # FASTEST
            fastest_path = get_route(
                start,
                destination,
                distance
            )

            if fastest_path is None:
                continue

            fastest = analyze(
                fastest_path
            )

            if fastest is None:
                continue

            # We specifically need FASTEST to use Extreme.
            if fastest["counts"][8.0] == 0:
                continue

            # ------------------------------------------------
            # Now completely block Extreme roads.
            # ------------------------------------------------

            safe_dist, safe_pred = dijkstra(
                safe_graph,
                directed=True,
                indices=start,
                return_predecessors=True
            )

            if not np.isfinite(safe_dist[destination]):
                continue

            safe_path = []

            current = destination

            while current != start:

                safe_path.append(current)

                current = safe_pred[current]

                if current < 0:
                    break

            safe_path.append(start)
            safe_path.reverse()

            safe = analyze(safe_path)

            if safe is None:
                continue

            # ------------------------------------------------
            # SUCCESS
            # ------------------------------------------------

            if safe["counts"][8.0] == 0:

                print("\n")
                print("========================================")
                print("SUCCESS")
                print("========================================")

                print("\nExtreme edge:")
                print(extreme_edge)

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

                print("\nSTART:")
                print(
                    start,
                    coordinates[start]
                )

                print("\nDESTINATION:")
                print(
                    destination,
                    coordinates[destination]
                )

                print("\n----------------------------------------")
                print("FASTEST")
                print("----------------------------------------")

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

                print("\n----------------------------------------")
                print("EXTREME-BLOCKED ROUTE")
                print("----------------------------------------")

                print(
                    "Distance:",
                    f"{safe['distance']/1000:.3f} km"
                )

                print(
                    "Normal:",
                    safe["counts"][1.0]
                )

                print(
                    "Low:",
                    safe["counts"][2.0]
                )

                print(
                    "Significant:",
                    safe["counts"][4.0]
                )

                print(
                    "Extreme:",
                    safe["counts"][8.0]
                )

                print("\n========================================")
                print("USE THESE FOR ROUTING TEST")
                print("========================================")

                print(
                    f"START = "
                    f"{coordinates[start][0]}, "
                    f"{coordinates[start][1]}"
                )

                print(
                    f"END   = "
                    f"{coordinates[destination][0]}, "
                    f"{coordinates[destination][1]}"
                )

                print("\nAlternative route exists!")

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
            len(extreme_edges)
        )


# ============================================================
# FINAL
# ============================================================

if not found:

    print("\n========================================")
    print("NO ALTERNATIVE FOUND")
    print("========================================")

    print(
        "No tested Extreme location had both:"
    )

    print(
        "1. A fastest route using an Extreme road"
    )

    print(
        "2. A route that completely avoids Extreme roads"
    )

    print(
        "\nThis does NOT mean the routing engine is broken."
    )

    print(
        "It may mean the mapped road network has no"
        " practical alternative around the hazard areas."
    )