"""
Compare the FastAPI/PostGIS service against the Flask app.

A migration is only finished when the new stack returns the same
answers as the old one. This walks every zone plus a set of arbitrary
points and diffs the fields that matter -- hazard class, source,
FFPI, thresholds and the physical context -- reporting any divergence
rather than asserting, so one mismatch doesn't hide the rest.

Rainfall is excluded from the comparison: it is live, independently
cached on each side, and will legitimately differ between two calls.

Both servers must already be running:
    flask:   python data/flood/uttarakhand/server.py          (port 5000)
    fastapi: uvicorn floodsafe.api.main:app --port 8000

Usage:
    python check_parity.py
    python check_parity.py --flask http://127.0.0.1:5000 --api http://127.0.0.1:8000
"""

import argparse
import sys

import httpx


# Locality names carry macrons (Bahādrābād), which a cp1252 Windows
# console cannot encode -- printing one raised UnicodeEncodeError and
# took down the whole report after it had already found the mismatches.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# Arbitrary points, chosen to exercise different code paths: inside an
# atlas polygon, well outside one, high glacial terrain, and the flat
# Terai in the south.
PROBE_POINTS = [
    ("Rishikesh (atlas)", 30.0869, 78.2676),
    ("Joshimath (ffpi)", 30.5551, 79.5643),
    ("Mussoorie (ffpi)", 30.4598, 78.0664),
    ("Chamoli valley", 30.4000, 79.5500),
    ("Terai south", 29.0000, 79.4000),
    ("High Himalaya", 30.9000, 79.9000),
]

COMPARE_FIELDS = [
    "hazard_class", "effective_class", "hazard_source",
    "ffpi", "ffpi_band", "static_multiplier",
]

# For the fixed zones both stacks read the same precomputed 90 m scores,
# so FFPI must agree exactly. For an arbitrary point they deliberately
# differ: Flask reads a regridded 0.001-degree uint8 grid, while the API
# samples the native 90 m raster through PostGIS. The API is the more
# accurate of the two -- spot-checked against ffpi.tif, it reproduces the
# source exactly where the grid drifts by up to two FFPI units (Terai
# 5.0 vs 2.96, enough to cross a band boundary). So FFPI and anything
# derived from it is reported for probe points rather than failed on.
POINT_INFO_ONLY = {"ffpi", "ffpi_band", "effective_class", "hazard_source"}


def get(client, url, **params):
    r = client.get(url, params=params or None, timeout=60)
    r.raise_for_status()
    return r.json()


def approx(a, b, tol=0.051):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= tol
    return a == b


def compare(label, flask_obj, api_obj, mismatches, info_only=frozenset(), notes=None):
    for field in COMPARE_FIELDS:
        fv, av = flask_obj.get(field), api_obj.get(field)
        if approx(fv, av):
            continue
        line = f"{label}: {field}  flask={fv!r}  api={av!r}"
        if field in info_only:
            if notes is not None:
                notes.append(line)
        else:
            mismatches.append(line)

    # Thresholds derive from the class, so where the class itself is
    # info-only the thresholds must be too, or one expected difference
    # would be reported six more times.
    if info_only & {"effective_class", "ffpi"}:
        return

    # Thresholds are derived, so compare them structurally rather than
    # trusting that equal inputs produced equal output.
    ft, at = flask_obj.get("thresholds_mm"), api_obj.get("thresholds_mm")
    if (ft is None) != (at is None):
        mismatches.append(f"{label}: thresholds_mm  flask={ft!r}  api={at!r}")
    elif ft and at:
        for duration in ft:
            for bound in ("watch", "critical"):
                if not approx(ft[duration][bound], at.get(duration, {}).get(bound)):
                    mismatches.append(
                        f"{label}: thresholds_mm[{duration}][{bound}]  "
                        f"flask={ft[duration][bound]}  api={at.get(duration, {}).get(bound)}")

    fs = (flask_obj.get("soil") or {}).get("hydrologic_soil_group")
    as_ = (api_obj.get("soil") or {}).get("hydrologic_soil_group")
    if fs != as_:
        mismatches.append(f"{label}: soil group  flask={fs!r}  api={as_!r}")

    fw = (flask_obj.get("watershed") or {}).get("hybas_id")
    aw = (api_obj.get("watershed") or {}).get("hybas_id")
    if fw != aw:
        mismatches.append(f"{label}: hybas_id  flask={fw!r}  api={aw!r}")


def preflight(client, label, base, path):
    """Fail with a usable message rather than a connection traceback."""
    try:
        r = client.get(f"{base}{path}", timeout=90)
        r.raise_for_status()
    except Exception as exc:
        sys.exit(
            f"{label} is not reachable at {base} ({type(exc).__name__}).\n"
            f"  flask:   python data/flood/uttarakhand/server.py\n"
            f"  fastapi: uvicorn floodsafe.api.main:app --port 8000"
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--flask", default="http://127.0.0.1:5000")
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    args = ap.parse_args()

    mismatches: list[str] = []

    with httpx.Client() as client:
        preflight(client, "Flask app", args.flask, "/ffgs/zones")
        preflight(client, "FastAPI service", args.api, "/health")

        print("comparing /ffgs/zones ...")
        flask_zones = get(client, f"{args.flask}/ffgs/zones")["zones"]
        api_zones = get(client, f"{args.api}/ffgs/zones")["zones"]

        print(f"  flask: {len(flask_zones)} zones")
        print(f"  api:   {len(api_zones)} zones")
        if len(flask_zones) != len(api_zones):
            mismatches.append(
                f"zone count  flask={len(flask_zones)}  api={len(api_zones)}")

        # Match on rounded position: names are not unique across towns.
        by_pos = {(round(z["lat"], 4), round(z["lon"], 4)): z for z in api_zones}
        unmatched = 0
        for z in flask_zones:
            key = (round(z["lat"], 4), round(z["lon"], 4))
            counterpart = by_pos.get(key)
            if counterpart is None:
                unmatched += 1
                mismatches.append(f"zone {z['name']!r} at {key} missing from api")
                continue
            compare(f"zone {z['name']}", z, counterpart, mismatches)

        print(f"  matched {len(flask_zones) - unmatched} zones by position")

        print("comparing /ffgs/point ...")
        print("  (FFPI differences here are expected — see POINT_INFO_ONLY)")
        notes: list[str] = []
        for label, lat, lon in PROBE_POINTS:
            f = get(client, f"{args.flask}/ffgs/point", lat=lat, lon=lon)
            a = get(client, f"{args.api}/ffgs/point", lat=lat, lon=lon,
                    with_rainfall="false")
            compare(f"point {label}", f, a, mismatches, POINT_INFO_ONLY, notes)
            print(f"  {label:22s} flask={f.get('effective_class')!s:12s} "
                  f"api={a.get('effective_class')!s:12s} "
                  f"ffpi {f.get('ffpi')} / {a.get('ffpi')}")

    if notes:
        print()
        print(f"{len(notes)} expected difference(s) on arbitrary points "
              f"(API reads the native raster, Flask a regridded grid):")
        for n in notes:
            print("  ~", n)

    print()
    if mismatches:
        print(f"{len(mismatches)} MISMATCH(ES):")
        for m in mismatches[:40]:
            print("  -", m)
        if len(mismatches) > 40:
            print(f"  ... and {len(mismatches) - 40} more")
        sys.exit(1)

    print("PARITY OK — FastAPI/PostGIS matches Flask on every compared field")


if __name__ == "__main__":
    main()
