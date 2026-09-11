import geopandas as gpd

FILE = (
    "data/flood/real/"
    "NDEM_All_India_Flood_Innundation_1998_to_2022.geojson"
)

print("Loading dataset...")
gdf = gpd.read_file(FILE)

print()
print("TOTAL FEATURES:", len(gdf))
print("CRS:", gdf.crs)

print()
print("DATASET BOUNDS")
print("----------------")

minx, miny, maxx, maxy = gdf.total_bounds

print("Minimum Longitude:", minx)
print("Minimum Latitude :", miny)
print("Maximum Longitude:", maxx)
print("Maximum Latitude :", maxy)

print()
print("GHaziabad expected area:")
print("Longitude: approximately 77.35 - 77.55")
print("Latitude : approximately 28.60 - 28.80")

from shapely.geometry import Point

ghaziabad = Point(77.4538, 28.6692)

distances = gdf.geometry.distance(ghaziabad)

nearest_index = distances.idxmin()

print()
print("NEAREST FLOOD POLYGON")
print("----------------------")
print("Distance:", distances[nearest_index])
print("Polygon:", gdf.loc[nearest_index].geometry)