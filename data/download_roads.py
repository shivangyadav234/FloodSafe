import geopandas as gpd
import osmnx as ox
from pathlib import Path

HAZARD_FILE = "data/flood/real/ghaziabad_hazard.geojson"
OUTPUT_FILE = "data/roads/ghaziabad_roads.graphml"

Path("data/roads").mkdir(parents=True, exist_ok=True)

print("Loading hazard GeoJSON...")

hazard = gpd.read_file(HAZARD_FILE)
hazard = hazard.to_crs("EPSG:4326")

boundary = hazard.union_all()

print("Hazard extent:")
print(hazard.total_bounds)

# --------------------------------------------------
# Use an alternative Overpass server
# --------------------------------------------------

ox.settings.overpass_url = "https://overpass.kumi.systems/api/interpreter"

# Give the request more time
ox.settings.requests_timeout = 300

print("\nDownloading OpenStreetMap road network...")
print("Overpass server: overpass.kumi.systems")
print("This may take several minutes...")

G = ox.graph.graph_from_polygon(
    boundary,
    network_type="drive",
    simplify=True,
    retain_all=True
)

print("\n==============================")
print("ROAD NETWORK DOWNLOADED")
print("==============================")

print("Nodes:", len(G.nodes))
print("Edges:", len(G.edges))

ox.io.save_graphml(G, OUTPUT_FILE)

print("\n==============================")
print("SUCCESS!")
print("==============================")

print("Saved:")
print(OUTPUT_FILE)