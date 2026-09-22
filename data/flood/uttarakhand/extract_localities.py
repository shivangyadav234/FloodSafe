import os
import json
import math

# ============================================================
# PROJ FIX (same pattern as the rest of the build pipeline)
# ============================================================

_PROJ_OVERRIDE = os.environ.get("PROJ_DATA_OVERRIDE")

if _PROJ_OVERRIDE and os.path.isdir(_PROJ_OVERRIDE):
    os.environ["PROJ_DATA"] = _PROJ_OVERRIDE
    os.environ["PROJ_LIB"] = _PROJ_OVERRIDE

import pyproj

if _PROJ_OVERRIDE and os.path.isdir(_PROJ_OVERRIDE):
    pyproj.datadir.set_data_dir(_PROJ_OVERRIDE)

from pyrosm import OSM

# ============================================================
# PATHS
# ============================================================

PBF = r"D:\FloodSafe\data\osm\uttarakhand\uttarakhand.osm.pbf"

DATA_DIR = os.environ.get(
    "FLOODSAFE_DATA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
)

OUTPUT_FILE = os.path.join(DATA_DIR, "localities.json")

# ============================================================
# EXTRACT NAMED LOCALITIES FOR WARD-LEVEL FFGS GRANULARITY
#
# Real OSM place tags only — no fabricated ward list. India's
# municipal corporations don't publish machine-readable ward
# boundaries (checked: OSM has zero admin_level boundaries for any
# Uttarakhand ULB, and no free GeoJSON/shapefile exists for
# Dehradun/Haridwar/Haldwani beyond PDF ward maps on their own
# sites), so this uses OSM's place=suburb/neighbourhood/quarter
# nodes as a practical, verifiable proxy for sub-town granularity.
# These are real, named, OSM-mapped localities — not official ward
# numbers — and the FFGS page labels them as such.
#
# place=village/hamlet was deliberately excluded: including them
# produced 200-600+ hits per town (every tiny hillside settlement),
# which is noise at this granularity, not a usable ward-equivalent
# list.
#
# Each locality is assigned to its nearest FFGS anchor town (see
# ANCHOR_TOWNS below, same set as GUIDANCE_TOWNS in server.py) if
# within ANCHOR_MAX_KM. Whether a locality ends up actually useful
# for FFGS depends on the hazard atlas separately covering that
# point — that's decided at request time in server.py (same
# unmapped-if-too-far-from-the-atlas rule already applied to the
# anchor towns themselves), not baked into this file. In practice
# that means most of these end up unused except near Rishikesh,
# where the atlas has real coverage.
# ============================================================

ANCHOR_TOWNS = [
    {"name": "Dehradun", "lat": 30.3165, "lon": 78.0322},
    {"name": "Rishikesh", "lat": 30.0869, "lon": 78.2676},
    {"name": "Haridwar", "lat": 29.9457, "lon": 78.1642},
    {"name": "Mussoorie", "lat": 30.4598, "lon": 78.0664},
    {"name": "Nainital", "lat": 29.3803, "lon": 79.4636},
    {"name": "Haldwani", "lat": 29.2183, "lon": 79.5130},
    {"name": "Almora", "lat": 29.5892, "lon": 79.6467},
    {"name": "Pithoragarh", "lat": 29.5822, "lon": 80.2181},
    {"name": "Joshimath", "lat": 30.5551, "lon": 79.5643},
]

ANCHOR_MAX_KM = 18.0

PLACE_TAGS = ["suburb", "neighbourhood", "quarter"]


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


def _get_name(row, has_name_column):
    if has_name_column:
        value = row.get("name")
        if isinstance(value, str) and value:
            return value

    tags_raw = row.get("tags")

    if not tags_raw:
        return None

    try:
        tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
    except Exception:
        return None

    return tags.get("name") if isinstance(tags, dict) else None


print("Loading OSM data:", PBF)
osm = OSM(PBF)

print("Extracting named localities (place=" + "/".join(PLACE_TAGS) + ")...")
places = osm.get_pois(custom_filter={"place": PLACE_TAGS})

print("Total place features found:", len(places))

has_name_column = "name" in places.columns

localities = []
seen = set()

for _, row in places.iterrows():

    name = _get_name(row, has_name_column)

    if not name:
        continue

    lat, lon = row.get("lat"), row.get("lon")

    if lat is None or lon is None or (isinstance(lat, float) and math.isnan(lat)):
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        centroid = geom.centroid
        lat, lon = centroid.y, centroid.x

    lat, lon = float(lat), float(lon)

    nearest_town = None
    nearest_km = None

    for town in ANCHOR_TOWNS:
        d = _haversine_km(lat, lon, town["lat"], town["lon"])
        if nearest_km is None or d < nearest_km:
            nearest_km = d
            nearest_town = town["name"]

    if nearest_km is None or nearest_km > ANCHOR_MAX_KM:
        continue

    dedupe_key = (nearest_town, name)

    if dedupe_key in seen:
        continue
    seen.add(dedupe_key)

    localities.append({
        "name": str(name),
        "town": nearest_town,
        "lat": round(lat, 5),
        "lon": round(lon, 5),
        "dist_km": round(nearest_km, 2)
    })

print(f"\nSaved localities: {len(localities)}")

by_town = {}
for loc in localities:
    by_town[loc["town"]] = by_town.get(loc["town"], 0) + 1

for town, count in sorted(by_town.items()):
    print(f"  {town}: {count}")

os.makedirs(DATA_DIR, exist_ok=True)

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(localities, f, ensure_ascii=False, indent=2)

print("\nWritten to:", OUTPUT_FILE)
