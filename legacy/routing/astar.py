import heapq


def a_star(graph, start, goal, heuristic):

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

        if current == goal:
            return path, g

        for neighbour, cost in graph.get(current, {}).items():

            if neighbour in visited:
                continue

            new_g = g + cost

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