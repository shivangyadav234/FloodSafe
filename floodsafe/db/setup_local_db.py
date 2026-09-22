"""
Stand up an isolated PostGIS cluster for local development, with no
administrator rights and no passwords.

The intended path for this project is docker-compose.yml in this
directory. This script exists because Docker is not installed on every
machine and installing it needs admin rights and often a reboot. It
reaches the same end state -- a PostGIS database that belongs to this
project alone -- using only user-space files.

How it works:

  1. Copies the existing PostgreSQL install's bin/lib/share into
     .localdb/pg. Program Files is only ever read, never modified, so
     the system PostgreSQL service is untouched.
  2. Downloads the OSGeo PostGIS bundle and merges its files in, which
     is all a PostGIS "install" actually is on Windows.
  3. Runs initdb on a fresh cluster under .localdb/data with trust
     authentication, listening on 127.0.0.1 only.
  4. Starts it on port 5433 -- not 5432 -- so it cannot collide with a
     system PostgreSQL, and creates the floodsafe database with the
     postgis extension enabled.

Trust auth is safe here only because the cluster listens on loopback
and holds nothing but public open data. Do not reuse this
configuration for anything reachable from a network.

Usage:
    python setup_local_db.py            # create and start
    python setup_local_db.py --start    # start an existing cluster
    python setup_local_db.py --stop
    python setup_local_db.py --status
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
import zipfile
from urllib.request import urlopen


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCALDB = os.path.join(REPO_ROOT, ".localdb")
PG_HOME = os.path.join(LOCALDB, "pg")
PG_DATA = os.path.join(LOCALDB, "data")
DOWNLOADS = os.path.join(LOCALDB, "downloads")

POSTGIS_URL = ("https://download.osgeo.org/postgis/windows/pg18/"
               "postgis-bundle-pg18-3.6.2x64.zip")

PORT = 5433
DB_NAME = "floodsafe"
SYSTEM_PG_CANDIDATES = [
    r"C:\Program Files\PostgreSQL\18",
    r"C:\Program Files\PostgreSQL\17",
    r"C:\Program Files\PostgreSQL\16",
]


def log(msg):
    print(f"[db] {msg}", flush=True)


def find_system_pg():
    for path in SYSTEM_PG_CANDIDATES:
        if os.path.isfile(os.path.join(path, "bin", "initdb.exe")):
            return path
    raise SystemExit(
        "No PostgreSQL installation found to copy binaries from.\n"
        "Install PostgreSQL, or use docker-compose.yml in this directory."
    )


def copy_binaries():
    if os.path.isfile(os.path.join(PG_HOME, "bin", "postgres.exe")):
        log("PostgreSQL binaries already staged")
        return

    src = find_system_pg()
    log(f"copying PostgreSQL binaries from {src} (read-only source)")
    os.makedirs(PG_HOME, exist_ok=True)

    for sub in ("bin", "lib", "share"):
        dst = os.path.join(PG_HOME, sub)
        if os.path.isdir(dst):
            continue
        log(f"  {sub} ...")
        shutil.copytree(os.path.join(src, sub), dst)

    log("binaries staged")


def install_postgis():
    marker = os.path.join(PG_HOME, "share", "extension", "postgis.control")
    if os.path.isfile(marker):
        log("PostGIS already present")
        return

    os.makedirs(DOWNLOADS, exist_ok=True)
    archive = os.path.join(DOWNLOADS, os.path.basename(POSTGIS_URL))

    if not os.path.isfile(archive):
        log(f"downloading PostGIS bundle ({POSTGIS_URL.rsplit('/', 1)[-1]}) ...")
        tmp = archive + ".part"
        with urlopen(POSTGIS_URL, timeout=300) as r, open(tmp, "wb") as fh:
            shutil.copyfileobj(r, fh)
        os.replace(tmp, archive)
    log(f"bundle at {archive} ({os.path.getsize(archive) / 1e6:.0f} MB)")

    extract_dir = os.path.join(DOWNLOADS, "postgis")
    if not os.path.isdir(extract_dir):
        log("extracting ...")
        with zipfile.ZipFile(archive) as z:
            z.extractall(extract_dir)

    # The bundle contains a single top-level folder holding bin/lib/share
    # laid out exactly like a PostgreSQL install; merge it over ours.
    roots = [os.path.join(extract_dir, d) for d in os.listdir(extract_dir)]
    roots = [d for d in roots if os.path.isdir(os.path.join(d, "bin"))]
    if not roots:
        raise SystemExit(f"unexpected bundle layout under {extract_dir}")

    log(f"merging {os.path.basename(roots[0])} into the staged install")
    for sub in ("bin", "lib", "share"):
        src_sub = os.path.join(roots[0], sub)
        if os.path.isdir(src_sub):
            shutil.copytree(src_sub, os.path.join(PG_HOME, sub), dirs_exist_ok=True)

    if not os.path.isfile(marker):
        raise SystemExit("PostGIS merge finished but postgis.control is missing")
    log("PostGIS installed")


def pg_bin(name):
    return os.path.join(PG_HOME, "bin", name + ".exe")


def run(cmd, **kw):
    env = dict(os.environ)
    # Make sure the staged lib/ wins over any system PostgreSQL on PATH.
    env["PATH"] = os.path.join(PG_HOME, "bin") + os.pathsep + env.get("PATH", "")
    return subprocess.run(cmd, env=env, capture_output=True, text=True, **kw)


def init_cluster():
    if os.path.isfile(os.path.join(PG_DATA, "PG_VERSION")):
        log("cluster already initialised")
        return

    log(f"initdb -> {PG_DATA}")
    os.makedirs(PG_DATA, exist_ok=True)
    r = run([pg_bin("initdb"), "-D", PG_DATA, "-U", "postgres",
             "--auth=trust", "--encoding=UTF8", "--locale=C"])
    if r.returncode != 0:
        raise SystemExit(f"initdb failed:\n{r.stdout}\n{r.stderr}")

    # Loopback only. Trust auth is acceptable precisely because nothing
    # off this machine can reach the socket.
    with open(os.path.join(PG_DATA, "postgresql.conf"), "a", encoding="utf-8") as f:
        f.write(f"\nport = {PORT}\nlisten_addresses = '127.0.0.1'\n")

    log("cluster initialised")


def logfile():
    return os.path.join(LOCALDB, "postgres.log")


def start():
    r = run([pg_bin("pg_ctl"), "-D", PG_DATA, "-l", logfile(), "start", "-w"])
    if r.returncode != 0 and "already running" not in (r.stdout + r.stderr):
        raise SystemExit(f"failed to start:\n{r.stdout}\n{r.stderr}")
    log(f"server running on 127.0.0.1:{PORT}")


def stop():
    r = run([pg_bin("pg_ctl"), "-D", PG_DATA, "stop", "-m", "fast"])
    log(r.stdout.strip() or r.stderr.strip() or "stopped")


def status():
    r = run([pg_bin("pg_ctl"), "-D", PG_DATA, "status"])
    print(r.stdout.strip() or r.stderr.strip())
    return r.returncode == 0


def psql(sql, dbname="postgres"):
    return run([pg_bin("psql"), "-h", "127.0.0.1", "-p", str(PORT),
                "-U", "postgres", "-d", dbname, "-v", "ON_ERROR_STOP=1",
                "-c", sql])


def create_database():
    r = psql("SELECT 1 FROM pg_database WHERE datname = 'floodsafe'")
    if "1 row" not in r.stdout:
        log(f"creating database {DB_NAME}")
        r = psql(f"CREATE DATABASE {DB_NAME}")
        if r.returncode != 0:
            raise SystemExit(f"create database failed:\n{r.stderr}")
    else:
        log(f"database {DB_NAME} already exists")

    for ext in ("postgis", "postgis_raster"):
        r = psql(f"CREATE EXTENSION IF NOT EXISTS {ext}", DB_NAME)
        if r.returncode != 0:
            log(f"  WARNING: could not enable {ext}: {r.stderr.strip()[:200]}")
        else:
            log(f"  extension {ext} ready")

    r = psql("SELECT postgis_full_version()", DB_NAME)
    log(r.stdout.strip().splitlines()[2].strip() if r.returncode == 0 else r.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.stop:
        stop()
        return
    if args.status:
        sys.exit(0 if status() else 1)
    if args.start:
        start()
        return

    os.makedirs(LOCALDB, exist_ok=True)
    copy_binaries()
    install_postgis()
    init_cluster()
    start()
    time.sleep(1)
    create_database()

    log("")
    log("connection string:")
    log(f"  postgresql://postgres@127.0.0.1:{PORT}/{DB_NAME}")


if __name__ == "__main__":
    main()
