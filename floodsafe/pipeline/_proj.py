"""
PROJ database bootstrap. Import this before pyproj, geopandas or
anything that pulls them in.

pyproj in this environment ships a proj_dir that its own loader cannot
open ("no database context specified"), so CRS lookups fail even though
rasterio's bundled GDAL resolves them fine. The conda environment's
Library/share/proj holds a working proj.db, but pointing PROJ_DATA at it
is not enough on its own -- pyproj.datadir.set_data_dir() has to be
called explicitly as well.

Only import this where pyproj or geopandas is actually used. The
override is not free: pointing PROJ_DATA at the conda database makes
rasterio's bundled GDAL abort the process on less common projections
(Interrupted Goode Homolosine, as used by SoilGrids). Modules that only
touch rasterio should transform through rasterio.warp instead and leave
this alone.

Set FLOODSAFE_PROJ_DATA to override the directory.
"""

import os
import sys

_CANDIDATES = [
    os.environ.get("FLOODSAFE_PROJ_DATA"),
    os.environ.get("PROJ_DATA_OVERRIDE"),
    os.path.join(sys.prefix, "Library", "share", "proj"),
]


def _apply():
    for path in _CANDIDATES:
        if not path or not os.path.isfile(os.path.join(path, "proj.db")):
            continue

        os.environ["PROJ_DATA"] = path
        os.environ["PROJ_LIB"] = path

        try:
            import pyproj
            pyproj.datadir.set_data_dir(path)
            # Prove it actually resolves before declaring success -- a
            # present proj.db from a mismatched PROJ build still fails.
            pyproj.CRS.from_user_input("EPSG:4326")
            return path
        except Exception:
            continue

    return None


PROJ_DATA_DIR = _apply()
