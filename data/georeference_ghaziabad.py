import pymupdf
import numpy as np
import rasterio
from rasterio.transform import from_origin
import os


PDF_FILE = (
    r"data\flood\real"
    r"\Flood Hazard Atlas of Uttar Pradesh.pdf"
)

OUTPUT_FILE = (
    r"data\flood\real"
    r"\ghaziabad_hazard_map.tif"
)


print("Opening Flood Hazard Atlas...")

pdf = pymupdf.open(PDF_FILE)

# Page 100 in the PDF = index 99
page = pdf[99]

print("Rendering page 100...")

pix = page.get_pixmap(
    matrix=pymupdf.Matrix(2.5, 2.5),
    alpha=False
)

img = np.frombuffer(
    pix.samples,
    dtype=np.uint8
).reshape(
    pix.height,
    pix.width,
    3
)

print(
    "Rendered image:",
    pix.width,
    "x",
    pix.height
)


# ---------------------------------------------------------
# MAP FRAME
# ---------------------------------------------------------
#
# These coordinates correspond to the actual map frame
# visible on page 100.
#
# The page contains:
#
# 77°20'E
# 77°40'E
# 78°00'E
#
# 29°00'N
# 28°40'N
# 28°20'N
#
# ---------------------------------------------------------

# Pixel coordinates at 2.5x rendering.
#
# Left/right edges of the actual geographic map frame
# and top/bottom edges.

X_MIN = 410
X_MAX = 1890

Y_MIN = 325
Y_MAX = 2025


map_img = img[
    Y_MIN:Y_MAX,
    X_MIN:X_MAX
]

height, width, _ = map_img.shape

print(
    "Map image:",
    width,
    "x",
    height
)


# ---------------------------------------------------------
# GEOREFERENCING
# ---------------------------------------------------------
#
# From the coordinate ticks on the map:
#
# 77°20'E = 77.333333
# 77°40'E = 77.666667
# 78°00'E = 78.000000
#
# 29°00'N
# 28°40'N
# 28°20'N
#
# Pixel positions of those ticks are used to determine
# the geographic transformation.
# ---------------------------------------------------------

# Full-page pixel positions of longitude ticks
lon_pixels = np.array([
    584,
    1066,
    1551
], dtype=float)

lon_values = np.array([
    77 + 20 / 60,
    77 + 40 / 60,
    78.0
])


# Full-page pixel positions of latitude ticks
lat_pixels = np.array([
    686,
    1218,
    1770
], dtype=float)

lat_values = np.array([
    29.0,
    28 + 40 / 60,
    28 + 20 / 60
])


# Linear transformation:
#
# longitude = a*x + b
# latitude  = c*y + d

lon_a, lon_b = np.polyfit(
    lon_pixels,
    lon_values,
    1
)

lat_a, lat_b = np.polyfit(
    lat_pixels,
    lat_values,
    1
)


# Geographic coordinate of the cropped image's
# top-left pixel.

top_left_lon = (
    lon_a * X_MIN + lon_b
)

top_left_lat = (
    lat_a * Y_MIN + lat_b
)


# Pixel resolution
pixel_width_lon = abs(lon_a)
pixel_height_lat = abs(lat_a)


transform = from_origin(
    top_left_lon,
    top_left_lat,
    pixel_width_lon,
    pixel_height_lat
)


os.makedirs(
    os.path.dirname(OUTPUT_FILE),
    exist_ok=True
)


print("Writing GeoTIFF...")

with rasterio.open(
    OUTPUT_FILE,
    "w",
    driver="GTiff",
    height=height,
    width=width,
    count=3,
    dtype="uint8",
    crs="EPSG:4326",
    transform=transform
) as dst:

    dst.write(
        map_img[:, :, 0],
        1
    )

    dst.write(
        map_img[:, :, 1],
        2
    )

    dst.write(
        map_img[:, :, 2],
        3
    )


print()
print("SUCCESS!")
print("--------------------------------")
print("Georeferenced map created:")
print(OUTPUT_FILE)

print()
print("Approximate geographic extent:")
print(
    "Longitude:",
    top_left_lon,
    "to",
    top_left_lon + pixel_width_lon * width
)

print(
    "Latitude:",
    top_left_lat - pixel_height_lat * height,
    "to",
    top_left_lat
)