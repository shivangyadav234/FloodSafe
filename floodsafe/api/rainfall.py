"""
Live rainfall from Open-Meteo.

One batched call covers every zone and the result is cached process-wide.
This is not an optimisation, it is a correctness requirement the Flask
app learned twice: the zone list is identical for every visitor, so
letting each browser fetch it multiplied one request by the number of
users and exhausted Open-Meteo's free daily quota from a single shared
IP -- a whole hackathon venue behind one NAT would do it in minutes.

Genuinely per-visitor lookups ("check my location", an arbitrary point)
are a different case and are fetched per request, since no two callers
ask for the same point.

A failed refresh returns the last good reading rather than an empty
map, so a transient upstream error or a quota block degrades to stale
data instead of a blank table.
"""

import threading
import time

import httpx

from . import config


_lock = threading.Lock()
_cache: dict = {"timestamp": 0.0, "data": {}}


def _parse_durations(payload: dict) -> dict:
    """Totals over the trailing 1h / 3h / 24h, plus the 48h before that.

    Open-Meteo returns hourly series; `current` is a separate instant
    reading and is deliberately not added to the hourly sums, which
    would double-count the current hour.
    """
    hourly = (payload or {}).get("hourly") or {}
    values = hourly.get("precipitation") or []
    times = hourly.get("time") or []

    if not values or not times:
        return {"1h": None, "3h": None, "24h": None, "antecedent_48h": None}

    # Locate "now" in the series rather than assuming the last entry:
    # past_days + forecast_days means the tail is in the future.
    now = time.time()
    current_index = len(values) - 1
    for i, stamp in enumerate(times):
        try:
            parsed = time.mktime(time.strptime(stamp, "%Y-%m-%dT%H:%M"))
        except (ValueError, OverflowError):
            continue
        if parsed <= now:
            current_index = i
        else:
            break

    def window(hours: int, end: int) -> float | None:
        start = max(0, end - hours + 1)
        chunk = [v for v in values[start:end + 1] if v is not None]
        return round(sum(chunk), 2) if chunk else None

    antecedent_end = max(0, current_index - 24)

    return {
        "1h": window(1, current_index),
        "3h": window(3, current_index),
        "24h": window(24, current_index),
        "antecedent_48h": window(48, antecedent_end),
    }


def _request(points: list[tuple[float, float]]) -> dict:
    params = {
        "latitude": ",".join(str(lat) for lat, _ in points),
        "longitude": ",".join(str(lon) for _, lon in points),
        "current": "precipitation",
        "hourly": "precipitation",
        "past_days": 2,
        "forecast_days": 1,
        "timezone": "auto",
    }
    with httpx.Client(timeout=config.RAINFALL_TIMEOUT_SECONDS) as client:
        response = client.get(config.OPEN_METEO_URL, params=params)
        response.raise_for_status()
        payload = response.json()

    # A single location comes back as a bare object, several as a list.
    per_location = payload if isinstance(payload, list) else [payload]
    return {
        point: _parse_durations(loc)
        for point, loc in zip(points, per_location)
    }


def for_points(points: list[tuple[float, float]]) -> dict:
    """Cached rainfall keyed by (lat, lon) for the shared zone list."""
    now = time.time()

    with _lock:
        fresh_enough = (_cache["data"]
                        and now - _cache["timestamp"] < config.RAINFALL_CACHE_TTL_SECONDS)
        if fresh_enough:
            return _cache["data"]

    if not points:
        return _cache["data"]

    try:
        data = _request(points)
    except Exception as exc:
        # Stale beats empty; the table still renders with the last reading.
        print(f"[rainfall] refresh failed ({exc!r}); serving cached data")
        return _cache["data"]

    with _lock:
        _cache["timestamp"] = now
        _cache["data"] = data
    return data


def for_single_point(lat: float, lon: float) -> dict:
    """Uncached, for arbitrary one-off points. See the module docstring."""
    try:
        return _request([(lat, lon)])[(lat, lon)]
    except Exception as exc:
        print(f"[rainfall] point fetch failed ({exc!r})")
        return {"1h": None, "3h": None, "24h": None, "antecedent_48h": None}


def cache_age_seconds() -> float | None:
    if not _cache["data"]:
        return None
    return round(time.time() - _cache["timestamp"], 1)
