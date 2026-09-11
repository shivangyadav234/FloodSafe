import osmnx as ox
import geopandas as gpd
from shapely.geometry import shape
import json
import os
from routing.flood_risk import calculate_flood_risk


# --------------------------------------------------
# SETTINGS
# --------------------------------------------------

PLACE = "Ghaziabad, Uttar Pradesh, India"

FLOOD_FILE = os.path.join(
    "data",
    "flood",
    "test_flood.geojson"
)

# Ghaziabad is around UTM Zone 43N
PROJECTED_CRS = "EPSG:32643"


# --------------------------------------------------
# LOAD FLOOD GEOJSON
# --------------------------------------------------

def load_flood_polygon():

    print("Loading flood GeoJSON...")

    with open(
        FLOOD_FILE,
        "r",
        encoding="utf-8"
    ) as file:

        data = json.load(file)

    polygons = []

    if data["type"] == "FeatureCollection":

        for feature in data["features"]:

            geometry = feature.get("geometry")

            if geometry:
                polygons.append(shape(geometry))

    elif data["type"] == "Feature":

        geometry = data.get("geometry")

        if geometry:
            polygons.append(shape(geometry))

    else:
        polygons.append(shape(data))

    if not polygons:
        raise ValueError("No flood polygons found!")

    flood_gdf = gpd.GeoDataFrame(
        geometry=polygons,
        crs="EPSG:4326"
    )

    print("Flood polygons loaded:", len(polygons))

    return flood_gdf


# --------------------------------------------------
# ROAD FLOOD ANALYSIS
# --------------------------------------------------

def analyze_roads():

    print("Downloading road network...")

    graph = ox.graph_from_place(
        PLACE,
        network_type="drive"
    )

    print("Road network downloaded!")
    print("Nodes:", len(graph.nodes))
    print("Edges:", len(graph.edges))

    # Convert OSM graph to GeoDataFrame
    nodes, edges = ox.graph_to_gdfs(
        graph,
        nodes=True,
        edges=True
    )

    # Load flood polygons
    flood_gdf = load_flood_polygon()

    # --------------------------------------------------
    # PROJECT BOTH TO METERS
    # --------------------------------------------------

    print("Projecting data to meter-based CRS...")

    edges_projected = edges.to_crs(PROJECTED_CRS)
    flood_projected = flood_gdf.to_crs(PROJECTED_CRS)

    # Combine all flood polygons
    flood_geometry = flood_projected.geometry.union_all()

    # --------------------------------------------------
    # CLASSIFICATION
    # --------------------------------------------------

    safe = 0
    risky = 0
    blocked = 0

    affected_roads = []

    for index, row in edges_projected.iterrows():

        road_geometry = row.geometry

        if road_geometry is None:
            continue

        road_length = road_geometry.length

        if road_length <= 0:
            continue

        intersection = road_geometry.intersection(
            flood_geometry
        )

        flooded_length = intersection.length

        flood_ratio = flooded_length / road_length

        # ----------------------------------------------
        # RISK CALCULATION
        # ----------------------------------------------

        # Prototype depth/velocity values.
        # These will later come from real flood data.
        flood_depth = 0.5
        water_velocity = 0.8

        risk = calculate_flood_risk(
            flood_ratio=flood_ratio,
            flood_depth=flood_depth if flood_ratio > 0 else 0,
            water_velocity=water_velocity if flood_ratio > 0 else 0
        )

        # ----------------------------------------------
        # STATUS
        # ----------------------------------------------

        if flood_ratio >= 0.80:

            status = "BLOCKED"
            blocked += 1

        elif flood_ratio >= 0.10:

            status = "RISKY"
            risky += 1

        else:

            status = "SAFE"
            safe += 1

        # ----------------------------------------------
        # SAVE AFFECTED ROAD
        # ----------------------------------------------

        if status != "SAFE":

            affected_roads.append({
                "u": index[0],
                "v": index[1],
                "key": index[2],
                "flood_ratio": flood_ratio,
                "risk": risk,
                "status": status
            })

    # --------------------------------------------------
    # RESULTS
    # --------------------------------------------------

    print()
    print("ROAD FLOOD ANALYSIS")
    print("--------------------")

    print("SAFE   :", safe)
    print("RISKY  :", risky)
    print("BLOCKED:", blocked)

    print()
    print("Sample affected roads")
    print("---------------------")

    for road in affected_roads[:10]:

        print(
            f"Road {road['u']} -> {road['v']}"
        )

        print(
            f"Flood ratio : {road['flood_ratio']:.3f}"
        )

        print(
            f"Risk        : {road['risk']:.3f}"
        )

        print(
            f"Status      : {road['status']}"
        )

        print()


# --------------------------------------------------
# RUN
# --------------------------------------------------

if __name__ == "__main__":
    analyze_roads()