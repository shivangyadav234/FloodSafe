import numpy as np
from .routing_engine import route, coordinates


def get_route(start_lat, start_lon, end_lat, end_lon, mode="BALANCED"):
    result = route(
        start_lon,
        start_lat,
        end_lon,
        end_lat,
        mode
    )

    # result["path"] contains node indices
    path_nodes = np.asarray(result["path"], dtype=np.int64)

    # Convert node indices -> [longitude, latitude]
    path_coords = coordinates[path_nodes]

    route_coordinates = [
        [float(lon), float(lat)]
        for lon, lat in path_coords
    ]

    return {
        "coordinates": route_coordinates,
        "distance_km": result["distance_km"],
        "risk_counts": result["risk_counts"],
    }