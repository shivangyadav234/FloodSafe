import osmnx as ox
import math
import heapq

from shapely.geometry import box, LineString

from routing.flood_risk import calculate_flood_risk


LAMBDA = 10.0


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


def get_geometry(graph, u, v, data):

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

    return geometry


def classify_roads(graph, flood_polygon):

    safe = 0
    risky = 0
    blocked = 0

    for u, v, key, data in graph.edges(
        keys=True,
        data=True
    ):

        geometry = get_geometry(
            graph,
            u,
            v,
            data
        )

        road_length = geometry.length

        if road_length == 0:

            flood_ratio = 0.0

        else:

            intersection = geometry.intersection(
                flood_polygon
            )

            flood_ratio = (
                intersection.length /
                road_length
            )

            flood_ratio = min(
                max(flood_ratio, 0.0),
                1.0
            )

        # Prototype depth/velocity.
        # Later these will come from real flood data.

        if flood_ratio == 0:

            depth = 0.0
            velocity = 0.0

        else:

            depth = 0.5
            velocity = 0.8

        risk = calculate_flood_risk(
            flood_ratio,
            depth,
            velocity
        )

        if risk >= 0.80:

            status = "BLOCKED"
            blocked += 1

        elif risk >= 0.20:

            status = "RISKY"
            risky += 1

        else:

            status = "SAFE"
            safe += 1

        data["flood_ratio"] = flood_ratio
        data["flood_risk"] = risk
        data["flood_status"] = status

    print()
    print("Flood classification:")
    print("SAFE   :", safe)
    print("RISKY  :", risky)
    print("BLOCKED:", blocked)

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

        risk = data.get(
            "flood_risk",
            0.0
        )

        status = data.get(
            "flood_status",
            "SAFE"
        )

        if v not in astar_graph[u]:

            astar_graph[u][v] = {
                "time": travel_time,
                "risk": risk,
                "status": status
            }

        else:

            if travel_time < astar_graph[u][v]["time"]:

                astar_graph[u][v] = {
                    "time": travel_time,
                    "risk": risk,
                    "status": status
                }

    return astar_graph


def risk_aware_astar(
    graph,
    astar_graph,
    start,
    goal
):

    open_list = []

    heapq.heappush(
        open_list,
        (
            heuristic(
                graph,
                start,
                goal
            ),
            0.0,
            start,
            [start]
        )
    )

    best_cost = {
        start: 0.0
    }

    while open_list:

        f, g, current, path = heapq.heappop(
            open_list
        )

        if current == goal:

            return path, g

        if g > best_cost.get(
            current,
            float("inf")
        ):

            continue

        for neighbour, edge in astar_graph.get(
            current,
            {}
        ).items():

            if edge["status"] == "BLOCKED":

                continue

            travel_time = edge["time"]
            risk = edge["risk"]

            # ------------------------------------
            # Risk-aware edge cost
            # ------------------------------------

            edge_cost = (
                travel_time
                * (
                    1
                    + LAMBDA * risk
                )
            )

            new_g = g + edge_cost

            if new_g >= best_cost.get(
                neighbour,
                float("inf")
            ):

                continue

            best_cost[neighbour] = new_g

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

    graph = ox.add_edge_speeds(
        graph
    )

    graph = ox.add_edge_travel_times(
        graph
    )

    print("Road network downloaded!")

    print(
        "Nodes:",
        len(graph.nodes)
    )

    print(
        "Edges:",
        len(graph.edges)
    )

    # --------------------------------------------
    # Start / destination
    # --------------------------------------------

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

    # --------------------------------------------
    # Find normal route first
    # --------------------------------------------

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

    normal_path, normal_cost = risk_aware_astar(
        graph,
        astar_graph,
        start,
        goal
    )

    print()
    print("NORMAL ROUTE")

    print(
        "Nodes:",
        len(normal_path)
    )

    print(
        "Travel cost:",
        normal_cost,
        "seconds"
    )

    # --------------------------------------------
    # Flood around middle of normal route
    # --------------------------------------------

    middle_index = (
        len(normal_path) // 2
    )

    middle_node = (
        normal_path[middle_index]
    )

    flood_lat = graph.nodes[
        middle_node
    ]["y"]

    flood_lon = graph.nodes[
        middle_node
    ]["x"]

    print()
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

    # --------------------------------------------
    # Recalculate risk
    # --------------------------------------------

    graph = classify_roads(
        graph,
        flood_polygon
    )

    astar_graph = convert_graph(
        graph
    )

    # --------------------------------------------
    # Risk-aware route
    # --------------------------------------------

    safe_path, safe_cost = risk_aware_astar(
        graph,
        astar_graph,
        start,
        goal
    )

    print()
    print("FLOODSAFE RISK-AWARE ROUTE")

    if safe_path:

        print(
            "Nodes:",
            len(safe_path)
        )

        print(
            "Risk-adjusted cost:",
            safe_cost,
            "seconds"
        )

        print()
        print(
            "FloodSafe selected a "
            "risk-aware route!"
        )

    else:

        print(
            "No safe route found."
        )