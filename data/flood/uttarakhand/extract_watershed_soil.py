import os
import json
import math
import time

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

import rasterio
from rasterio.warp import transform as warp_transform
import geopandas as gpd
from shapely.geometry import Point

# ============================================================
# WATERSHED + SOIL DATA FOR FFGS
#
# Adds two real, independently-sourced physical factors to FFGS's
# rainfall thresholds, on top of the existing hazard-atlas class and
# antecedent-rainfall heuristics:
#
# - Soil texture (sand/clay/silt %, 0-5cm) from SoilGrids v2.0
#   (ISRIC — soilgrids.org), used to estimate a simplified NRCS
#   Hydrologic Soil Group (A-D), a standard proxy for infiltration
#   capacity used in SCS Curve Number-style runoff estimation. Poorly
#   draining soil (group C/D) needs less rain to produce the same
#   runoff as free-draining soil (group A/B).
#
# - Upstream contributing catchment area (km²) from HydroBASINS v1c
#   level 8 (HydroSHEDS/WWF), used as a proxy for how fast rainfall
#   concentrates into runoff at a point: a small, steep headwater
#   catchment reacts far faster to a rain burst than a point on a
#   large river system already carries a huge, slower-responding
#   upstream area — the classic flash-flood mechanism this system is
#   named for.
#
# Both are real datasets, sampled/joined once here and cached to a
# JSON file — never fetched live. SoilGrids' point-query API alone
# takes 2-20+ seconds per point (and times out outright under load),
# so this reads its underlying Cloud-Optimized GeoTIFFs directly via
# GDAL's /vsicurl/ remote-read support instead, which is what ISRIC
# itself recommends for anything beyond one-off lookups.
#
# Coverage gaps are real and kept as gaps, not papered over: SoilGrids
# has no data for some pixels (rivers, some steep/glaciated terrain),
# and a few points may fall outside every HydroBASINS polygon. Both
# show up as null fields here, and server.py treats null as "no
# adjustment for this factor" rather than guessing a value.
# ============================================================

DATA_DIR = os.environ.get(
    "FLOODSAFE_DATA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
)

LOCALITIES_FILE = os.path.join(DATA_DIR, "localities.json")
OUTPUT_FILE = os.path.join(DATA_DIR, "watershed_soil.json")

# uttarakhand_watersheds.geojson is a one-time clip of HydroBASINS v1c
# level 8 for Asia (237 sub-basins over Uttarakhand's extent, out of
# ~1000s continent-wide) -- committed directly rather than
# re-downloaded on every run, since the full Asia shapefile is ~35MB
# zipped / ~85MB unzipped and doesn't change. To regenerate it after
# a HydroBASINS update:
#
#   curl -o hybas_as_lev08_v1c.zip \
#     https://data.hydrosheds.org/file/HydroBASINS/standard/hybas_as_lev08_v1c.zip
#   unzip hybas_as_lev08_v1c.zip
#   python -c "
#   import geopandas as gpd
#   gdf = gpd.read_file('hybas_as_lev08_v1c.shp', bbox=(77.0, 28.3, 81.5, 31.6))
#   gdf.to_file('data/uttarakhand_watersheds.geojson', driver='GeoJSON')
#   "
WATERSHED_FILE = os.path.join(DATA_DIR, "uttarakhand_watersheds.geojson")

# Same 9 towns FFGS has always used (see GUIDANCE_TOWNS / ANCHOR_TOWNS
# in server.py and extract_localities.py) -- duplicated here rather
# than imported, same reasoning as extract_localities.py: server.py
# isn't a clean module to import from a standalone build script.
GUIDANCE_TOWNS = [
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

SOILGRIDS_PROPERTIES = ["sand", "clay", "silt"]
SOILGRIDS_DEPTH = "0-5cm"
SOILGRIDS_BASE_URL = "/vsicurl/https://files.isric.org/soilgrids/latest/data/{prop}/{prop}_{depth}_mean.vrt"
SOILGRIDS_NODATA = -32768
SOILGRIDS_SCALE = 10.0  # mapped units are g/kg * 10 -> percent


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


def classify_hydrologic_soil_group(sand_pct, clay_pct):
    """
    Simplified NRCS Hydrologic Soil Group from surface texture.
    This is a coarse approximation of the full USDA texture-triangle
    lookup (real HSG assignment also considers depth-to-restrictive-
    layer and saturated hydraulic conductivity, neither of which is
    practical to source here) -- good enough to rank relative
    infiltration capacity between FFGS zones, not a substitute for a
    proper soil survey.
    """

    if clay_pct is None or sand_pct is None:
        return None

    if clay_pct >= 40:
        return "D"
    if clay_pct >= 27 or (clay_pct >= 20 and sand_pct < 45):
        return "C"
    if sand_pct >= 50 and clay_pct < 20:
        return "A"
    return "B"


def load_points():

    points = [dict(t, source="town") for t in GUIDANCE_TOWNS]

    if os.path.exists(LOCALITIES_FILE):
        with open(LOCALITIES_FILE, "r", encoding="utf-8") as f:
            localities = json.load(f)
        points.extend(
            {"name": loc["name"], "lat": loc["lat"], "lon": loc["lon"], "source": "locality"}
            for loc in localities
        )

    return points


def sample_soilgrids(points):

    print("\nSampling SoilGrids (sand/clay/silt, 0-5cm)...")

    results = {i: {} for i in range(len(points))}

    for prop in SOILGRIDS_PROPERTIES:

        url = SOILGRIDS_BASE_URL.format(prop=prop, depth=SOILGRIDS_DEPTH)
        print(f"  Opening {prop} raster...")
        t0 = time.time()

        with rasterio.open(url) as src:

            print(f"    opened in {time.time() - t0:.1f}s")

            lons = [p["lon"] for p in points]
            lats = [p["lat"] for p in points]
            xs, ys = warp_transform("EPSG:4326", src.crs, lons, lats)

            t1 = time.time()

            for i, (x, y) in enumerate(zip(xs, ys)):
                raw = list(src.sample([(x, y)]))[0][0]
                results[i][prop] = None if raw == SOILGRIDS_NODATA else round(raw / SOILGRIDS_SCALE, 1)

            print(f"    sampled {len(points)} points in {time.time() - t1:.1f}s")

    return results


def load_watersheds():

    print("\nLoading watershed boundaries:", WATERSHED_FILE)
    gdf = gpd.read_file(WATERSHED_FILE)
    print("Sub-basins loaded:", len(gdf))
    return gdf


def watershed_for_point(lat, lon, watersheds_gdf):

    point = Point(lon, lat)
    match = watersheds_gdf[watersheds_gdf.contains(point)]

    if len(match) == 0:
        return None

    row = match.iloc[0]

    return {
        "hybas_id": int(row["HYBAS_ID"]),
        "up_area_km2": float(row["UP_AREA"]),
        "sub_area_km2": float(row["SUB_AREA"])
    }


def main():

    points = load_points()
    print("Points to process:", len(points))

    soil_by_index = sample_soilgrids(points)
    watersheds_gdf = load_watersheds()

    output = []

    for i, point in enumerate(points):

        soil = soil_by_index.get(i, {})
        sand, clay, silt = soil.get("sand"), soil.get("clay"), soil.get("silt")
        hsg = classify_hydrologic_soil_group(sand, clay)

        watershed = watershed_for_point(point["lat"], point["lon"], watersheds_gdf)

        output.append({
            "name": point["name"],
            "lat": point["lat"],
            "lon": point["lon"],
            "soil": {
                "sand_pct": sand,
                "clay_pct": clay,
                "silt_pct": silt,
                "hydrologic_soil_group": hsg
            } if hsg or sand is not None else None,
            "watershed": watershed
        })

    print(f"\nTotal points: {len(output)}")

    soil_hits = sum(1 for o in output if o["soil"])
    watershed_hits = sum(1 for o in output if o["watershed"])
    print(f"  with soil data: {soil_hits}")
    print(f"  with watershed data: {watershed_hits}")

    hsg_counts = {}
    for o in output:
        if o["soil"] and o["soil"]["hydrologic_soil_group"]:
            g = o["soil"]["hydrologic_soil_group"]
            hsg_counts[g] = hsg_counts.get(g, 0) + 1
    print("  soil group breakdown:", hsg_counts)

    os.makedirs(DATA_DIR, exist_ok=True)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("\nWritten to:", OUTPUT_FILE)


if __name__ == "__main__":
    main()
