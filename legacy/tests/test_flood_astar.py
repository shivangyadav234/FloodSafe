from routing.flood_astar import flood_aware_a_star
from routing.visualize import draw_map
graph = {

    (0, 0): {
        (1, 0): 1,
        (0, 1): 1
    },

    (1, 0): {
        (0, 0): 1,
        (2, 0): 1,
        (1, 1): 1
    },

    (0, 1): {
        (0, 0): 1,
        (1, 1): 1
    },

    (1, 1): {
        (1, 0): 1,
        (0, 1): 1,
        (2, 1): 1
    },

    (2, 0): {
        (1, 0): 1,
        (2, 1): 1
    },

    (2, 1): {
        (2, 0): 1,
        (1, 1): 1,
        (2, 2): 1
    },

    (2, 2): {
        (2, 1): 1
    }
}


# --------------------------------
# FLOOD RISK
# --------------------------------

flood_risk = {

    (0, 0): 0.0,
    (1, 0): 0.0,
    (0, 1): 0.0,

    # High flood risk
    (1, 1): 0.9,

    (2, 0): 0.0,
    (2, 1): 0.0,
    (2, 2): 0.0
}


# --------------------------------
# ROAD STATUS
# --------------------------------

road_status = {

    (0, 0): "SAFE",
    (1, 0): "SAFE",
    (0, 1): "SAFE",

    # This road is completely flooded
    (1, 1): "BLOCKED",

    (2, 0): "SAFE",
    (2, 1): "SAFE",
    (2, 2): "SAFE"
}


# --------------------------------
# START / GOAL
# --------------------------------

start = (0, 0)
goal = (2, 2)


# --------------------------------
# RUN FLOOD-AWARE A*
# --------------------------------

path, cost = flood_aware_a_star(
    graph,
    flood_risk,
    road_status,
    start,
    goal
)


print("Flood-Aware Path:", path)
print("Total Cost:", cost)

draw_map(
    graph,
    road_status,
    path,
    start,
    goal
)