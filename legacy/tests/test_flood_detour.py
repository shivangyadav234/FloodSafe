import osmnx as ox
import math
import heapq

from shapely.geometry import box, LineString


def heuristic(graph, a, b):

    lat1 = graph.nodes[a]["y"]
    lon1 = graph.nodes[a]["x"]

    lat2 = graph.nodes[b]["y"]
    lon2 = graph.nodes[b]["x"]

    lat_meters = (lat1 - lat2) * 111_000

    lon_meters = (
        (lon1 - lon2)
        * 111_000
        * math.cos(
            math.radians(
                (lat1 + lat2) / 2
            )
        )
    )

    distance = math.sqrt(
        lat_meters ** 2 +
        lon_meters ** 2
    )

    max_speed_mps = 130 / 3.6

    return distance / max_speed_mps


def classify_roads(graph, flood_polygon):

    for u, v, key, data in graph.edges(
        keys=True,
        data=True
    ):

        geometry = data.get("geometry")

        if geometry is None:

            geometry = LineString([
                (
                    graph.nodes[u]["x"],
                    graph.nodes[u]["y"]
                ),
                (
                    graph.nodes[v]["x"],
                    graph.nodes[v]["y"]
                )
            ])

        if geometry.intersects(flood_polygon):

            data["flood_status"] = "BLOCKED"

        else:

            data["flood_status"] = "SAFE"

    return graph


def convert_graph(graph):

    astar_graph = {}

    for node in graph.nodes:

        astar_graph[node] = {}

    for u, v, key, data in graph.edges(
        keys=True,
        data=True
    ):

        travel_time = data.get(
            "travel_time",
            data.get("length", 1)
        )

        status = data.get(
            "flood_status",
            "SAFE"
        )

        # Keep fastest parallel edge
        if v not in astar_graph[u]:

            astar_graph[u][v] = {
                "time": travel_time,
                "status": status
            }

        elif travel_time < astar_graph[u][v]["time"]:

            astar_graph[u][v] = {
                "time": travel_time,
                "status": status
            }

    return astar_graph


def astar(
    graph,
    astar_graph,
    start,
    goal,
    flood_aware=False
):

    open_list = []

    heapq.heappush(
        open_list,
        (
            heuristic(graph, start, goal),
            0,
            start,
            [start]
        )
    )

    visited = set()

    while open_list:

        f, g, current, path = heapq.heappop(
            open_list
        )

        if current in visited:
            continue

        visited.add(current)

        if current == goal:

            return path, g

        for neighbour, edge in astar_graph.get(
            current,
            {}
        ).items():

            if neighbour in visited:
                continue

            status = edge["status"]

            travel_time = edge["time"]

            # Normal A*
            if not flood_aware:

                edge_cost = travel_time

            # FloodSafe A*
            else:

                if status == "BLOCKED":

                    continue

                edge_cost = travel_time

            new_g = g + edge_cost

            new_f = (
                new_g
                + heuristic(
                    graph,
                    neighbour,
                    goal
                )
            )

            heapq.heappush(
                open_list,
                (
                    new_f,
                    new_g,
                    neighbour,
                    path + [neighbour]
                )
            )

    return None, float("inf")


if __name__ == "__main__":

    place = "Ghaziabad, Uttar Pradesh, India"

    print("Downloading road network...")

    graph = ox.graph_from_place(
        place,
        network_type="drive"
    )

    graph = ox.add_edge_speeds(graph)
    graph = ox.add_edge_travel_times(graph)

    print("Nodes:", len(graph.nodes))
    print("Edges:", len(graph.edges))

    # ------------------------------------------------
    # Start and destination
    # ------------------------------------------------

    start_lat = 28.6692
    start_lon = 77.4538

    end_lat = 28.6810
    end_lon = 77.4470

    start = ox.distance.nearest_nodes(
        graph,
        X=start_lon,
        Y=start_lat
    )

    goal = ox.distance.nearest_nodes(
        graph,
        X=end_lon,
        Y=end_lat
    )

    print()
    print("Start:", start)
    print("Goal :", goal)

    # ------------------------------------------------
    # First find normal route
    # ------------------------------------------------

    empty_flood = box(
        77.0,
        28.0,
        77.1,
        28.1
    )

    graph = classify_roads(
        graph,
        empty_flood
    )

    astar_graph = convert_graph(
        graph
    )

    normal_path, normal_cost = astar(
        graph,
        astar_graph,
        start,
        goal,
        flood_aware=False
    )

    print()
    print("NORMAL A*")
    print("Nodes:", len(normal_path))
    print("Cost :", normal_cost, "seconds")

    # ------------------------------------------------
    # Create flood around the middle of normal route
    # ------------------------------------------------

    middle_index = len(normal_path) // 2

    middle_node = normal_path[middle_index]

    flood_lat = graph.nodes[middle_node]["y"]
    flood_lon = graph.nodes[middle_node]["x"]

    print()
    print("Creating flood around normal route...")
    print(
        "Flood center:",
        flood_lat,
        flood_lon
    )

    flood_polygon = box(
        flood_lon - 0.0008,
        flood_lat - 0.0008,
        flood_lon + 0.0008,
        flood_lat + 0.0008
    )

    # ------------------------------------------------
    # Classify roads again
    # ------------------------------------------------

    graph = classify_roads(
        graph,
        flood_polygon
    )

    astar_graph = convert_graph(
        graph
    )

    # ------------------------------------------------
    # FloodSafe route
    # ------------------------------------------------

    safe_path, safe_cost = astar(
        graph,
        astar_graph,
        start,
        goal,
        flood_aware=True
    )

    print()
    print("FLOODSAFE A*")

    if safe_path:

        print(
            "Nodes:",
            len(safe_path)
        )

        print(
            "Cost:",
            safe_cost,
            "seconds"
        )

        print()
        print("FloodSafe found an alternate route!")

    else:

        print(
            "No safe route available."
        )