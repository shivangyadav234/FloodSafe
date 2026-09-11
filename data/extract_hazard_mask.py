import rasterio
import numpy as np
from PIL import Image


INPUT = r"data\flood\real\ghaziabad_hazard_map.tif"

OUTPUT = r"data\flood\real\ghaziabad_hazard_mask.png"


print("Loading georeferenced map...")

with rasterio.open(INPUT) as src:
    img = src.read()
    
    # RGB
    rgb = np.moveaxis(img, 0, -1)


# ----------------------------------------------------
# HAZARD COLORS
# ----------------------------------------------------

# Very Low
VERY_LOW = np.array([250, 230, 170])

# Low
LOW = np.array([250, 210, 120])


# ----------------------------------------------------
# COLOR DISTANCE
# ----------------------------------------------------

def color_distance(image, target):

    return np.sqrt(
        np.sum(
            (image.astype(float) - target) ** 2,
            axis=2
        )
    )


very_low_distance = color_distance(
    rgb,
    VERY_LOW
)

low_distance = color_distance(
    rgb,
    LOW
)


# Tolerance for PDF anti-aliasing
TOLERANCE = 35


very_low_mask = (
    very_low_distance < TOLERANCE
)

low_mask = (
    low_distance < TOLERANCE
)


# Combined hazard
hazard_mask = (
    very_low_mask |
    low_mask
)


print()
print("HAZARD PIXELS")
print("----------------")

print(
    "Very Low pixels:",
    np.sum(very_low_mask)
)

print(
    "Low pixels:",
    np.sum(low_mask)
)

print(
    "Total hazard pixels:",
    np.sum(hazard_mask)
)


# ----------------------------------------------------
# CREATE DIAGNOSTIC IMAGE
# ----------------------------------------------------

output = np.zeros(
    (rgb.shape[0], rgb.shape[1], 3),
    dtype=np.uint8
)


# Very Low = yellow
output[very_low_mask] = [255, 255, 0]

# Low = orange
output[low_mask] = [255, 140, 0]


Image.fromarray(output).save(
    OUTPUT
)


print()
print("SUCCESS!")
print("Saved:")
print(OUTPUT)