import osmnx as ox
from shapely.geometry import box, LineString
import math
import heapq


# --------------------------------------------------
# 1. Download OSM road network
# --------------------------------------------------

def get_road_graph(place):

    print("Downloading road network...")

    graph = ox.graph_from_place(
        place,
        network_type="drive"
    )

    print("Road network downloaded!")
    print("Nodes:", len(graph.nodes))
    print("Edges:", len(graph.edges))

    graph = ox.add_edge_speeds(graph)
    graph = ox.add_edge_travel_times(graph)

    return graph


# --------------------------------------------------
# 2. Create simulated flood zone
# --------------------------------------------------

def create_flood_zone():

    flood_polygon = box(
        77.4480,
        28.6720,
        77.4520,
        28.6760
    )

    return flood_polygon


# --------------------------------------------------
# 3. Classify each road segment
# --------------------------------------------------

def classify_roads(graph, flood_polygon):

    safe = 0
    risky = 0
    blocked = 0

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

            safe += 1

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

                blocked += 1

            else:

                status = "RISKY"

                risky += 1

        data["flood_status"] = status

    print()
    print("Flood classification:")
    print("SAFE   :", safe)
    print("RISKY  :", risky)
    print("BLOCKED:", blocked)

    return graph


# --------------------------------------------------
# 4. Convert OSM graph to A* graph
# --------------------------------------------------

def convert_to_astar(graph):

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

        flood_status = data.get(
            "flood_status",
            "UNKNOWN"
        )

        if v not in astar_graph[u]:

            astar_graph[u][v] = {
                "time": travel_time,
                "status": flood_status
            }

        else:

            # Keep the faster parallel edge
            if travel_time < astar_graph[u][v]["time"]:

                astar_graph[u][v] = {
                    "time": travel_time,
                    "status": flood_status
                }

    return astar_graph


# --------------------------------------------------
# 5. Geographic heuristic
# --------------------------------------------------

def heuristic(graph, node_a, node_b):

    lat1 = graph.nodes[node_a]["y"]
    lon1 = graph.nodes[node_a]["x"]

    lat2 = graph.nodes[node_b]["y"]
    lon2 = graph.nodes[node_b]["x"]

    lat_meters = (
        lat1 - lat2
    ) * 111_000

    lon_meters = (
        lon1 - lon2
    ) * 111_000 * math.cos(
        math.radians(
            (lat1 + lat2) / 2
        )
    )

    distance_meters = math.sqrt(
        lat_meters ** 2 +
        lon_meters ** 2
    )

    # Conservative maximum speed
    max_speed_mps = 130 / 3.6

    return distance_meters / max_speed_mps


# --------------------------------------------------
# 6. Flood-aware A*
# --------------------------------------------------

def flood_aware_astar(
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

        for neighbour, edge_data in astar_graph.get(
            current,
            {}
        ).items():

            if neighbour in visited:
                continue

            travel_time = edge_data["time"]

            status = edge_data["status"]

            # --------------------------------------
            # BLOCKED = NEVER USE
            # --------------------------------------

            if status == "BLOCKED":

                continue

            # --------------------------------------
            # SAFE = normal travel time
            # --------------------------------------

            if status == "SAFE":

                edge_cost = travel_time

            # --------------------------------------
            # RISKY = strong penalty
            # --------------------------------------

            elif status == "RISKY":

                edge_cost = travel_time * 10

            # --------------------------------------
            # UNKNOWN = slight penalty
            # --------------------------------------

            else:

                edge_cost = travel_time * 2

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


# --------------------------------------------------
# 7. Main program
# --------------------------------------------------

if __name__ == "__main__":

    place = "Ghaziabad, Uttar Pradesh, India"

    # Download roads
    graph = get_road_graph(place)

    # Create simulated flood
    print()
    print("Creating simulated flood zone...")

    flood_polygon = create_flood_zone()

    print("Flood zone created.")

    # Classify roads
    graph = classify_roads(
        graph,
        flood_polygon
    )

    # Convert to A*
    print()
    print("Creating A* graph...")

    astar_graph = convert_to_astar(
        graph
    )

    print("A* graph created.")

    # Same start/end coordinates
    start_lat = 28.6692
    start_lon = 77.4538

    end_lat = 28.6810
    end_lon = 77.4470

    # Find nearest OSM nodes
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

    print()
    print("Start node:", start_node)
    print("Goal node:", goal_node)

    # Flood-aware route
    print()
    print("Finding flood-safe route...")

    path, cost = flood_aware_astar(
        graph,
        astar_graph,
        start_node,
        goal_node
    )

    print()

    if path:

        print("Flood-safe route found!")

        print(
            "Number of nodes:",
            len(path)
        )

        print(
            "Flood-aware cost:",
            cost,
            "seconds"
        )

        print()
        print("First 10 route nodes:")

        for node in path[:10]:

            print(node)

    else:

        print(
            "No safe route found."
        )