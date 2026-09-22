"""
Hazard classification and FFGS thresholds.

Most of these are regression tests for bugs that actually shipped. The
distance one in particular: classify_point used to rank hazard polygons
by raw lon/lat degrees, which at this latitude understates north-south
separation by about 15%. That flipped three of 127 zones between LOW
and EXTREME, and one of them -- shyaldoba -- is a genuine EXTREME zone
that was being served LOW thresholds, so it would not have raised a
CRITICAL alert until 54 mm/h instead of 7.2 mm/h.
"""

import math

import pytest


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class TestClassifyPoint:

    def test_returns_true_ground_distance_not_degrees(self, server):
        """The returned distance must be kilometres on the ground.

        A degree-based distance would come back as a number far below 1
        for these separations, which is how the original bug hid.
        """
        hazard_class, exact, distance_km = server.classify_point(29.90309, 79.63945)

        assert hazard_class is not None
        assert distance_km is not None
        # Degrees would be ~0.008 here; kilometres are ~0.8.
        assert 0.1 < distance_km < 15.0

    @pytest.mark.parametrize("name,lat,lon,expected", [
        # Each of these sits between a LOW and an EXTREME polygon at
        # near-equal distance, and each was classified wrongly while
        # ranking happened in degrees.
        ("Gagarigol", 29.90309, 79.63945, "LOW"),
        ("Garaser", 29.90074, 79.62049, "LOW"),
        ("shyaldoba", 29.80220, 79.82932, "EXTREME"),
    ])
    def test_contested_points_match_metric_nearest(self, server, name, lat, lon, expected):
        hazard_class, _, _ = server.classify_point(lat, lon)
        assert hazard_class == expected, (
            f"{name} should classify as {expected} by true ground distance"
        )

    def test_distance_matches_independent_haversine(self, server):
        """Cross-check the reported distance against the nearest polygon
        computed independently, so the function cannot drift."""
        from shapely.geometry import Point
        from shapely.ops import nearest_points

        lat, lon = 29.90309, 79.63945
        _, _, reported = server.classify_point(lat, lon)

        point = Point(lon, lat)
        best = min(
            haversine_km(lat, lon, nearest_points(geom, point)[0].y,
                         nearest_points(geom, point)[0].x)
            for geom, _ in server._guidance_atlas
        )
        assert reported == pytest.approx(best, abs=0.01)

    def test_far_outside_coverage_is_unmapped(self, server):
        """Well beyond the atlas, no class should be invented."""
        hazard_class, exact, _ = server.classify_point(20.0, 72.0)
        assert hazard_class is None
        assert exact is False


class TestEffectiveClass:

    def test_surveyed_atlas_wins_over_ffpi(self, server):
        """A surveyed class must never be overridden by the model."""
        assert server.FFPI_BAND_TO_HAZARD_CLASS["VERY HIGH"] == "EXTREME"

        # Rishikesh has atlas coverage (LOW) and an FFPI of ~5.4, which
        # bands as MODERATE. The atlas must win.
        zone = server.ffgs_guidance_for_point(30.0869, 78.2676)
        assert zone["hazard_class"] == "LOW"
        assert zone["effective_class"] == "LOW"
        assert zone["hazard_source"] == "atlas"

    def test_ffpi_fills_gaps_and_is_labelled(self, server):
        """Where the atlas is silent, FFPI supplies a class -- and says so."""
        zone = server.ffgs_guidance_for_point(30.5551, 79.5643)  # Joshimath
        assert zone["hazard_class"] is None
        assert zone["effective_class"] is not None
        assert zone["hazard_source"] == "ffpi", (
            "a modelled class must be attributed, never presented as surveyed"
        )

    def test_every_served_zone_has_a_source(self, server):
        for zone in server.FFGS_ZONES:
            assert zone["effective_class"] is not None
            assert zone["hazard_source"] in ("atlas", "ffpi")


class TestThresholds:

    def test_unknown_class_has_no_thresholds(self, server):
        assert server.ffgs_thresholds_for_class(None) is None

    def test_wetter_ground_lowers_thresholds(self, server):
        dry = server.ffgs_thresholds_for_class("EXTREME", 0.0)
        wet = server.ffgs_thresholds_for_class("EXTREME", 120.0)
        assert wet["1h"]["critical"] < dry["1h"]["critical"]

    def test_longer_windows_need_more_rain(self, server):
        t = server.ffgs_thresholds_for_class("LOW")
        assert t["1h"]["critical"] < t["3h"]["critical"] < t["24h"]["critical"]

    def test_watch_always_below_critical(self, server):
        for hazard_class in server.FFGS_DURATION_THRESHOLDS_MM:
            t = server.ffgs_thresholds_for_class(hazard_class)
            for window, bounds in t.items():
                assert bounds["watch"] < bounds["critical"], (
                    f"{hazard_class} {window} watch must sit below critical"
                )

    def test_more_hazardous_class_triggers_sooner(self, server):
        extreme = server.ffgs_thresholds_for_class("EXTREME")
        low = server.ffgs_thresholds_for_class("LOW")
        assert extreme["1h"]["critical"] < low["1h"]["critical"]

    def test_soil_radius_matches_the_flask_contract(self, server):
        """Soil is sampled per point, never interpolated.

        The FastAPI port defaulted this to 25 km, which silently gave
        296 zones a soil group this app reports as unknown and scaled
        all their thresholds down by 10%.
        """
        assert server.WATERSHED_SOIL_MAX_KM == 0.5
