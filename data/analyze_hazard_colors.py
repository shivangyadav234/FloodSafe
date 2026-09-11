import rasterio
from collections import Counter
import numpy as np


FILE = r"data\flood\real\ghaziabad_hazard_map.tif"


print("Loading georeferenced map...")

with rasterio.open(FILE) as src:

    img = src.read()

    # Convert:
    # (3, height, width)
    # to:
    # (pixels, 3)

    pixels = np.moveaxis(img, 0, -1).reshape(-1, 3)


# --------------------------------------------------
# Remove grayscale pixels
# --------------------------------------------------

# Flood colors are colored.
# Roads/text/borders are mostly grayscale.

colored = pixels[
    ~(
        (pixels[:, 0] == pixels[:, 1]) &
        (pixels[:, 1] == pixels[:, 2])
    )
]


print()
print("Total pixels   :", len(pixels))
print("Colored pixels :", len(colored))


# --------------------------------------------------
# Quantize colors
# --------------------------------------------------

# Reduce tiny differences caused by anti-aliasing.

quantized = (
    (colored // 10) * 10
).astype(np.uint8)


counter = Counter(
    map(tuple, quantized)
)


print()
print("MOST COMMON COLORED PIXELS")
print("---------------------------")


for color, count in counter.most_common(30):

    r, g, b = color

    print(
        f"RGB({r:3d}, {g:3d}, {b:3d})"
        f"  Pixels: {count}"
    )