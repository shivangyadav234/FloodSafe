import matplotlib.pyplot as plt


def draw_map(graph, road_status, path, start, goal):

    plt.figure(figsize=(8, 8))

    # Draw all roads
    for node in graph:

        for neighbour in graph[node]:

            # Avoid drawing same connection twice
            if node > neighbour:
                continue

            x1, y1 = node
            x2, y2 = neighbour

            status = road_status.get(neighbour, "UNKNOWN")

            if status == "SAFE":
                color = "green"

            elif status == "RISKY":
                color = "orange"

            elif status == "BLOCKED":
                color = "red"

            else:
                color = "gray"

            plt.plot(
                [x1, x2],
                [y1, y2],
                color=color,
                linewidth=4
            )

    # Draw selected route
    if path:

        path_x = [node[0] for node in path]
        path_y = [node[1] for node in path]

        plt.plot(
            path_x,
            path_y,
            color="blue",
            linewidth=6,
            label="Selected Route"
        )

    # Start point
    plt.scatter(
        start[0],
        start[1],
        s=200,
        color="blue",
        label="Start",
        zorder=5
    )

    # Destination
    plt.scatter(
        goal[0],
        goal[1],
        s=200,
        color="purple",
        label="Destination",
        zorder=5
    )

    # Node labels
    for node in graph:

        plt.text(
            node[0] + 0.05,
            node[1] + 0.05,
            str(node),
            fontsize=9
        )

    plt.title("FloodSafe - Flood Aware Route")

    plt.xlabel("X")
    plt.ylabel("Y")

    plt.grid(True)

    plt.legend()

    plt.show()