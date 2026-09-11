import osmnx as ox
import math

from routing.astar import a_star


def get_road_graph(place):

    print("Downloading road network...")

    graph = ox.graph_from_place(
        place,
        network_type="drive"
    )

    print("Roads downloaded.")
    print("Nodes:", len(graph.nodes))
    print("Edges:", len(graph.edges))

    graph = ox.add_edge_speeds(graph)
    graph = ox.add_edge_travel_times(graph)

    return graph


def convert_osm_to_astar(graph):

    astar_graph = {}

    for node in graph.nodes:
        astar_graph[node] = {}

    for u, v, data in graph.edges(data=True):

        travel_time = data.get(
            "travel_time",
            data.get("length", 1)
        )

        if v not in astar_graph[u]:

            astar_graph[u][v] = travel_time

        else:

            astar_graph[u][v] = min(
                astar_graph[u][v],
                travel_time
            )

    return astar_graph


def osm_heuristic(graph, node_a, node_b):

    lat1 = graph.nodes[node_a]["y"]
    lon1 = graph.nodes[node_a]["x"]

    lat2 = graph.nodes[node_b]["y"]
    lon2 = graph.nodes[node_b]["x"]

    # Convert latitude/longitude difference approximately to meters

    lat_meters = (lat1 - lat2) * 111_000

    lon_meters = (
        (lon1 - lon2)
        * 111_000
        * math.cos(math.radians((lat1 + lat2) / 2))
    )

    distance_meters = math.sqrt(
        lat_meters ** 2 +
        lon_meters ** 2
    )

    # Maximum assumed speed: 130 km/h
    # This makes the heuristic a lower bound
    max_speed_mps = 130 / 3.6

    minimum_time_seconds = (
        distance_meters / max_speed_mps
    )

    return minimum_time_seconds


def find_route(
    graph,
    astar_graph,
    start_lat,
    start_lon,
    end_lat,
    end_lon
):

    start_node = ox.distance.nearest_nodes(
        graph,
        X=start_lon,
        Y=start_lat
    )

    goal_node = ox.distance.nearest_nodes(
        graph,
        X=end_lon,
        Y=end_lat
    )

    print("Start node:", start_node)
    print("Goal node:", goal_node)

    path, cost = a_star(
        astar_graph,
        start_node,
        goal_node,
        lambda a, b: osm_heuristic(graph, a, b)
    )

    return path, cost


if __name__ == "__main__":

    place = "Ghaziabad, Uttar Pradesh, India"

    graph = get_road_graph(place)

    print()
    print("Converting OSM graph to A* graph...")

    astar_graph = convert_osm_to_astar(graph)

    print("A* graph created.")
    print("A* nodes:", len(astar_graph))

    start_lat = 28.6692
    start_lon = 77.4538

    end_lat = 28.6810
    end_lon = 77.4470

    print()
    print("Finding route...")

    path, cost = find_route(
        graph,
        astar_graph,
        start_lat,
        start_lon,
        end_lat,
        end_lon
    )

    print()

    if path:

        print("Route found!")
        print("Number of nodes:", len(path))
        print("Total cost:", cost, "seconds")

        print()
        print("First 10 route nodes:")

        for node in path[:10]:
            print(node)

    else:

        print("No route found.")