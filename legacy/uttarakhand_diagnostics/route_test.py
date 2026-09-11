from .routing_engine import route

START = (30.316, 78.032)       # latitude, longitude
DEST = (30.066, 78.492)        # latitude, longitude

for mode in ["FASTEST", "SAFEST", "BALANCED"]:
    print(f"\n=== {mode} ===")

    result = route(
        START[1], START[0],
        DEST[1], DEST[0],
        mode
    )

    print("Distance:", result["distance_km"], "km")
    print("Nodes:", len(result["path"]))
    print("Risk:", result["risk_counts"])