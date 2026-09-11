import geopandas as gpd
from shapely.geometry import Point

FILE = (
    "data/flood/real/"
    "ncr_flood_1998_2022.geojson"
)

# Ghaziabad test location
GHaziabad = Point(
    77.4538,
    28.6692
)

print("Loading NCR flood data...")
gdf = gpd.read_file(FILE)

print()
print("Total NCR flood polygons:", len(gdf))
print("CRS:", gdf.crs)

# Project to metres for accurate distance calculation
gdf_projected = gdf.to_crs("EPSG:32643")

point_gdf = gpd.GeoDataFrame(
    geometry=[GHaziabad],
    crs="EPSG:4326"
)

point_projected = point_gdf.to_crs("EPSG:32643")

point = point_projected.geometry.iloc[0]

# Calculate distance from Ghaziabad point
gdf_projected["distance_m"] = (
    gdf_projected.geometry.distance(point)
)

nearest = gdf_projected.sort_values(
    "distance_m"
).head(10)

print()
print("10 CLOSEST HISTORICAL FLOOD POLYGONS")
print("------------------------------------")

for index, row in nearest.iterrows():

    print(
        f"ID: {row.get('id', index)}"
    )

    print(
        f"Distance: {row['distance_m']:.2f} metres"
    )

    print(
        f"Gridcode: {row.get('gridcode', 'N/A')}"
    )

    print()