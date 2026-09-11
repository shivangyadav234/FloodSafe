import osmnx as ox


def download_road_graph(place):

    print("Downloading road network...")

    graph = ox.graph_from_place(
        place,
        network_type="drive"
    )

    print("Road network downloaded!")

    print("Number of nodes:", len(graph.nodes))
    print("Number of roads:", len(graph.edges))

    return graph


if __name__ == "__main__":

    place = "Ghaziabad, Uttar Pradesh, India"

    graph = download_road_graph(place)

    ox.plot_graph(
        graph,
        node_size=5,
        edge_linewidth=0.5
    )