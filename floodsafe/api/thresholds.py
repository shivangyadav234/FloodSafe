"""
FFGS threshold logic, ported from server.py unchanged.

These numbers are a heuristic approximation of a Flash Flood Guidance
product, not official CWC/IMD values -- reproduced here verbatim rather
than re-derived so the FastAPI service and the Flask app cannot drift
apart while both are running.

Base thresholds are per hazard class and rainfall duration, then scaled
by two independent multipliers:

  antecedent  how wet the ground already is (48h rainfall as a stand-in
              for soil moisture, which this project has no model for)
  static      catchment size and soil infiltration capacity, neither of
              which changes with the weather
"""

FFGS_DURATIONS = ("1h", "3h", "24h")

DURATION_THRESHOLDS_MM = {
    "EXTREME": {
        "1h":  {"watch": 4.0,  "critical": 8.0},
        "3h":  {"watch": 8.0,  "critical": 15.0},
        "24h": {"watch": 20.0, "critical": 40.0},
    },
    "SIGNIFICANT": {
        "1h":  {"watch": 12.0, "critical": 20.0},
        "3h":  {"watch": 20.0, "critical": 35.0},
        "24h": {"watch": 45.0, "critical": 80.0},
    },
    "MODERATE": {
        "1h":  {"watch": 20.0, "critical": 35.0},
        "3h":  {"watch": 35.0, "critical": 55.0},
        "24h": {"watch": 70.0, "critical": 120.0},
    },
    "LOW": {
        "1h":  {"watch": 35.0,  "critical": 60.0},
        "3h":  {"watch": 55.0,  "critical": 90.0},
        "24h": {"watch": 110.0, "critical": 180.0},
    },
}

# Highest floor first; the first one the total clears wins.
ANTECEDENT_BREAKPOINTS_MM = [(100.0, 0.70), (50.0, 0.85), (0.0, 1.0)]
CATCHMENT_BREAKPOINTS_KM2 = [(1000.0, 1.0), (100.0, 0.95), (0.0, 0.85)]
SOIL_GROUP_MULTIPLIER = {"A": 1.15, "B": 1.0, "C": 0.9, "D": 0.8}

# An FFPI band standing in for a hazard class where the atlas has no
# coverage. Only ever consulted when hazard_class is NULL.
FFPI_BAND_TO_CLASS = {
    "VERY HIGH": "EXTREME",
    "HIGH": "SIGNIFICANT",
    "MODERATE": "MODERATE",
    "LOW": "LOW",
    "VERY LOW": "LOW",
}


def antecedent_multiplier(antecedent_48h_mm: float | None) -> float:
    if antecedent_48h_mm is None:
        return 1.0
    for floor_mm, multiplier in ANTECEDENT_BREAKPOINTS_MM:
        if antecedent_48h_mm >= floor_mm:
            return multiplier
    return 1.0


def catchment_multiplier(up_area_km2: float | None) -> float:
    if up_area_km2 is None:
        return 1.0
    for floor_km2, multiplier in CATCHMENT_BREAKPOINTS_KM2:
        if up_area_km2 >= floor_km2:
            return multiplier
    return 1.0


def soil_multiplier(hydrologic_soil_group: str | None) -> float:
    return SOIL_GROUP_MULTIPLIER.get(hydrologic_soil_group, 1.0)


def static_multiplier(hydrologic_soil_group: str | None,
                      up_area_km2: float | None) -> float:
    return round(soil_multiplier(hydrologic_soil_group)
                 * catchment_multiplier(up_area_km2), 3)


def thresholds_for_class(hazard_class: str | None,
                         antecedent_48h_mm: float | None = None,
                         static_mult: float = 1.0) -> dict | None:
    base = DURATION_THRESHOLDS_MM.get(hazard_class or "")
    if base is None:
        return None

    multiplier = antecedent_multiplier(antecedent_48h_mm) * static_mult
    return {
        duration: {
            "watch": round(v["watch"] * multiplier, 1),
            "critical": round(v["critical"] * multiplier, 1),
        }
        for duration, v in base.items()
    }


def status_for_duration(rain_mm: float | None, thresholds: dict | None) -> str | None:
    """SAFE / WATCH / CRITICAL, or None when there is nothing to compare."""
    if rain_mm is None or not thresholds:
        return None
    if rain_mm >= thresholds["critical"]:
        return "CRITICAL"
    if rain_mm >= thresholds["watch"]:
        return "WATCH"
    return "SAFE"


_SEVERITY = {None: 0, "SAFE": 1, "WATCH": 2, "CRITICAL": 3}


def worse_status(a: str | None, b: str | None) -> str | None:
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


def resolve_class(hazard_class: str | None, ffpi_band: str | None) -> tuple:
    """
    Returns (effective_class, hazard_source). A surveyed atlas class
    always wins; FFPI only fills a gap, and never silently -- the
    source travels with the class so the UI can label it.
    """
    if hazard_class:
        return hazard_class, "atlas"
    if ffpi_band:
        return FFPI_BAND_TO_CLASS.get(ffpi_band), "ffpi"
    return None, None
