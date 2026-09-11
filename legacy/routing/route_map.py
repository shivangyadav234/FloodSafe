import osmnx as ox
import matplotlib.pyplot as plt
from shapely.geometry import box, LineString
import math
import heapq


# --------------------------------------------------
# HEURISTIC
# --------------------------------------------------

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


# --------------------------------------------------
# CLASSIFY ROADS
# --------------------------------------------------

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


# --------------------------------------------------
# CONVERT GRAPH
# --------------------------------------------------

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


# --------------------------------------------------
# FLOODSAFE A*
# --------------------------------------------------

def floodsafe_astar(
    graph,
    astar_graph,
    start,
    goal
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

            # Never use flooded road
            if edge["status"] == "BLOCKED":

                continue

            new_g = (
                g + edge["time"]
            )

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


# --------------------------------------------------
# DRAW FLOODSAFE MAP
# --------------------------------------------------

def draw_map(
    graph,
    flood_polygon,
    route,
    start,
    goal
):

    print()
    print("Creating FloodSafe map...")

    fig, ax = plt.subplots(
        figsize=(14, 14)
    )

    # ----------------------------------------------
    # Draw roads
    # ----------------------------------------------

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

        status = data.get(
            "flood_status",
            "SAFE"
        )

        if status == "BLOCKED":

            road_color = "red"
            width = 1.2

        else:

            road_color = "green"
            width = 0.5

        x, y = geometry.xy

        ax.plot(
            x,
            y,
            color=road_color,
            linewidth=width,
            zorder=1
        )

    # ----------------------------------------------
    # Draw flood zone
    # ----------------------------------------------

    x, y = flood_polygon.exterior.xy

    ax.fill(
        x,
        y,
        color="blue",
        alpha=0.30,
        zorder=2
    )

    ax.plot(
        x,
        y,
        color="blue",
        linewidth=2,
        zorder=3
    )

    # ----------------------------------------------
    # Draw selected route
    # ----------------------------------------------

    if route:

        route_x = []
        route_y = []

        for node in route:

            route_x.append(
                graph.nodes[node]["x"]
            )

            route_y.append(
                graph.nodes[node]["y"]
            )

        ax.plot(
            route_x,
            route_y,
            color="purple",
            linewidth=4,
            zorder=5,
            label="FloodSafe Route"
        )

    # ----------------------------------------------
    # Start point
    # ----------------------------------------------

    ax.scatter(
        graph.nodes[start]["x"],
        graph.nodes[start]["y"],
        s=150,
        color="yellow",
        edgecolors="black",
        zorder=6,
        label="Start"
    )

    # ----------------------------------------------
    # Destination
    # ----------------------------------------------

    ax.scatter(
        graph.nodes[goal]["x"],
        graph.nodes[goal]["y"],
        s=180,
        color="cyan",
        edgecolors="black",
        zorder=6,
        label="Destination"
    )

    # ----------------------------------------------
    # Labels
    # ----------------------------------------------

    ax.set_title(
        "FloodSafe - Flood Aware Navigation",
        fontsize=18
    )

    ax.set_xlabel(
        "Longitude"
    )

    ax.set_ylabel(
        "Latitude"
    )

    ax.legend()

    ax.grid(True)

    plt.tight_layout()

    output_file = (
        "data/floodsafe_route.png"
    )

    plt.savefig(
        output_file,
        dpi=200
    )

    print()
    print("Map saved to:")
    print(output_file)

    plt.show()


# --------------------------------------------------
# MAIN
# --------------------------------------------------

if __name__ == "__main__":

    place = (
        "Ghaziabad, Uttar Pradesh, India"
    )

    print(
        "Downloading road network..."
    )

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

    print(
        "Nodes:",
        len(graph.nodes)
    )

    print(
        "Edges:",
        len(graph.edges)
    )

    # ----------------------------------------------
    # Start / destination
    # ----------------------------------------------

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

    # ----------------------------------------------
    # First find normal route
    # ----------------------------------------------

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

    normal_route, normal_cost = floodsafe_astar(
        graph,
        astar_graph,
        start,
        goal
    )

    # ----------------------------------------------
    # Put flood around normal route
    # ----------------------------------------------

    middle_index = (
        len(normal_route) // 2
    )

    middle_node = (
        normal_route[middle_index]
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

    # ----------------------------------------------
    # Re-classify roads
    # ----------------------------------------------

    graph = classify_roads(
        graph,
        flood_polygon
    )

    astar_graph = convert_graph(
        graph
    )

    # ----------------------------------------------
    # Find FloodSafe route
    # ----------------------------------------------

    route, cost = floodsafe_astar(
        graph,
        astar_graph,
        start,
        goal
    )

    if route:

        print()
        print(
            "FloodSafe route found!"
        )

        print(
            "Route nodes:",
            len(route)
        )

        print(
            "Route cost:",
            cost,
            "seconds"
        )

        # ------------------------------------------
        # Draw everything
        # ------------------------------------------

        draw_map(
            graph,
            flood_polygon,
            route,
            start,
            goal
        )

    else:

        print(
            "No safe route found!"
        )