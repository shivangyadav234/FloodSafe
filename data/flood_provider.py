from abc import ABC, abstractmethod
from shapely.geometry import Point


class FloodDataProvider(ABC):

    @abstractmethod
    def get_flood_risk(self, lat, lon):
        """
        Return flood information for a location.

        Result format:

        {
            "risk": 0.0 - 1.0,
            "depth": meters,
            "velocity": meters/second,
            "status": "SAFE/RISKY/BLOCKED",
            "source": "...",
            "confidence": 0.0 - 1.0
        }
        """
        pass


class SimulatedFloodProvider(FloodDataProvider):

    def __init__(self, flood_polygon):
        self.flood_polygon = flood_polygon

    def get_flood_risk(self, lat, lon):

        point = Point(lon, lat)

        if not self.flood_polygon.contains(point):

            return {
                "risk": 0.0,
                "depth": 0.0,
                "velocity": 0.0,
                "status": "SAFE",
                "source": "simulation",
                "confidence": 1.0
            }

        # Simulated flood condition
        risk = 0.8
        depth = 0.5
        velocity = 0.8

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
            "source": "simulation",
            "confidence": 1.0
        }