import os

# =========================================================
# PROJ FIX
# =========================================================

PROJ_DATA = r"D:\miniconda\envs\FloodSafe\Library\share\proj"

os.environ["PROJ_DATA"] = PROJ_DATA
os.environ["PROJ_LIB"] = PROJ_DATA

import pyproj
pyproj.datadir.set_data_dir(PROJ_DATA)


# =========================================================
# IMPORTS
# =========================================================

import numpy as np
import geopandas as gpd

from shapely.geometry import LineString
from shapely.strtree import STRtree
from pyproj import Transformer


# =========================================================
# FILES
# =========================================================

GRAPH_DIR = r"D:\FloodSafe\data\routing\uttarakhand"

GRAPH_FILE = os.path.join(
    GRAPH_DIR,
    "roads.npz"
)

COORD_FILE = os.path.join(
    GRAPH_DIR,
    "coordinates.npy"
)

HAZARD_FILE = (
    r"D:\FloodSafe\data\flood\uttarakhand"
    r"\uttarakhand_flash_flood_hazard_clean.geojson"
)

OUTPUT_FILE = os.path.join(
    GRAPH_DIR,
    "road_flood_risk.npz"
)


# =========================================================
# SETTINGS
# =========================================================

CHUNK_SIZE = 50000

RISK = {
    "LOW": 1.0,
    "MODERATE": 2.0,
    "SIGNIFICANT": 4.0,
    "EXTREME": 8.0
}

HAZARD_CODE = {
    "NORMAL": 0,
    "LOW": 1,
    "MODERATE": 2,
    "SIGNIFICANT": 3,
    "EXTREME": 4
}

HAZARD_NAME = {
    0: "NORMAL",
    1: "LOW",
    2: "MODERATE",
    3: "SIGNIFICANT",
    4: "EXTREME"
}


# =========================================================
# LOAD CURRENT ROAD GRAPH
# =========================================================

print("=========================================================")
print("LOADING CURRENT ROAD GRAPH")
print("=========================================================")

graph = np.load(GRAPH_FILE)

indices = graph["indices"]
indptr = graph["indptr"]
data = graph["data"]

coordinates = np.load(COORD_FILE)

edge_count = len(data)
node_count = len(coordinates)

print("Nodes :", node_count)
print("Edges :", edge_count)


# =========================================================
# VERIFY CSR STRUCTURE
# =========================================================

print("\nVerifying CSR graph structure...")

if len(indptr) != node_count + 1:
    raise RuntimeError(
        f"CSR indptr mismatch: {len(indptr)} != {node_count + 1}"
    )

if len(indices) != edge_count:
    raise RuntimeError(
        f"CSR indices mismatch: {len(indices)} != {edge_count}"
    )

if indptr[-1] != edge_count:
    raise RuntimeError(
        f"CSR indptr[-1] = {indptr[-1]}, "
        f"but edge count = {edge_count}"
    )

if np.any(indices < 0) or np.any(indices >= node_count):
    raise RuntimeError("CSR contains invalid node indices.")

print("CSR structure: OK")


# =========================================================
# LOAD HAZARD
# =========================================================

print("\n=========================================================")
print("LOADING FLOOD HAZARD")
print("=========================================================")

hazard = gpd.read_file(HAZARD_FILE)

print("Hazard polygons:", len(hazard))

print("\nHazard classes:")
print(hazard["hazard"].value_counts())


# =========================================================
# PROJECT HAZARD TO UTM
# =========================================================

print("\nProjecting hazard polygons to EPSG:32644...")

hazard = hazard.to_crs("EPSG:32644")

hazard = hazard[
    hazard.geometry.notna()
    &
    ~hazard.geometry.is_empty
].copy()

hazard = hazard.reset_index(drop=True)

print("Valid hazard polygons:", len(hazard))


# =========================================================
# BUILD SPATIAL INDEX
# =========================================================

print("\nBuilding spatial index...")

hazard_geometries = list(hazard.geometry)

tree = STRtree(hazard_geometries)

hazard_values = (
    hazard["hazard"]
    .astype(str)
    .str.upper()
    .to_numpy()
)


# =========================================================
# COORDINATE TRANSFORMER
# =========================================================

print("Preparing coordinate transformation...")

transformer = Transformer.from_crs(
    "EPSG:4326",
    "EPSG:32644",
    always_xy=True
)


# =========================================================
# OUTPUT ARRAYS
# =========================================================

risk_data = np.ones(
    edge_count,
    dtype=np.float32
)

hazard_class = np.zeros(
    edge_count,
    dtype=np.uint8
)


# =========================================================
# PROCESS EDGES
# =========================================================

print("\n=========================================================")
print("CALCULATING FLOOD RISK")
print("=========================================================")

print("Chunk size:", CHUNK_SIZE)

for start in range(
    0,
    edge_count,
    CHUNK_SIZE
):

    end = min(
        start + CHUNK_SIZE,
        edge_count
    )

    print(
        f"Processing edges "
        f"{start:,} - {end:,} "
        f"({end / edge_count * 100:.1f}%)"
    )

    # -----------------------------------------------------
    # CSR source node for each edge
    # -----------------------------------------------------

    edge_positions = np.arange(
        start,
        end
    )

    source_nodes = np.searchsorted(
        indptr,
        edge_positions,
        side="right"
    ) - 1

    target_nodes = indices[
        start:end
    ]

    # -----------------------------------------------------
    # Coordinates
    #
    # coordinates[:,0] = longitude
    # coordinates[:,1] = latitude
    # -----------------------------------------------------

    lon1 = coordinates[
        source_nodes,
        0
    ]

    lat1 = coordinates[
        source_nodes,
        1
    ]

    lon2 = coordinates[
        target_nodes,
        0
    ]

    lat2 = coordinates[
        target_nodes,
        1
    ]

    # -----------------------------------------------------
    # Transform to UTM
    # -----------------------------------------------------

    x1, y1 = transformer.transform(
        lon1,
        lat1
    )

    x2, y2 = transformer.transform(
        lon2,
        lat2
    )

    # -----------------------------------------------------
    # Process every edge
    # -----------------------------------------------------

    for local_i in range(
        end - start
    ):

        global_i = start + local_i

        line = LineString([
            (
                x1[local_i],
                y1[local_i]
            ),
            (
                x2[local_i],
                y2[local_i]
            )
        ])

        candidates = tree.query(
            line,
            predicate="intersects"
        )

        if len(candidates) == 0:
            continue

        max_risk = 1.0
        max_code = 0

        for candidate in candidates:

            hazard_name = (
                hazard_values[
                    candidate
                ]
            )

            risk_value = RISK.get(
                hazard_name,
                1.0
            )

            code = HAZARD_CODE.get(
                hazard_name,
                0
            )

            # Highest hazard wins
            if risk_value > max_risk:

                max_risk = risk_value
                max_code = code

            elif (
                risk_value == max_risk
                and code > max_code
            ):

                max_code = code

        risk_data[
            global_i
        ] = max_risk

        hazard_class[
            global_i
        ] = max_code


# =========================================================
# PRE-SAVE VALIDATION
# =========================================================

print("\n=========================================================")
print("VALIDATING GENERATED RISK DATA")
print("=========================================================")

if len(risk_data) != edge_count:
    raise RuntimeError(
        "Risk array length does not match graph edge count."
    )

if len(hazard_class) != edge_count:
    raise RuntimeError(
        "Hazard class length does not match graph edge count."
    )

print(
    "Risk array length :",
    len(risk_data)
)

print(
    "Hazard array length:",
    len(hazard_class)
)

print(
    "Graph edge count   :",
    edge_count
)

print("Array alignment: OK")


# =========================================================
# SAVE
# =========================================================

print("\n=========================================================")
print("SAVING FLOOD-RISK GRAPH")
print("=========================================================")

np.savez_compressed(
    OUTPUT_FILE,
    indices=indices,
    indptr=indptr,
    data=data,
    risk=risk_data,
    hazard_class=hazard_class
)

print("Saved:")
print(OUTPUT_FILE)


# =========================================================
# RELOAD AND VERIFY
# =========================================================

print("\n=========================================================")
print("RELOADING OUTPUT FOR INTEGRITY CHECK")
print("=========================================================")

check = np.load(
    OUTPUT_FILE
)

check_indices = check["indices"]
check_indptr = check["indptr"]
check_data = check["data"]
check_risk = check["risk"]
check_hazard = check["hazard_class"]


# ---------------------------------------------------------
# Exact graph comparison
# ---------------------------------------------------------

indices_ok = np.array_equal(
    indices,
    check_indices
)

indptr_ok = np.array_equal(
    indptr,
    check_indptr
)

data_ok = np.array_equal(
    data,
    check_data
)

risk_ok = (
    len(check_risk)
    == edge_count
)

hazard_ok = (
    len(check_hazard)
    == edge_count
)


print(
    "indices identical :",
    indices_ok
)

print(
    "indptr identical  :",
    indptr_ok
)

print(
    "data identical    :",
    data_ok
)

print(
    "risk aligned      :",
    risk_ok
)

print(
    "hazard aligned    :",
    hazard_ok
)


if not (
    indices_ok
    and indptr_ok
    and data_ok
    and risk_ok
    and hazard_ok
):

    raise RuntimeError(
        "\nFATAL: risk graph integrity check FAILED."
    )


print("\nGRAPH/RISK INTEGRITY: PASSED")


# =========================================================
# SUMMARY
# =========================================================

print("\n=========================================================")
print("FLOOD RISK SUMMARY")
print("=========================================================")

unique, counts = np.unique(
    risk_data,
    return_counts=True
)

for value, count in zip(
    unique,
    counts
):

    print(
        f"Risk multiplier {value:.1f}: "
        f"{count:,} edges"
    )


print("\n=========================================================")
print("HAZARD CLASS SUMMARY")
print("=========================================================")

unique, counts = np.unique(
    hazard_class,
    return_counts=True
)

for code, count in zip(
    unique,
    counts
):

    print(
        f"{HAZARD_NAME[int(code)]:12s}: "
        f"{count:,} edges"
    )


# =========================================================
# EXTREME EDGE GEOGRAPHIC SANITY CHECK
# =========================================================

print("\n=========================================================")
print("EXTREME EDGE SANITY CHECK")
print("=========================================================")

extreme_indices = np.where(
    hazard_class == HAZARD_CODE["EXTREME"]
)[0]

print(
    "Extreme edges:",
    len(extreme_indices)
)

if len(extreme_indices) > 0:

    print(
        "\nFirst 20 Extreme edges:"
    )

    print(
        "EDGE | FROM (lon,lat) | TO (lon,lat) | DISTANCE"
    )

    for edge_i in extreme_indices[:20]:

        source = np.searchsorted(
            indptr,
            edge_i,
            side="right"
        ) - 1

        target = indices[
            edge_i
        ]

        p1 = coordinates[
            source
        ]

        p2 = coordinates[
            target
        ]

        coord_distance = np.sqrt(
            np.sum(
                (p1 - p2) ** 2
            )
        )

        print(
            f"{edge_i:6d} | "
            f"{p1} | "
            f"{p2} | "
            f"{coord_distance:.6f}°"
        )


print("\nTotal edges:", edge_count)

print("\nOutput:")
print(OUTPUT_FILE)

print("\n=========================================================")
print("DONE")
print("=========================================================")