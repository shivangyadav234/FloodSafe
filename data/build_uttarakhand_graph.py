import os

# Fix PROJ before importing geopandas/pyproj-dependent libraries
PROJ_DATA = r"D:\miniconda\envs\FloodSafe\Library\share\proj"
os.environ["PROJ_DATA"] = PROJ_DATA
os.environ["PROJ_LIB"] = PROJ_DATA

import pyproj
pyproj.datadir.set_data_dir(PROJ_DATA)

from pyrosm import OSM
import numpy as np
from scipy.sparse import csr_matrix, save_npz


PBF = r"data\osm\uttarakhand\uttarakhand_roads.osm.pbf"
OUTPUT_DIR = r"data\routing\uttarakhand"

os.makedirs(OUTPUT_DIR, exist_ok=True)

print("======================================")
print("UTTARAKHAND ROAD GRAPH - FIXED")
print("======================================")

print("\nLoading OSM road data...")
osm = OSM(PBF)

print("Building driving network...")
nodes, edges = osm.get_network(
    network_type="driving",
    nodes=True
)

print(f"\nNodes: {len(nodes):,}")
print(f"OSM Edges: {len(edges):,}")

# ---------------------------------------------------------
# COMPACT NODE INDEX
# ---------------------------------------------------------

print("\nCreating compact node index...")

node_ids = nodes["id"].to_numpy(dtype=np.int64)

id_to_index = {
    node_id: i
    for i, node_id in enumerate(node_ids)
}

# ---------------------------------------------------------
# CONVERT OSM EDGES
# ---------------------------------------------------------

print("Converting edges...")

u = edges["u"].to_numpy(dtype=np.int64)
v = edges["v"].to_numpy(dtype=np.int64)

u_idx = np.array(
    [id_to_index[x] for x in u],
    dtype=np.int32
)

v_idx = np.array(
    [id_to_index[x] for x in v],
    dtype=np.int32
)

length = edges["length"].fillna(0).to_numpy(
    dtype=np.float32
)

# ---------------------------------------------------------
# HANDLE ONEWAY
# ---------------------------------------------------------

print("\nProcessing one-way restrictions...")

if "oneway" in edges.columns:
    oneway = edges["oneway"].fillna("").astype(str).str.lower().str.strip().to_numpy()
else:
    oneway = np.array([""] * len(edges), dtype=str)

# Values explicitly meaning one-way
ONEWAY_FORWARD = {
    "yes",
    "true",
    "1"
}

ONEWAY_REVERSE = {
    "-1",
    "reverse"
}

from_u = []
from_v = []
weights = []

two_way_count = 0
oneway_forward_count = 0
oneway_reverse_count = 0

for i in range(len(edges)):

    a = u_idx[i]
    b = v_idx[i]
    w = length[i]

    direction = oneway[i]

    # Explicit reverse one-way
    if direction in ONEWAY_REVERSE:

        from_u.append(b)
        from_v.append(a)
        weights.append(w)

        oneway_reverse_count += 1

    # Explicit forward one-way
    elif direction in ONEWAY_FORWARD:

        from_u.append(a)
        from_v.append(b)
        weights.append(w)

        oneway_forward_count += 1

    # Normal road = TWO WAY
    else:

        from_u.append(a)
        from_v.append(b)
        weights.append(w)

        from_u.append(b)
        from_v.append(a)
        weights.append(w)

        two_way_count += 1


from_u = np.asarray(from_u, dtype=np.int32)
from_v = np.asarray(from_v, dtype=np.int32)
weights = np.asarray(weights, dtype=np.float32)

print(f"Two-way OSM edges : {two_way_count:,}")
print(f"Forward one-way   : {oneway_forward_count:,}")
print(f"Reverse one-way   : {oneway_reverse_count:,}")
print(f"Final graph edges : {len(weights):,}")

# ---------------------------------------------------------
# BUILD CSR GRAPH
# ---------------------------------------------------------

print("\nBuilding sparse routing graph...")

n = len(nodes)

graph = csr_matrix(
    (weights, (from_u, from_v)),
    shape=(n, n),
    dtype=np.float32
)

print(f"CSR graph shape: {graph.shape}")
print(f"CSR graph edges: {graph.nnz:,}")

# ---------------------------------------------------------
# SAVE GRAPH
# ---------------------------------------------------------

print("\nSaving compact graph...")

save_npz(
    os.path.join(OUTPUT_DIR, "roads.npz"),
    graph
)

np.save(
    os.path.join(OUTPUT_DIR, "node_ids.npy"),
    node_ids
)

coordinates = nodes[["lon", "lat"]].to_numpy(
    dtype=np.float64
)

np.save(
    os.path.join(OUTPUT_DIR, "coordinates.npy"),
    coordinates
)

# ---------------------------------------------------------
# COMPLETE
# ---------------------------------------------------------

print("\n======================================")
print("GRAPH BUILD COMPLETE")
print("======================================")

print(f"Nodes       : {n:,}")
print(f"OSM Edges   : {len(edges):,}")
print(f"CSR Edges   : {graph.nnz:,}")

print(f"\nGraph       : {OUTPUT_DIR}\\roads.npz")
print(f"Node IDs    : {OUTPUT_DIR}\\node_ids.npy")
print(f"Coordinates : {OUTPUT_DIR}\\coordinates.npy")

print("======================================")