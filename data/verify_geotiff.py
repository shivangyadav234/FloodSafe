import rasterio


FILE = (
    "data/flood/real/"
    "ghaziabad_hazard_map.tif"
)


with rasterio.open(FILE) as src:

    print("CRS:", src.crs)

    print()
    print("Bounds:")
    print(src.bounds)

    print()
    print("Width :", src.width)
    print("Height:", src.height)

    print()
    print("Transform:")
    print(src.transform)