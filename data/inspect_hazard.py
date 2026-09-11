import geopandas as gpd

FILE = "data/flood/real/ghaziabad_hazard.geojson"

print("Loading hazard GeoJSON...")
gdf = gpd.read_file(FILE)

print("\n==============================")
print("HAZARD GEOJSON INSPECTION")
print("==============================")

print("CRS:", gdf.crs)
print("Total polygons:", len(gdf))

print("\nColumns:")
print(gdf.columns.tolist())

print("\nHazard classes:")
print(gdf["hazard"].value_counts())

print("\nGeometry types:")
print(gdf.geometry.geom_type.value_counts())

print("\nInvalid geometries:",
      (~gdf.geometry.is_valid).sum())

# Project to UTM for accurate area
gdf_utm = gdf.to_crs("EPSG:32644")

gdf_utm["area_m2"] = gdf_utm.geometry.area
gdf_utm["area_ha"] = gdf_utm["area_m2"] / 10000

print("\nArea:")
print("Minimum:", gdf_utm["area_m2"].min(), "m²")
print("Maximum:", gdf_utm["area_m2"].max(), "m²")
print("Total:", gdf_utm["area_m2"].sum(), "m²")

print("\nPolygons below 100 m²:",
      (gdf_utm["area_m2"] < 100).sum())

print("\n==============================")
print("Inspection complete")
print("==============================")