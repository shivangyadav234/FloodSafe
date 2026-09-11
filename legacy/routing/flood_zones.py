import osmnx as ox
from shapely.geometry import box, LineString


def download_road_graph(place):

    print("Downloading road network...")

    graph = ox.graph_from_place(
        place,
        network_type="drive"
    )

    print("Road network downloaded!")
    print("Nodes:", len(graph.nodes))
    print("Edges:", len(graph.edges))

    return graph


def create_simulated_flood():

    # Simulated flood area around central Ghaziabad
    #
    # min longitude
    # min latitude
    # max longitude
    # max latitude

    flood_polygon = box(
        77.4480,
        28.6720,
        77.4520,
        28.6760
    )

    return flood_polygon


def classify_roads(graph, flood_polygon):

    print()
    print("Checking roads against flood zone...")

    safe_count = 0
    risky_count = 0
    blocked_count = 0

    for u, v, key, data in graph.edges(
        keys=True,
        data=True
    ):

        # Get road geometry
        geometry = data.get("geometry")

        # Some OSM edges may not have geometry
        if geometry is None:

            x1 = graph.nodes[u]["x"]
            y1 = graph.nodes[u]["y"]

            x2 = graph.nodes[v]["x"]
            y2 = graph.nodes[v]["y"]

            geometry = LineString([
                (x1, y1),
                (x2, y2)
            ])

        # Check whether road intersects flood
        if not geometry.intersects(flood_polygon):

            status = "SAFE"

            safe_count += 1

        else:

            # Calculate how much of the road is inside
            # the flood polygon

            intersection = geometry.intersection(
                flood_polygon
            )

            road_length = geometry.length

            flooded_length = intersection.length

            if road_length == 0:

                flood_ratio = 0

            else:

                flood_ratio = (
                    flooded_length / road_length
                )

            # If more than half of the road is flooded
            # consider it BLOCKED

            if flood_ratio >= 0.5:

                status = "BLOCKED"

                blocked_count += 1

            else:

                status = "RISKY"

                risky_count += 1

        # Store status directly on the OSM edge
        data["flood_status"] = status

    print()
    print("Flood analysis complete!")

    print()
    print("SAFE roads   :", safe_count)
    print("RISKY roads  :", risky_count)
    print("BLOCKED roads:", blocked_count)

    return graph


if __name__ == "__main__":

    place = "Ghaziabad, Uttar Pradesh, India"

    # Step 1: Download roads
    graph = download_road_graph(place)

    # Step 2: Create simulated flood
    print()
    print("Creating simulated flood zone...")

    flood_polygon = create_simulated_flood()

    print("Flood zone created.")

    # Step 3: Classify roads
    graph = classify_roads(
        graph,
        flood_polygon
    )

    print()
    print("Sample road results:")
    print()

    count = 0

    for u, v, key, data in graph.edges(
        keys=True,
        data=True
    ):

        print(
            "Road:",
            u,
            "->",
            v,
            "| Status:",
            data["flood_status"]
        )

        count += 1

        if count >= 20:
            break