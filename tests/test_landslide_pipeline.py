"""
The landslide calibration pipeline (floodsafe/pipeline/), on synthetic
data: the real inputs -- NASA's catalogue and the ERA5 cache -- are
downloads that live outside the repository.
"""

import json
import os
import sys
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

PIPELINE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "floodsafe", "pipeline")
sys.path.insert(0, PIPELINE)

import build_calibrated_thresholds as B  # noqa: E402
import build_landslide_thresholds as BL  # noqa: E402
import calibrate_landslide_thresholds as L  # noqa: E402
import calibrate_thresholds as C  # noqa: E402
import fetch_landslide_catalog as F  # noqa: E402


class TestCatalogueSelection:

    CURRENT = pd.DataFrame({
        "event_id": ["1", "2", "3", "4", "5", "6"],
        "event_date": ["08/14/2013 12:00:00 AM", "2015-07-02T00:00:00.000", "07/01/2012 12:00:00 AM",
                       "07/01/2012 12:00:00 AM", "07/01/2012 12:00:00 AM", "07/01/2012 12:00:00 AM"],
        # Dehradun city, Chamoli (Gopeshwar), Delhi, then three Dehradun
        # rows each failing one test.
        "latitude": ["30.3165", "30.4100", "28.6139", "30.3165", "30.3165", "30.3165"],
        "longitude": ["78.0322", "79.3200", "77.2090", "78.0322", "78.0322", "78.0322"],
        "location_accuracy": ["5km", "exact", "1km", "50km", "unknown", "1km"],
        "landslide_trigger": ["downpour", "monsoon", "rain", "rain", "rain", "earthquake"],
        "landslide_category": ["landslide"] * 6,
        "fatality_count": ["3", "", "0", "0", "0", "0"],
        "event_title": ["Mussoorie road slide", "Gopeshwar", "Delhi", "coarse", "unknown", "quake"],
    })

    def select(self, raw):
        return F.select(F.normalise(raw), F.district_polygons())

    def test_keeps_located_rain_triggered_slides_in_uttarakhand(self):
        features, counts = self.select(self.CURRENT)

        assert [(f["properties"]["district"], f["properties"]["date"]) for f in features] == [
            ("Dehradun", "2013-08-14"), ("Chamoli", "2015-07-02")]
        assert features[0]["properties"]["fatalities"] == 3
        assert counts["dropped_location_too_coarse"] == 2
        assert counts["dropped_trigger_earthquake"] == 1

    def test_reads_the_older_release_too(self):
        older = self.CURRENT.rename(columns={
            "event_id": "id", "event_date": "date", "landslide_trigger": "trigger",
            "landslide_category": "landslide_type", "fatality_count": "fatalities",
            "event_title": "nearest_places"})
        features, _ = self.select(older)
        assert len(features) == 2

    def test_unknown_layout_fails_loudly(self):
        with pytest.raises(SystemExit, match="missing"):
            F.normalise(pd.DataFrame({"when": ["2013-08-14"], "where": ["Dehradun"]}))


def _write_landslides(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [78.0, 30.3]},
             "properties": {"district": d, "date": day.isoformat()}} for d, day in rows]}, f)


def test_nearby_landslides_are_one_episode(tmp_path):
    path = tmp_path / "slides.json"
    _write_landslides(path, [
        ("Chamoli", date(2013, 6, 16)), ("Chamoli", date(2013, 6, 17)), ("Chamoli", date(2013, 6, 19)),
        ("Chamoli", date(2013, 8, 1)), ("Dehradun", date(2013, 6, 17))])

    episodes = L.load_landslide_events(str(path))

    assert [(e["districts"][0], e["start"], e["end"]) for e in episodes] == [
        ("Chamoli", date(2013, 6, 16), date(2013, 6, 19)),
        ("Chamoli", date(2013, 8, 1), date(2013, 8, 1)),
        ("Dehradun", date(2013, 6, 17), date(2013, 6, 17))]
    assert all(e["kind"] == "landslide" for e in episodes)


@pytest.fixture()
def synthetic_record(tmp_path, monkeypatch):
    """
    Eight monsoons at six grid points in three districts. Rain comes as
    short bursts, which never cause landslides here, and multi-day wet
    spells, which do -- so a 7-day rule should clearly beat the flood
    rule's 1h/3h/24h peaks.
    """
    rng = np.random.default_rng(7)
    era5 = tmp_path / "era5"
    era5.mkdir()
    districts = {"Dehradun": [(30.25, 78.0), (30.5, 78.0)], "Chamoli": [(30.25, 79.5), (30.5, 79.5)],
                 "Almora": [(29.5, 79.5), (29.75, 79.75)]}
    hours = 127 * 24
    slides = []

    for year in range(2010, 2018):
        start = datetime(year, 5, 27)
        for district, points in districts.items():
            base = rng.gamma(0.3, 0.8, size=hours) * (rng.random(hours) < 0.25)
            bursts = np.zeros(hours)
            for at in rng.choice(hours - 3, size=6, replace=False):
                bursts[at:at + 2] += rng.uniform(15, 40)
            spells = np.zeros(hours)
            for _ in range(3):
                at = int(rng.integers(10 * 24, hours - 8 * 24))
                spells[at:at + 6 * 24] += rng.uniform(1.0, 2.0)
                # Slides on the spell's last day, most of the time.
                if rng.random() < 0.8:
                    slides.append((district, (start + timedelta(hours=at + 6 * 24)).date()))
            for lat, lon in points:
                series = base + bursts + spells + rng.gamma(0.2, 0.2, size=hours)
                with open(era5 / f"{lat:.2f}_{lon:.2f}_{year}.json", "w", encoding="utf-8") as f:
                    json.dump({"district": district, "lat": lat, "lon": lon, "year": year,
                               "start": start.strftime("%Y-%m-%dT%H:%M"),
                               "precipitation_mm": np.round(series, 2).tolist()}, f)

    landslides = tmp_path / "landslides.json"
    _write_landslides(landslides, slides)

    # An identity mapping: live units = ERA5 units.
    levels = np.round(np.arange(0.5, 0.9991, 0.001), 3)
    ramp = np.linspace(0, 500, len(levels)).round(2).tolist()
    bias = tmp_path / "bias.json"
    bias.write_text(json.dumps({"windows": {w: {"era5_quantiles": ramp, "live_quantiles": ramp,
                                                "quantile_levels": levels.tolist()}
                                            for w in ("1h", "3h", "24h", "72h", "168h")}}))

    monkeypatch.setattr(C, "ERA5_DIR", str(era5))
    monkeypatch.setattr(L, "LANDSLIDES", str(landslides))
    monkeypatch.setattr(L, "OUT", str(tmp_path / "report.json"))
    monkeypatch.setattr(B, "BIAS", str(bias))
    monkeypatch.setattr(BL, "OUT", str(tmp_path / "landslide_thresholds.json"))
    # load_landslide_events takes the path as a default argument.
    monkeypatch.setattr(L.load_landslide_events, "__defaults__", (str(landslides),))
    return tmp_path


def test_calibration_and_build_on_synthetic_record(synthetic_record, monkeypatch):
    L.main()
    report = json.loads((synthetic_record / "report.json").read_text())

    assert report["events"]["positive_district_days"] >= 30
    # Wet spells are what the synthetic slides follow.
    assert report["roc_auc_relative_to_local_climate"]["168h"] > report["roc_auc_relative_to_local_climate"]["1h"]
    ours = report["landslide_rule_at_flood_levels"]["critical"]
    flood = report["flood_rule_on_landslides"]["critical"]
    assert ours["TSS"] > flood["TSS"]
    assert [p["target_POFD"] for p in report["operating_curve_relative"]] == list(L.CURVE_TARGETS)

    monkeypatch.setattr(sys, "argv", ["build_landslide_thresholds.py"])
    BL.main()
    built = json.loads((synthetic_record / "landslide_thresholds.json").read_text())

    assert len(built["points"]) == 6
    assert built["validation"]["adds_warning_over_flood_rule"] is True
    for point in built["points"]:
        assert set(point["thresholds_mm"]) == {"24h", "72h", "168h"}
        for pair in point["thresholds_mm"].values():
            assert 0 < pair["watch"] < pair["critical"]
        # A week holds more rain than its wettest day.
        assert point["thresholds_mm"]["168h"]["critical"] > point["thresholds_mm"]["24h"]["critical"]


def test_build_refuses_without_the_long_window_mapping(synthetic_record, monkeypatch):
    bias = json.loads(open(B.BIAS).read())
    del bias["windows"]["168h"]
    with open(B.BIAS, "w") as f:
        json.dump(bias, f)
    monkeypatch.setattr(sys, "argv", ["build_landslide_thresholds.py"])
    with pytest.raises(SystemExit, match="check_live_model_bias"):
        BL.main()
