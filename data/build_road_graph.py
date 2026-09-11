import os

# --------------------------------------------------
# PROJ FIX
# --------------------------------------------------
PROJ_DATA = r"D:\miniconda\envs\FloodSafe\Library\share\proj"

os.environ["PROJ_DATA"] = PROJ_DATA
os.environ["PROJ_LIB"] = PROJ_DATA

import pyproj
pyproj.datadir.set_data_dir(PROJ_DATA)

# --------------------------------------------------
# IMPORTS
# --------------------------------------------------
from pyrosm import OSM
import geopandas as gpd
import numpy as np
from scipy.sparse import csr_matrix, save_npz

# --------------------------------------------------
# FILES
# --------------------------------------------------
PBF = r"data\osm\northern\ghaziabad_roads_full.osm.pbf"
HAZARD = r"data\flood\real\ghaziabad_hazard.geojson"

OUTPUT_DIR = r"data\routing\ghaziabad"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# --------------------------------------------------
# LOAD HAZARD EXTENT
# --------------------------------------------------
print("Loading hazard extent...")

hazard = gpd.read_file(HAZARD)

minx, miny, maxx, maxy = hazard.total_bounds

BUFFER = 0.03

minx -= BUFFER
miny -= BUFFER
maxx += BUFFER
maxy += BUFFER

print("\nRouting bounding box:")
print(f"  Longitude: {minx:.6f} -> {maxx:.6f}")
print(f"  Latitude:  {miny:.6f} -> {maxy:.6f}")

# --------------------------------------------------
# LOAD CROPPED OSM DATA
# --------------------------------------------------
print("\nLoading OSM road data...")

osm = OSM(PBF)

print("Building driving network...")

nodes, edges = osm.get_network(
    network_type="driving",
    nodes=True
)

print(f"\nNodes: {len(nodes):,}")
print(f"Edges: {len(edges):,}")

# --------------------------------------------------
# CREATE COMPACT NODE INDEX
# --------------------------------------------------
print("\nCreating compact node index...")

node_ids = nodes["id"].to_numpy(dtype=np.int64)

id_to_index = {
    node_id: i
    for i, node_id in enumerate(node_ids)
}

# --------------------------------------------------
# CONVERT EDGES
# --------------------------------------------------
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

length = edges["length"].fillna(0).to_numpy(dtype=np.float32)

# --------------------------------------------------
# BUILD SPARSE GRAPH
# --------------------------------------------------
print("Building sparse routing graph...")

n = len(nodes)

graph = csr_matrix(
    (length, (u_idx, v_idx)),
    shape=(n, n),
    dtype=np.float32
)

# --------------------------------------------------
# SAVE GRAPH
# --------------------------------------------------
print("Saving compact graph...")

save_npz(
    os.path.join(OUTPUT_DIR, "roads.npz"),
    graph
)

np.save(
    os.path.join(OUTPUT_DIR, "node_ids.npy"),
    node_ids
)

coordinates = nodes[["lon", "lat"]].to_numpy(dtype=np.float64)

np.save(
    os.path.join(OUTPUT_DIR, "coordinates.npy"),
    coordinates
)

print("\n======================================")
print("GRAPH BUILD COMPLETE")
print("======================================")
print(f"Nodes: {n:,}")
print(f"Edges: {len(edges):,}")
print(f"Graph: {OUTPUT_DIR}\\roads.npz")
print(f"Nodes: {OUTPUT_DIR}\\node_ids.npy")
print(f"Coordinates: {OUTPUT_DIR}\\coordinates.npy")
print("======================================")