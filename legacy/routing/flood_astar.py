import heapq


def heuristic(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def flood_aware_a_star(
    graph,
    flood_risk,
    road_status,
    start,
    goal,
    flood_penalty=10
):

    open_list = []

    heapq.heappush(
        open_list,
        (
            heuristic(start, goal),
            0,
            start,
            [start]
        )
    )

    visited = set()

    while open_list:

        f, g, current, path = heapq.heappop(open_list)

        if current in visited:
            continue

        visited.add(current)

        # Goal reached
        if current == goal:
            return path, g

        for neighbour, road_cost in graph.get(current, {}).items():

            if neighbour in visited:
                continue

            # --------------------------------
            # 1. CHECK ROAD STATUS
            # --------------------------------

            status = road_status.get(neighbour, "UNKNOWN")

            # Completely block flooded/closed roads
            if status == "BLOCKED":
                continue

            # --------------------------------
            # 2. GET FLOOD RISK
            # --------------------------------

            risk = flood_risk.get(neighbour, 0)

            # --------------------------------
            # 3. CALCULATE FLOOD PENALTY
            # --------------------------------

            if status == "RISKY":
                flood_cost = road_cost * (
                    1 + flood_penalty * risk
                )

            elif status == "SAFE":
                flood_cost = road_cost

            else:
                # UNKNOWN should not be treated as perfectly safe
                flood_cost = road_cost * 1.5

            # --------------------------------
            # 4. NEW TOTAL COST
            # --------------------------------

            new_g = g + flood_cost

            new_f = new_g + heuristic(
                neighbour,
                goal
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