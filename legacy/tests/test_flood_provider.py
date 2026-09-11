from shapely.geometry import box

from data.flood_provider import (
    SimulatedFloodProvider
)


# -----------------------------------------
# Create test flood zone
# -----------------------------------------

flood_polygon = box(
    77.4500,
    28.6750,
    77.4550,
    28.6800
)


provider = SimulatedFloodProvider(
    flood_polygon
)


# -----------------------------------------
# Test SAFE location
# -----------------------------------------

safe_result = provider.get_flood_risk(
    28.6700,
    77.4500
)

print("SAFE LOCATION")
print(safe_result)


# -----------------------------------------
# Test FLOODED location
# -----------------------------------------

flooded_result = provider.get_flood_risk(
    28.6770,
    77.4520
)

print()
print("FLOODED LOCATION")
print(flooded_result)