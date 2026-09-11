import geopandas as gpd
from shapely.geometry import Point


class GhaziabadFloodProvider:

    def __init__(self, flood_file):
        print("Loading Ghaziabad flood data...")

        self.flood_data = gpd.read_file(flood_file)

        # Ensure geographic coordinates
        self.flood_data = self.flood_data.to_crs("EPSG:4326")

        print(
            "Flood polygons loaded:",
            len(self.flood_data)
        )

    def get_flood_risk(self, lat, lon):

        point = Point(lon, lat)

        # Find polygons containing the point
        matches = self.flood_data[
            self.flood_data.geometry.contains(point)
        ]

        # No historical flood polygon here
        if matches.empty:
            return {
                "risk": 0.0,
                "depth": 0.0,
                "velocity": 0.0,
                "status": "SAFE",
                "source": "NDEM historical flood data",
                "confidence": 0.7
            }

        # NDEM dataset contains historical inundation,
        # but not water depth or velocity.
        return {
            "risk": 0.8,
            "depth": 0.5,
            "velocity": 0.0,
            "status": "BLOCKED",
            "source": "NDEM historical flood data",
            "confidence": 0.7
        }


if __name__ == "__main__":

    FILE = (
        "data/flood/real/"
        "ncr_flood_1998_2022.geojson"
    )

    provider = GhaziabadFloodProvider(FILE)

    # Test location
    lat = 28.6692
    lon = 77.4538

    result = provider.get_flood_risk(
        lat,
        lon
    )

    print()
    print("FLOODSAFE FLOOD RESULT")
    print("----------------------")
    print(result)