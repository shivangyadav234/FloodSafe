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
from shapely.geometry import shape as shapely_shape, Point

# ============================================================
# PATHS
# ============================================================

PBF = r"D:\FloodSafe\data\osm\uttarakhand\uttarakhand.osm.pbf"

DATA_DIR = os.environ.get(
    "FLOODSAFE_DATA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
)

OUTPUT_FILE = os.path.join(DATA_DIR, "localities.json")
HAZARD_ATLAS_FILE = os.path.join(DATA_DIR, "uttarakhand_flash_flood_hazard_clean.geojson")
DISTRICT_BOUNDARY_FILE = os.path.join(DATA_DIR, "uttarakhand_boundary.geojson")

# ============================================================
# EXTRACT NAMED LOCALITIES FOR WARD-LEVEL FFGS GRANULARITY
#
# Real OSM place tags only — no fabricated ward list. India's
# municipal corporations don't publish machine-readable ward
# boundaries (checked: OSM has zero admin_level boundaries for any
# Uttarakhand ULB, and no free GeoJSON/shapefile exists for
# Dehradun/Haridwar/Haldwani beyond PDF ward maps on their own
# sites), so this uses OSM's named place nodes as a practical,
# verifiable proxy for sub-town granularity. These are real,
# OSM-mapped localities — not official ward numbers — and every page
# that shows them labels them as such.
#
# Two independent passes feed the same output file:
#
# PASS 1 — near a known town (place=suburb/neighbourhood/quarter
# only; village/hamlet excluded here since within a flat radius of a
# town they produce 200+ hits, mostly outlying settlements rather
# than anything ward-like). Kept for continuity with the original
# Rishikesh-centric set.
#
# PASS 2 — genuinely flood-prone (any named place tag, including
# village/hamlet, within FLOOD_PRONE_MAX_KM of a MODERATE/
# SIGNIFICANT/EXTREME hazard polygon specifically). This is the
# opposite selection principle from Pass 1: instead of "near a big
# town," it's "near/in a mapped hazard corridor," which is what
# surfaces real village names in the hilly hazard-prone corridors
# this atlas actually covers (Pass 1's town-proximity search mostly
# only turns up LOW-hazard localities, since the anchor towns
# themselves are rarely inside the atlas's higher hazard classes).
# A tight 1km buffer keeps this a curated "flood-prone villages"
# list rather than every settlement anywhere near the broad
# hazard-prone terrain (which balloons past 800 hits at 5km).
#
# Whether any given locality ends up actually usable in FFGS still
# depends on server.py's classify_point() separately covering that
# exact point (same nearest-zone-within-15km rule as everywhere
# else) — this script doesn't duplicate that logic, it just decides
# which real, named places are worth checking.
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
NEAR_TOWN_PLACE_TAGS = ["suburb", "neighbourhood", "quarter"]

FLOOD_PRONE_PLACE_TAGS = ["suburb", "neighbourhood", "quarter", "village", "hamlet", "town"]
FLOOD_PRONE_HAZARD_CLASSES = ("MODERATE", "SIGNIFICANT", "EXTREME")
FLOOD_PRONE_MAX_KM = 1.0

# A flood-prone hit can be far from every anchor town (the hazard
# corridors run through hill districts the 9 anchor towns don't
# cover well) — labelling it with a distant anchor town would be
# misleading, so anything farther than this falls back to its
# district name instead.
FLOOD_PRONE_TOWN_LABEL_MAX_KM = 25.0


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
        if isinstance(value, str) and value.strip():
            return value.strip()

    tags_raw = row.get("tags")

    if not tags_raw:
        return None

    try:
        tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
    except Exception:
        return None

    if not isinstance(tags, dict):
        return None

    name = tags.get("name")

    return name.strip() if isinstance(name, str) and name.strip() else None


def _row_latlon(row):

    lat, lon = row.get("lat"), row.get("lon")

    if lat is None or lon is None or (isinstance(lat, float) and math.isnan(lat)):
        geom = row.geometry
        if geom is None or geom.is_empty:
            return None
        centroid = geom.centroid
        lat, lon = centroid.y, centroid.x

    return float(lat), float(lon)


def _nearest_town(lat, lon):

    nearest_town, nearest_km = None, None

    for town in ANCHOR_TOWNS:
        d = _haversine_km(lat, lon, town["lat"], town["lon"])
        if nearest_km is None or d < nearest_km:
            nearest_km, nearest_town = d, town["name"]

    return nearest_town, nearest_km


def _load_hazard_polygons(path):

    with open(path, "r", encoding="utf-8") as f:
        atlas = json.load(f)

    polygons = []

    for feature in atlas.get("features", []):
        try:
            hazard_class = feature["properties"]["hazard"]
            if hazard_class in FLOOD_PRONE_HAZARD_CLASSES:
                polygons.append((shapely_shape(feature["geometry"]), hazard_class))
        except Exception:
            continue

    return polygons


def _load_districts(path):

    if not os.path.exists(path):
        return []

    with open(path, "r", encoding="utf-8") as f:
        boundary = json.load(f)

    districts = []

    for feature in boundary.get("features", []):
        try:
            props = feature.get("properties", {})
            name = props.get("name")
            if props.get("admin_level") != "5" or not name:
                continue
            districts.append((shapely_shape(feature["geometry"]), name))
        except Exception:
            continue

    return districts


def _district_for_point(lat, lon, districts):

    point = Point(lon, lat)

    for geom, name in districts:
        if geom.contains(point):
            return name

    return None


def _distance_to_nearest_polygon_km(lat, lon, polygons):
    """
    Returns (distance_km, hazard_class) for the closest polygon,
    0.0 if the point falls inside one. None if there are no polygons.
    """

    from shapely.ops import nearest_points

    point = Point(lon, lat)
    best_km, best_class = None, None

    for geom, hazard_class in polygons:

        if geom.contains(point):
            return 0.0, hazard_class

        nearest_on_geom = nearest_points(geom, point)[0]
        d_km = _haversine_km(lat, lon, nearest_on_geom.y, nearest_on_geom.x)

        if best_km is None or d_km < best_km:
            best_km, best_class = d_km, hazard_class

    return best_km, best_class


print("Loading OSM data:", PBF)
osm = OSM(PBF)

print("Loading hazard atlas (MODERATE/SIGNIFICANT/EXTREME only)...")
hazard_polygons = _load_hazard_polygons(HAZARD_ATLAS_FILE)
print("Non-LOW hazard polygons:", len(hazard_polygons))

print("Loading district boundaries (for flood-prone label fallback)...")
districts = _load_districts(DISTRICT_BOUNDARY_FILE)
print("Districts loaded:", len(districts))

localities = []
seen = set()


def _add(name, town, lat, lon, extra_km, source):

    dedupe_key = (town, name)

    if dedupe_key in seen:
        return False

    seen.add(dedupe_key)

    localities.append({
        "name": name,
        "town": town,
        "lat": round(lat, 5),
        "lon": round(lon, 5),
        "dist_km": round(extra_km, 2),
        "source": source
    })

    return True


# ---- PASS 1: near a known anchor town ----

print("\nPass 1 — near an anchor town (place=" + "/".join(NEAR_TOWN_PLACE_TAGS) + ")...")
near_town_places = osm.get_pois(custom_filter={"place": NEAR_TOWN_PLACE_TAGS})
has_name_column = "name" in near_town_places.columns

pass1_count = 0

for _, row in near_town_places.iterrows():

    name = _get_name(row, has_name_column)
    if not name:
        continue

    latlon = _row_latlon(row)
    if latlon is None:
        continue
    lat, lon = latlon

    nearest_town, nearest_km = _nearest_town(lat, lon)
    if nearest_km is None or nearest_km > ANCHOR_MAX_KM:
        continue

    if _add(name, nearest_town, lat, lon, nearest_km, "near_town"):
        pass1_count += 1

print("Pass 1 localities:", pass1_count)


# ---- PASS 2: genuinely flood-prone (near/in a non-LOW hazard polygon) ----

print("\nPass 2 — flood-prone (place=" + "/".join(FLOOD_PRONE_PLACE_TAGS) +
      f", within {FLOOD_PRONE_MAX_KM}km of MODERATE/SIGNIFICANT/EXTREME)...")
flood_prone_places = osm.get_pois(custom_filter={"place": FLOOD_PRONE_PLACE_TAGS})
has_name_column_2 = "name" in flood_prone_places.columns

pass2_count = 0

for _, row in flood_prone_places.iterrows():

    name = _get_name(row, has_name_column_2)
    if not name:
        continue

    latlon = _row_latlon(row)
    if latlon is None:
        continue
    lat, lon = latlon

    hazard_km, hazard_class = _distance_to_nearest_polygon_km(lat, lon, hazard_polygons)
    if hazard_km is None or hazard_km > FLOOD_PRONE_MAX_KM:
        continue

    nearest_town, nearest_km = _nearest_town(lat, lon)

    if nearest_km is not None and nearest_km <= FLOOD_PRONE_TOWN_LABEL_MAX_KM:
        town_label = nearest_town
    else:
        district_name = _district_for_point(lat, lon, districts)
        if district_name:
            # A couple of OSM district relations already have "district"
            # baked into their name (e.g. "Pithoragarh district") while
            # most don't (e.g. "Almora") -- normalize before appending.
            district_name = district_name[:-len(" district")] if district_name.endswith(" district") else district_name
            town_label = district_name + " district"
        else:
            town_label = "Uttarakhand"

    if _add(name, town_label, lat, lon, hazard_km, "flood_prone_" + hazard_class.lower()):
        pass2_count += 1

print("Pass 2 localities:", pass2_count)


print(f"\nTotal localities: {len(localities)}")

by_town = {}
for loc in localities:
    by_town[loc["town"]] = by_town.get(loc["town"], 0) + 1

for town, count in sorted(by_town.items()):
    print(f"  {town}: {count}")

os.makedirs(DATA_DIR, exist_ok=True)

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(localities, f, ensure_ascii=False, indent=2)

print("\nWritten to:", OUTPUT_FILE)
