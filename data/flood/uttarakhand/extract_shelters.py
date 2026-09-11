import os
import json

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

OUTPUT_FILE = os.path.join(DATA_DIR, "shelters.json")

# ============================================================
# EXTRACT SHELTER-RELEVANT POIS
#
# Real OSM tags only — no fabricated "official" shelter list.
# "shelter" bucket: places genuinely usable as an evacuation/
# relief point (explicitly tagged shelters, community centres,
# social facilities). "hospital" bucket: medical aid, shown
# separately since it serves a different need in an evacuation.
# Schools/colleges are excluded even though they're often used
# as real-world flood shelters in India — at 800+ points they'd
# swamp the map and we have no way to confirm which are actually
# designated relief points.
# ============================================================

SHELTER_AMENITIES = {"shelter", "community_centre", "social_facility"}
HOSPITAL_AMENITIES = {"hospital"}

print("Loading OSM data:", PBF)
osm = OSM(PBF)

print("Extracting POIs...")
pois = osm.get_pois(custom_filter={
    "amenity": list(SHELTER_AMENITIES | HOSPITAL_AMENITIES)
})

print("Total POIs found:", len(pois))

shelters = []

for _, row in pois.iterrows():

    amenity = row.get("amenity")

    if amenity in SHELTER_AMENITIES:
        kind = "shelter"
    elif amenity in HOSPITAL_AMENITIES:
        kind = "hospital"
    else:
        continue

    geom = row.geometry

    if geom is None or geom.is_empty:
        continue

    centroid = geom.centroid

    name = row.get("name")

    if not name or (isinstance(name, float)):
        name = "Unnamed " + ("Shelter" if kind == "shelter" else "Hospital")

    shelters.append({
        "id": f"osm-{int(row['id'])}",
        "name": str(name),
        "kind": kind,
        "amenity": str(amenity),
        "lat": float(centroid.y),
        "lon": float(centroid.x)
    })

print(f"\nSaved shelter-relevant points: {len(shelters)}")

by_kind = {}
for s in shelters:
    by_kind[s["amenity"]] = by_kind.get(s["amenity"], 0) + 1

for amenity, count in sorted(by_kind.items()):
    print(f"  {amenity}: {count}")

os.makedirs(DATA_DIR, exist_ok=True)

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(shelters, f, ensure_ascii=False, indent=2)

print("\nWritten to:", OUTPUT_FILE)
