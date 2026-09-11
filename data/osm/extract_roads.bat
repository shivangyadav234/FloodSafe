@echo off

osmium tags-filter ^
data\osm\northern\northern-zone-260904.osm.pbf ^
w/highway ^
-o data\osm\northern\northern_roads.osm.pbf ^
--overwrite

echo.
echo Road extraction complete.
pause