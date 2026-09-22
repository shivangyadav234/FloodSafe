"""
Load every reference layer into PostGIS.

Reads the same files server.py reads today, so the database is built
from the committed sources rather than from a dump -- re-running this
after a pipeline rebuild is the whole update path.

Applies schema.sql first, which drops and recreates the reference
tables. hazard_reports is deliberately created with IF NOT EXISTS and
never dropped, so user-submitted reports survive a re-ingest.

The FFPI raster is loaded with raster2pgsql from the PostGIS bundle,
tiled 100x100 so a point lookup touches one small tile.

Usage:
    python ingest.py
    python ingest.py --dsn postgresql://postgres@127.0.0.1:5433/floodsafe
"""

import argparse
import json
import os
import subprocess

import psycopg


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_DATA = os.path.join(REPO_ROOT, "data", "flood", "uttarakhand", "data")
TERRAIN = os.path.join(REPO_ROOT, "floodsafe", "data", "terrain", "90m")
PG_HOME = os.path.join(REPO_ROOT, ".localdb", "pg")

DEFAULT_DSN = os.environ.get(
    "FLOODSAFE_DSN", "postgresql://postgres@127.0.0.1:5433/floodsafe")

# Same nine towns server.py uses; localities.json holds the rest.
GUIDANCE_TOWNS = [
    ("Dehradun", 30.3165, 78.0322),
    ("Rishikesh", 30.0869, 78.2676),
    ("Haridwar", 29.9457, 78.1642),
    ("Mussoorie", 30.4598, 78.0664),
    ("Nainital", 29.3803, 79.4636),
    ("Haldwani", 29.2183, 79.5130),
    ("Almora", 29.5892, 79.6467),
    ("Pithoragarh", 29.5822, 80.2181),
    ("Joshimath", 30.5551, 79.5643),
]


def log(msg):
    print(f"[ingest] {msg}", flush=True)


def load_json(name, base=APP_DATA):
    path = os.path.join(base, name)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def apply_schema(conn):
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql"),
              "r", encoding="utf-8") as f:
        sql = f.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    log("schema applied")


def ingest_hazard_zones(conn):
    gj = load_json("uttarakhand_flash_flood_hazard_clean.geojson")
    if not gj:
        log("  WARNING: hazard atlas missing")
        return

    rows = [
        (f["properties"]["hazard"], f["properties"].get("area_ha"),
         json.dumps(f["geometry"]))
        for f in gj["features"]
    ]

    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO hazard_zones (hazard_class, area_ha, geom)
               VALUES (%s, %s, ST_Multi(ST_GeomFromGeoJSON(%s)))""",
            rows,
        )
    conn.commit()
    log(f"hazard_zones: {len(rows)}")


def ingest_watersheds(conn):
    gj = load_json("uttarakhand_watersheds.geojson")
    if not gj:
        log("  WARNING: watersheds missing")
        return

    seen, rows = set(), []
    for f in gj["features"]:
        p = f["properties"]
        hid = int(p["HYBAS_ID"])
        if hid in seen:
            continue
        seen.add(hid)
        rows.append((hid, float(p["UP_AREA"]), float(p["SUB_AREA"]),
                     json.dumps(f["geometry"])))

    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO watersheds (hybas_id, up_area_km2, sub_area_km2, geom)
               VALUES (%s, %s, %s, ST_Multi(ST_GeomFromGeoJSON(%s)))""",
            rows,
        )
    conn.commit()
    log(f"watersheds: {len(rows)}")


def ingest_localities(conn):
    rows = [(name, None, "town", lon, lat) for name, lat, lon in GUIDANCE_TOWNS]

    for loc in (load_json("localities.json") or []):
        rows.append((loc["name"], loc.get("town"), "locality",
                     loc["lon"], loc["lat"]))

    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO localities (name, parent_town, kind, geom)
               VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
               ON CONFLICT (name, kind, parent_town) DO NOTHING""",
            rows,
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT kind, count(*) FROM localities GROUP BY kind ORDER BY kind")
        log(f"localities: {dict(cur.fetchall())}")


def ingest_shelters(conn):
    shelters = load_json("shelters.json")
    if not shelters:
        log("  WARNING: shelters missing")
        return

    rows = []
    for s in shelters:
        # The file's own discriminator is `kind`: 'shelter' or 'hospital'.
        # server.py routes evacuations only to shelters and treats
        # hospitals as a separate destination type, so that split is
        # preserved rather than flattened. `amenity` keeps the finer
        # OSM tag (shelter / community_centre / social_facility).
        kind = s.get("kind") or "unknown"
        rows.append((s.get("name"), s.get("amenity") or kind,
                     kind == "shelter", s["lon"], s["lat"]))

    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO shelters (name, category, is_evacuation_target, geom)
               VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))""",
            rows,
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT is_evacuation_target, count(*) FROM shelters "
                    "GROUP BY is_evacuation_target")
        log(f"shelters: {len(rows)} (evacuation_target -> {dict(cur.fetchall())})")


def ingest_soil(conn):
    samples = load_json("watershed_soil.json")
    if not samples:
        log("  WARNING: watershed_soil.json missing")
        return

    rows = [
        (s["soil"].get("sand_pct"), s["soil"].get("clay_pct"),
         s["soil"].get("silt_pct"), s["soil"].get("hydrologic_soil_group"),
         s["lon"], s["lat"])
        for s in samples
        if s.get("soil") and s["soil"].get("hydrologic_soil_group")
    ]

    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO soil_samples
                   (sand_pct, clay_pct, silt_pct, hydrologic_soil_group, geom)
               VALUES (%s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))""",
            rows,
        )
    conn.commit()
    log(f"soil_samples: {len(rows)} of {len(samples)} points had usable soil data")


def ingest_scores(conn):
    payload = load_json("location_scores.json")
    if not payload:
        log("  WARNING: location_scores.json missing")
        return

    matched = missed = 0
    with conn.cursor() as cur:
        for loc in payload["locations"]:
            # Match on position rather than name: locality names are not
            # unique across towns, coordinates are.
            cur.execute(
                """SELECT id FROM localities
                   ORDER BY geom <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326)
                   LIMIT 1""",
                (loc["lon"], loc["lat"]),
            )
            row = cur.fetchone()
            if not row:
                missed += 1
                continue

            cur.execute(
                """INSERT INTO ffpi_scores
                       (locality_id, ffpi, ffpi_band, model_prob, components, terrain)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (locality_id) DO UPDATE SET
                       ffpi = EXCLUDED.ffpi, ffpi_band = EXCLUDED.ffpi_band,
                       model_prob = EXCLUDED.model_prob,
                       components = EXCLUDED.components, terrain = EXCLUDED.terrain""",
                (row[0], loc.get("ffpi"), loc.get("ffpi_band"), loc.get("model_prob"),
                 json.dumps(loc.get("ffpi_components")), json.dumps(loc.get("terrain"))),
            )
            matched += 1
    conn.commit()
    log(f"ffpi_scores: {matched} matched, {missed} unmatched")


def ingest_ffpi_raster(dsn):
    tif = os.path.join(TERRAIN, "ffpi.tif")
    if not os.path.exists(tif):
        log(f"  WARNING: {tif} missing -- run floodsafe/pipeline/build_ffpi.py")
        return

    raster2pgsql = os.path.join(PG_HOME, "bin", "raster2pgsql.exe")
    psql = os.path.join(PG_HOME, "bin", "psql.exe")
    if not os.path.exists(raster2pgsql):
        log("  WARNING: raster2pgsql not found; skipping raster load")
        return

    # -s tags the SRID, it does NOT reproject -- the raster stays in its
    # native UTM 44N and ffpi_at_point transforms the query point instead.
    # -a appends into the table schema.sql already created, -t tiles it so
    # a point lookup touches one small tile, -C adds the raster
    # constraints ST_Value relies on. No -I: schema.sql already creates
    # the convex-hull index, and -I would add a second, redundant one.
    log("loading FFPI raster (UTM 44N, 100x100 tiles) ...")
    gen = subprocess.run(
        [raster2pgsql, "-a", "-s", "32644", "-t", "100x100", "-C",
         tif, "ffpi_raster"],
        capture_output=True, text=True,
    )
    if gen.returncode != 0:
        log(f"  raster2pgsql failed: {gen.stderr[:300]}")
        return

    load = subprocess.run([psql, dsn, "-v", "ON_ERROR_STOP=1", "-q"],
                          input=gen.stdout, capture_output=True, text=True)
    if load.returncode != 0:
        log(f"  raster load failed: {load.stderr[:300]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    args = ap.parse_args()

    log(f"connecting to {args.dsn}")
    with psycopg.connect(args.dsn) as conn:
        apply_schema(conn)
        ingest_hazard_zones(conn)
        ingest_watersheds(conn)
        ingest_localities(conn)
        ingest_shelters(conn)
        ingest_soil(conn)
        ingest_scores(conn)

    ingest_ffpi_raster(args.dsn)

    with psycopg.connect(args.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM ffpi_raster")
        log(f"ffpi_raster tiles: {cur.fetchone()[0]}")

        # Prove the spatial path works end to end before declaring success.
        cur.execute("SELECT * FROM classify_hazard_point(30.0869, 78.2676)")
        log(f"classify(Rishikesh) -> {cur.fetchone()}")
        cur.execute("SELECT ffpi_at_point(30.5551, 79.5643)")
        log(f"ffpi(Joshimath) -> {cur.fetchone()[0]}")

    log("ingest complete")


if __name__ == "__main__":
    main()
