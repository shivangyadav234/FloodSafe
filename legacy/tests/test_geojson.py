from data.flood.geojson_provider import (
    GeoJSONFloodProvider
)


provider = GeoJSONFloodProvider(
    "data/flood/test_flood.geojson"
)


# -----------------------------------------
# Safe location
# -----------------------------------------

result1 = provider.get_flood_risk(
    28.6700,
    77.4500
)

print()
print("SAFE LOCATION")
print(result1)


# -----------------------------------------
# Flooded location
# -----------------------------------------

result2 = provider.get_flood_risk(
    28.6770,
    77.4520
)

print()
print("FLOODED LOCATION")
print(result2)