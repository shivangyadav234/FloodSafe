import json

from shapely.geometry import shape, Point
from shapely.ops import unary_union

from data.flood_provider import FloodDataProvider


class GeoJSONFloodProvider(FloodDataProvider):

    def __init__(self, geojson_file):

        self.geojson_file = geojson_file

        print("Loading flood data...")

        with open(
            geojson_file,
            "r",
            encoding="utf-8"
        ) as file:

            data = json.load(file)

        self.features = []

        if data["type"] == "FeatureCollection":

            for feature in data["features"]:

                geometry = feature.get("geometry")

                if geometry is None:
                    continue

                polygon = shape(geometry)

                self.features.append({
                    "geometry": polygon,
                    "properties": feature.get(
                        "properties",
                        {}
                    )
                })

        elif data["type"] == "Feature":

            geometry = data.get("geometry")

            if geometry:

                self.features.append({
                    "geometry": shape(geometry),
                    "properties": data.get(
                        "properties",
                        {}
                    )
                })

        else:

            self.features.append({
                "geometry": shape(data),
                "properties": {}
            })

        if not self.features:

            raise ValueError(
                "No valid flood polygons found!"
            )

        print(
            "Flood polygons loaded:",
            len(self.features)
        )


    def get_flood_risk(self, lat, lon):

        point = Point(lon, lat)

        for feature in self.features:

            polygon = feature["geometry"]
            properties = feature["properties"]

            if polygon.contains(point):

                risk = float(
                    properties.get(
                        "risk",
                        0.8
                    )
                )

                depth = float(
                    properties.get(
                        "depth",
                        0.5
                    )
                )

                velocity = float(
                    properties.get(
                        "velocity",
                        0.0
                    )
                )

                risk = max(
                    0.0,
                    min(1.0, risk)
                )

                if risk >= 0.8:

                    status = "BLOCKED"

                elif risk >= 0.2:

                    status = "RISKY"

                else:

                    status = "SAFE"

                return {
                    "risk": risk,
                    "depth": depth,
                    "velocity": velocity,
                    "status": status,
                    "source": properties.get(
                        "source",
                        "GeoJSON"
                    ),
                    "confidence": float(
                        properties.get(
                            "confidence",
                            1.0
                        )
                    )
                }

        return {
            "risk": 0.0,
            "depth": 0.0,
            "velocity": 0.0,
            "status": "SAFE",
            "source": "GeoJSON",
            "confidence": 1.0
        }