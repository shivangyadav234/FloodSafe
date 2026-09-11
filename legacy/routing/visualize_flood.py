import osmnx as ox
import matplotlib.pyplot as plt
from shapely.geometry import box, LineString


def create_simulated_flood():

    flood_polygon = box(
        77.4480,
        28.6720,
        77.4520,
        28.6760
    )

    return flood_polygon


def classify_roads(graph, flood_polygon):

    for u, v, key, data in graph.edges(
        keys=True,
        data=True
    ):

        geometry = data.get("geometry")

        if geometry is None:

            x1 = graph.nodes[u]["x"]
            y1 = graph.nodes[u]["y"]

            x2 = graph.nodes[v]["x"]
            y2 = graph.nodes[v]["y"]

            geometry = LineString([
                (x1, y1),
                (x2, y2)
            ])

        if not geometry.intersects(flood_polygon):

            status = "SAFE"

        else:

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

            if flood_ratio >= 0.5:

                status = "BLOCKED"

            else:

                status = "RISKY"

        data["flood_status"] = status

    return graph


def visualize(graph, flood_polygon):

    print()
    print("Creating flood map...")

    fig, ax = plt.subplots(
        figsize=(12, 12)
    )

    # Draw roads
    for u, v, key, data in graph.edges(
        keys=True,
        data=True
    ):

        geometry = data.get("geometry")

        if geometry is None:

            x1 = graph.nodes[u]["x"]
            y1 = graph.nodes[u]["y"]

            x2 = graph.nodes[v]["x"]
            y2 = graph.nodes[v]["y"]

            geometry = LineString([
                (x1, y1),
                (x2, y2)
            ])

        status = data.get(
            "flood_status",
            "SAFE"
        )

        if status == "SAFE":

            color = "green"

        elif status == "RISKY":

            color = "orange"

        elif status == "BLOCKED":

            color = "red"

        else:

            color = "gray"

        x, y = geometry.xy

        ax.plot(
            x,
            y,
            color=color,
            linewidth=0.8
        )

    # Draw flood polygon
    x, y = flood_polygon.exterior.xy

    ax.fill(
        x,
        y,
        color="blue",
        alpha=0.25
    )

    ax.plot(
        x,
        y,
        color="blue",
        linewidth=2
    )

    ax.set_title(
        "FloodSafe - Flood Risk Road Map"
    )

    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")

    ax.grid(True)

    # Legend
    ax.plot(
        [],
        [],
        color="green",
        label="SAFE"
    )

    ax.plot(
        [],
        [],
        color="orange",
        label="RISKY"
    )

    ax.plot(
        [],
        [],
        color="red",
        label="BLOCKED"
    )

    ax.fill(
        [],
        [],
        color="blue",
        alpha=0.25,
        label="Flood Zone"
    )

    ax.legend()

    plt.tight_layout()

    output_file = "data/flood_map.png"

    plt.savefig(
        output_file,
        dpi=200
    )

    print()
    print("Map saved to:")
    print(output_file)

    plt.show()


if __name__ == "__main__":

    place = "Ghaziabad, Uttar Pradesh, India"

    print("Downloading road network...")

    graph = ox.graph_from_place(
        place,
        network_type="drive"
    )

    print("Road network downloaded!")

    print("Nodes:", len(graph.nodes))
    print("Edges:", len(graph.edges))

    flood_polygon = create_simulated_flood()

    graph = classify_roads(
        graph,
        flood_polygon
    )

    visualize(
        graph,
        flood_polygon
    )