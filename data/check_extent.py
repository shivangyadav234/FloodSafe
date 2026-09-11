import geopandas as gpd

gdf = gpd.read_file(
    "data/flood/real/ghaziabad_hazard.geojson"
)

print("Bounding box:")
print(gdf.total_bounds)

print("\nLatitude range:")
print(gdf.total_bounds[1], "to", gdf.total_bounds[3])

print("\nLongitude range:")
print(gdf.total_bounds[0], "to", gdf.total_bounds[2])