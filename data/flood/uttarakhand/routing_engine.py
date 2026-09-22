import os
import json
import math

# ============================================================
# PROJ CONFIGURATION
#
# Only override pyproj's data dir if the caller explicitly set
# PROJ_DATA_OVERRIDE and that path actually exists. Otherwise use
# whatever pyproj ships with — this is what lets the same code run
# unmodified on any teammate's machine or a judge's laptop.
# ============================================================

_PROJ_OVERRIDE = os.environ.get("PROJ_DATA_OVERRIDE")

if _PROJ_OVERRIDE and os.path.isdir(_PROJ_OVERRIDE):
    os.environ["PROJ_DATA"] = _PROJ_OVERRIDE
    os.environ["PROJ_LIB"] = _PROJ_OVERRIDE

import pyproj

if _PROJ_OVERRIDE and os.path.isdir(_PROJ_OVERRIDE):
    pyproj.datadir.set_data_dir(_PROJ_OVERRIDE)


# ============================================================
# IMPORTS
# ============================================================

import numpy as np

from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree


# ============================================================
# FILE PATHS
#
# All data lives under FLOODSAFE_DATA_DIR (defaults to a
# "data" folder next to this file), so the project runs the
# same way on any machine — no hardcoded drive letters.
# ============================================================

DATA_DIR = os.environ.get(
    "FLOODSAFE_DATA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
)

GRAPH_FILE = os.path.join(DATA_DIR, "roads.npz")
COORDINATE_FILE = os.path.join(DATA_DIR, "coordinates.npy")
RISK_FILE = os.path.join(DATA_DIR, "road_flood_risk.npz")
SHELTER_FILE = os.path.join(DATA_DIR, "shelters.json")


# ============================================================
# LOAD ROUTING GRAPH
# ============================================================

print("Loading Uttarakhand routing graph...")

graph_data = np.load(GRAPH_FILE)

indices = graph_data["indices"]
indptr = graph_data["indptr"]
graph_distance = graph_data["data"].astype(np.float64)

coordinates = np.load(COORDINATE_FILE)

num_nodes = len(indptr) - 1
num_edges = len(indices)

print("Nodes:", num_nodes)
print("Edges:", num_edges)


# ============================================================
# CHECK COORDINATES
# ============================================================

print(
    "Coordinate range:",
    coordinates[:, 0].min(),
    "to",
    coordinates[:, 0].max(),
    "longitude"
)

print(
    "Latitude range:",
    coordinates[:, 1].min(),
    "to",
    coordinates[:, 1].max()
)


# ============================================================
# LOAD FLOOD-RISK DATA
# ============================================================

print("\nLoading flood-risk data...")

risk_data = np.load(RISK_FILE)

risk = risk_data["risk"].astype(np.float64)

print("Risk values:", len(risk))


# ============================================================
# VERIFY GRAPH/RISK ALIGNMENT
# ============================================================

if len(risk) != num_edges:

    raise RuntimeError(
        "ERROR: Risk array does not match graph edge count!"
    )

print("Risk data aligned with graph.")


# ============================================================
# BUILD CSR GRAPH
# ============================================================

base_graph = csr_matrix(
    (
        graph_distance,
        indices,
        indptr
    ),
    shape=(num_nodes, num_nodes)
)


# ============================================================
# SPATIAL INDEX
# ============================================================

print("\nBuilding spatial index...")

tree = cKDTree(coordinates)

print("Spatial index ready.")


# ============================================================
# CROWDSOURCED REPORTS
#
# A reported hazard hard-blocks the roads right around it for
# every mode, not just SAFEST — a road someone just reported as
# flooded is a fact on the ground, not a graduated risk estimate,
# so even FASTEST should route around it.
# ============================================================

REPORT_RADIUS_M = 150.0
REPORT_BLOCK_MULTIPLIER = 1_000_000.0

# Live rain nudges SAFEST's penalty formula up one tier when it's
# actually raining hard near the trip, on top of whatever the
# static hazard map already says.
LIVE_RAIN_ESCALATION_THRESHOLD_MM = 8.0

# SAFEST's absolute EXTREME block can force a wildly disproportionate
# detour to dodge a tiny hazardous stretch (e.g. +40km to avoid 300m
# of extreme-risk road). If the "purely safe" route is more than this
# many times longer than the direct route, that's no longer a sane
# safety tradeoff — SAFEST falls back to a milder penalty instead.
SAFEST_DETOUR_CAP = 3.0


def _haversine_m(lon1, lat1, lon2, lat2):

    r = 6371000.0

    phi1 = np.radians(lat1)
    phi2 = np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlambda = np.radians(lon2 - lon1)

    a = (
        np.sin(dphi / 2.0) ** 2
        + np.cos(phi1) * np.cos(phi2) * np.sin(dlambda / 2.0) ** 2
    )

    return 2 * r * np.arcsin(np.sqrt(a))


def _edges_near_points(points_lonlat, radius_m):

    if not points_lonlat:
        return np.array([], dtype=int)

    affected_nodes = set()

    for lon, lat in points_lonlat:

        # A flat 111 km/degree radius under-covers the east-west
        # direction at Uttarakhand's latitude (~29-31N), where a
        # degree of longitude is only ~96-97 km — so a fixed-degree
        # circle misses points that are genuinely within radius_m to
        # the east/west of a reported hazard. Query with the
        # (always-larger) longitude-based degree radius instead, so
        # the KD-tree candidate set is a superset of the true circle,
        # then filter to the real radius with a great-circle check.
        lon_scale = 111320.0 * max(math.cos(math.radians(lat)), 0.01)
        query_radius_deg = radius_m / lon_scale

        for idx in tree.query_ball_point([lon, lat], r=query_radius_deg):

            c_lon, c_lat = coordinates[idx]

            if _haversine_m(lon, lat, c_lon, c_lat) <= radius_m:
                affected_nodes.add(idx)

    if not affected_nodes:
        return np.array([], dtype=int)

    affected_nodes = np.array(sorted(affected_nodes))

    outgoing_edges = [
        np.arange(indptr[u], indptr[u + 1])
        for u in affected_nodes
    ]

    outgoing = (
        np.concatenate(outgoing_edges)
        if outgoing_edges else np.array([], dtype=int)
    )

    incoming = np.where(np.isin(indices, affected_nodes))[0]

    return np.unique(np.concatenate([outgoing, incoming]))


# ============================================================
# FIND EDGE INDEX
# ============================================================

def find_edge(u, v):

    start = indptr[u]
    end = indptr[u + 1]

    row = indices[start:end]

    matches = np.where(row == v)[0]

    if len(matches) == 0:
        return None

    return start + matches[0]


# ============================================================
# ROUTE FUNCTION
# ============================================================

def calculate_route(
    start_lon,
    start_lat,
    end_lon,
    end_lat,
    mode="FASTEST",
    report_points=None,
    live_rain_mm=None
):
    """
    Calculate a route between two points on the Uttarakhand road graph.

    IMPORTANT: all arguments are passed by keyword everywhere they're
    used (see server.py) — don't call this positionally, since the
    lon/lat order here doesn't match the lat/lon order the API uses.

    mode: "FASTEST" | "SAFEST"
    report_points: optional list of [lon, lat] crowdsourced hazard
        reports — roads near them are hard-blocked for every mode.
    live_rain_mm: optional current+near-term rainfall total (mm) —
        above LIVE_RAIN_ESCALATION_THRESHOLD_MM, SAFEST treats
        MODERATE/SIGNIFICANT roads one tier more cautiously.
    Returns a result dict, or None if no route exists between the
    two points in the directed road graph.
    """

    print("\n================================")
    print("ROUTING")
    print("================================")

    print("Mode:", mode)

    # --------------------------------------------------------
    # Snap start/end coordinates to nearest road nodes
    # --------------------------------------------------------

    start_point = [
        start_lon,
        start_lat
    ]

    end_point = [
        end_lon,
        end_lat
    ]

    start_snap_distance, start_node = tree.query(
        start_point
    )

    end_snap_distance, end_node = tree.query(
        end_point
    )

    print(
        "Start node:",
        start_node,
        "snap distance:",
        f"{start_snap_distance * 111000:.3f} m"
    )

    print(
        "End node:",
        end_node,
        "snap distance:",
        f"{end_snap_distance * 111000:.3f} m"
    )


    # --------------------------------------------------------
    # REPORTS + LIVE RAIN
    #
    # effective_risk = static hazard-map risk with crowdsourced
    # report edges forced to EXTREME. This is what gets reported
    # back in risk_counts/segment_risks, since it reflects real,
    # named hazards rather than a temporary weather nudge.
    #
    # routing_risk further escalates effective_risk by one tier
    # when live_rain_mm crosses the threshold, and is only used
    # to steer SAFEST — it never affects what gets reported as
    # the route's risk breakdown.
    # --------------------------------------------------------

    report_edges = _edges_near_points(report_points, REPORT_RADIUS_M)

    effective_risk = risk.copy()

    if report_edges.size > 0:
        effective_risk[report_edges] = 8.0

    live_escalate = (
        live_rain_mm is not None
        and live_rain_mm >= LIVE_RAIN_ESCALATION_THRESHOLD_MM
    )

    if live_escalate:

        routing_risk = np.where(
            effective_risk == 2.0, 4.0, effective_risk
        )

        routing_risk = np.where(
            effective_risk == 4.0, 8.0, routing_risk
        )

    else:

        routing_risk = effective_risk


    # --------------------------------------------------------
    # HELPERS: WEIGHTS + SHORTEST PATH
    #
    # Factored out so the SAFEST detour cap below can rerun
    # Dijkstra with a different weight formula without
    # duplicating the graph-building/path-reconstruction logic.
    # --------------------------------------------------------

    def _relaxed_safest_weights(risk_array):
        # A milder penalty than SAFEST's absolute EXTREME block —
        # used only as SAFEST's own fallback when the fully-safe
        # detour is disproportionately long (see SAFEST_DETOUR_CAP
        # below). Not a user-selectable mode on its own.

        EXTREME_RISK_VALUE = 8.0
        RELAXED_EXTREME_MULTIPLIER = 25.0

        mild_penalty = (
            graph_distance *
            (
                1.0 +
                0.75 * (risk_array - 1.0)
            )
        )

        return np.where(
            risk_array >= EXTREME_RISK_VALUE,
            graph_distance * RELAXED_EXTREME_MULTIPLIER,
            mild_penalty
        )

    def _apply_report_block(weight_array):

        if report_edges.size == 0:
            return weight_array

        weight_array = weight_array.copy()

        weight_array[report_edges] = (
            graph_distance[report_edges] * REPORT_BLOCK_MULTIPLIER
        )

        return weight_array

    def _shortest_path(weight_array):

        weighted_graph = csr_matrix(
            (weight_array, indices, indptr),
            shape=(num_nodes, num_nodes)
        )

        distances, predecessors = dijkstra(
            weighted_graph,
            directed=True,
            indices=int(start_node),
            return_predecessors=True
        )

        cost = distances[int(end_node)]

        if not np.isfinite(cost):
            return None, None

        found_path = []
        current = int(end_node)

        while current != int(start_node):

            found_path.append(current)
            current = predecessors[current]

            if current < 0:
                return None, None

        found_path.append(int(start_node))
        found_path.reverse()

        return found_path, float(cost)

    def _path_real_distance(path_nodes):

        total = 0.0

        for i in range(len(path_nodes) - 1):

            edge = find_edge(path_nodes[i], path_nodes[i + 1])

            if edge is not None:
                total += graph_distance[edge]

        return total

    def _path_extreme_count(path_nodes):

        count = 0

        for i in range(len(path_nodes) - 1):

            edge = find_edge(path_nodes[i], path_nodes[i + 1])

            if edge is not None and effective_risk[edge] >= 8.0:
                count += 1

        return count


    # --------------------------------------------------------
    # ROUTING WEIGHTS
    # --------------------------------------------------------

    if mode == "FASTEST":

        weights = graph_distance.copy()


    elif mode == "SAFEST":

        # Strongly penalize flood-risk roads.
        #
        # Risk:
        # NORMAL      = 1  (also covers the LOW hazard-class polygons —
        #                    those don't carry an edge penalty)
        # MODERATE    = 2
        # SIGNIFICANT = 4
        # EXTREME     = 8
        #
        # A simple risk^2 multiplier (max 64x for EXTREME) is
        # too weak: if an EXTREME segment is a short shortcut
        # and the safe detour is much longer, 64x still loses
        # to the detour's raw distance, so Dijkstra picks the
        # "safest" route straight through the extreme segment
        # anyway.
        #
        # Fix: treat EXTREME roads as effectively blocked
        # (huge multiplier) so they're only ever used when
        # there is truly no other way through. LOW/SIGNIFICANT
        # still get the risk^2 penalty so the router prefers
        # safer roads whenever a reasonable option exists.

        EXTREME_RISK_VALUE = 8.0
        EXTREME_BLOCK_MULTIPLIER = 1_000_000.0

        weights = np.where(
            routing_risk >= EXTREME_RISK_VALUE,
            graph_distance * EXTREME_BLOCK_MULTIPLIER,
            graph_distance * (routing_risk ** 2)
        )


    else:

        raise ValueError(
            "Unknown routing mode: "
            + str(mode)
        )


    # --------------------------------------------------------
    # HARD-BLOCK REPORTED HAZARDS (all modes)
    # --------------------------------------------------------

    weights = _apply_report_block(weights)


    # --------------------------------------------------------
    # DIJKSTRA
    # --------------------------------------------------------

    print("Running Dijkstra...")

    path, route_cost = _shortest_path(weights)

    if path is None:

        print("\nNO ROUTE FOUND")

        print(
            "The selected locations are not connected"
            " in the directed road graph."
        )

        return None

    # Extreme-edge count on the route Dijkstra picked under the
    # absolute EXTREME block (1,000,000x). If this is nonzero, it
    # means literally no extreme-free path connects start to end —
    # true unavoidability. Captured before the detour cap below may
    # swap `path` out for a milder-penalty fallback that trades
    # safety for distance, which is a policy choice, not
    # unavoidability, and must not be reported as the latter.
    initial_extreme_count = (
        _path_extreme_count(path) if mode == "SAFEST" else 0
    )


    # --------------------------------------------------------
    # SAFEST DETOUR CAP
    #
    # If the purely-safe route is disproportionately longer than
    # the direct route (see SAFEST_DETOUR_CAP above), relax to a
    # milder penalty instead of an absolute EXTREME block.
    # --------------------------------------------------------

    safest_capped = False

    if mode == "SAFEST":

        baseline_path, _ = _shortest_path(
            _apply_report_block(graph_distance.copy())
        )

        if baseline_path is not None:

            baseline_distance = _path_real_distance(baseline_path)
            safest_distance = _path_real_distance(path)

            if (
                baseline_distance > 0
                and safest_distance > SAFEST_DETOUR_CAP * baseline_distance
            ):

                fallback_weights = _apply_report_block(
                    _relaxed_safest_weights(routing_risk)
                )

                fallback_path, fallback_cost = _shortest_path(
                    fallback_weights
                )

                if fallback_path is not None:

                    path = fallback_path
                    route_cost = fallback_cost
                    safest_capped = True

                    print(
                        "\nSAFEST detour capped: pure-avoidance route "
                        f"was {safest_distance / baseline_distance:.1f}x "
                        "the direct distance — relaxed to a milder "
                        "penalty."
                    )


    # ========================================================
    # CALCULATE REAL ROAD DISTANCE
    # ========================================================

    total_distance = 0.0

    risk_counts = {
        1.0: 0,
        2.0: 0,
        4.0: 0,
        8.0: 0
    }

    route_edges = []

    # segment_risks holds one risk value per (path[i], path[i+1]) hop, in
    # the same order as path — so it always has len(path) - 1 entries and
    # lines up 1:1 with the coordinate list the frontend draws. That lets
    # the map color each stretch of the route by its own risk instead of
    # drawing the whole route as a single flat color.
    segment_risks = []

    for i in range(len(path) - 1):

        u = path[i]
        v = path[i + 1]

        edge = find_edge(u, v)

        if edge is None:

            # Edge lookup failed unexpectedly (shouldn't normally happen,
            # since the path came from this same graph) — fall back to
            # "Normal" risk rather than dropping the entry, so
            # segment_risks never falls out of sync with the path.
            segment_risks.append(1.0)

            continue

        route_edges.append(edge)

        total_distance += graph_distance[edge]

        edge_risk = effective_risk[edge]

        segment_risks.append(float(edge_risk))

        if edge_risk in risk_counts:

            risk_counts[edge_risk] += 1


    # ========================================================
    # RESULT
    # ========================================================

    print("\n========== ROUTE RESULT ==========")

    print(
        "Distance:",
        f"{total_distance / 1000:.2f} km"
    )

    print(
        "Road nodes:",
        len(path)
    )

    print("\nFlood-risk edges:")

    print(
        "Normal:",
        risk_counts[1.0]
    )

    print(
        "Low:",
        risk_counts[2.0]
    )

    print(
        "Significant:",
        risk_counts[4.0]
    )

    print(
        "Extreme:",
        risk_counts[8.0]
    )


    # ========================================================
    # RETURN RESULT
    # ========================================================

    extreme_unavoidable = (
        mode == "SAFEST"
        and initial_extreme_count > 0
    )

    reported_hazard_count = (
        int(np.intersect1d(np.array(route_edges), report_edges).size)
        if route_edges and report_edges.size > 0
        else 0
    )

    return {
        "mode": mode,
        "start_node": int(start_node),
        "end_node": int(end_node),
        "start_snap_distance": float(
            start_snap_distance
        ),
        "end_snap_distance": float(
            end_snap_distance
        ),
        "distance_m": float(total_distance),
        "route_cost": float(route_cost),
        "path": path,
        "route_edges": route_edges,
        "risk_counts": risk_counts,
        "segment_risks": segment_risks,
        "extreme_unavoidable": extreme_unavoidable,
        "live_escalation_applied": bool(live_escalate),
        "reported_hazard_count": reported_hazard_count,
        "safest_capped": bool(safest_capped)
    }


# ============================================================
# RESCUE SHELTERS
#
# Loaded once at import time from shelters.json (real OSM points
# tagged shelter/community_centre/social_facility/hospital — see
# extract_shelters.py). Missing/unreadable file degrades to "no
# shelters known" rather than breaking routing entirely.
# ============================================================

def _load_shelters():

    if not os.path.exists(SHELTER_FILE):
        return []

    try:

        with open(SHELTER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, list):
            return []

        return data

    except (OSError, ValueError) as e:

        print("WARNING: failed to load shelters.json:", repr(e))
        return []


shelters = _load_shelters()

# Only "shelter"-kind points (shelter/community_centre/social_facility)
# are evacuation targets — hospitals are a separate target list used
# for "route to nearest hospital" instead of flood evacuation.
_evacuation_targets = [s for s in shelters if s.get("kind") == "shelter"]
_hospital_targets = [s for s in shelters if s.get("kind") == "hospital"]


def _build_tree(targets):

    if not targets:
        return None

    coords = np.array([[t["lon"], t["lat"]] for t in targets])

    return cKDTree(coords)


_shelter_tree = _build_tree(_evacuation_targets)
_hospital_tree = _build_tree(_hospital_targets)

print(f"Loaded {len(shelters)} shelter/hospital points "
      f"({len(_evacuation_targets)} evacuation targets, "
      f"{len(_hospital_targets)} hospitals)")


def _find_nearest_target(
    targets,
    tree,
    start_lon,
    start_lat,
    mode,
    report_points,
    live_rain_mm,
    max_candidates
):
    """
    Shared shortlist-then-route logic for both find_nearest_shelter()
    and find_nearest_hospital(): shortlist the `max_candidates`
    straight-line-closest points (cheap), then run the real
    flood-aware router to each and keep the one with the shortest
    real route — the straight-line-nearest point isn't necessarily
    the fastest/safest one to actually reach.
    """

    if tree is None:
        return None

    k = min(max_candidates, len(targets))

    distances, indexes = tree.query(
        [start_lon, start_lat],
        k=k
    )

    indexes = np.atleast_1d(indexes)

    best_target = None
    best_result = None

    for idx in indexes:

        target = targets[int(idx)]

        result = calculate_route(
            start_lon=start_lon,
            start_lat=start_lat,
            end_lon=target["lon"],
            end_lat=target["lat"],
            mode=mode,
            report_points=report_points,
            live_rain_mm=live_rain_mm
        )

        if result is None:
            continue

        if best_result is None or result["distance_m"] < best_result["distance_m"]:
            best_target = target
            best_result = result

    if best_result is None:
        return None

    return {
        "target": best_target,
        "route": best_result
    }


def find_nearest_shelter(
    start_lon,
    start_lat,
    mode="SAFEST",
    report_points=None,
    live_rain_mm=None,
    max_candidates=5
):
    """
    Find the best reachable shelter for an evacuation. Returns
    {"shelter": {...}, "route": <calculate_route result>} or None if
    no shelters are loaded or none are reachable.
    """

    found = _find_nearest_target(
        _evacuation_targets, _shelter_tree,
        start_lon, start_lat, mode,
        report_points, live_rain_mm, max_candidates
    )

    if found is None:
        return None

    return {"shelter": found["target"], "route": found["route"]}


def find_nearest_hospital(
    start_lon,
    start_lat,
    mode="SAFEST",
    report_points=None,
    live_rain_mm=None,
    max_candidates=5
):
    """
    Find the best reachable hospital, by real road distance. Returns
    {"hospital": {...}, "route": <calculate_route result>} or None if
    no hospitals are loaded or none are reachable.
    """

    found = _find_nearest_target(
        _hospital_targets, _hospital_tree,
        start_lon, start_lat, mode,
        report_points, live_rain_mm, max_candidates
    )

    if found is None:
        return None

    return {"hospital": found["target"], "route": found["route"]}


# ============================================================
# MAIN TEST
# ============================================================

if __name__ == "__main__":

    # ========================================================
    # RISHIKESH
    # ========================================================

    start_lon = 78.2916193
    start_lat = 30.1086537


    # ========================================================
    # DEHRADUN
    # ========================================================

    end_lon = 78.032174
    end_lat = 30.3165127


    print("\n")
    print("########################################")
    print("#      FLOODSAFE ROUTING TEST          #")
    print("########################################")

    print("\nStart:")
    print(
        f"Rishikesh "
        f"({start_lon}, {start_lat})"
    )

    print("\nDestination:")
    print(
        f"Dehradun "
        f"({end_lon}, {end_lat})"
    )


    # ========================================================
    # RUN ALL THREE MODES
    # ========================================================

    results = {}


    # --------------------------------------------------------
    # FASTEST
    # --------------------------------------------------------

    results["FASTEST"] = calculate_route(
        start_lon,
        start_lat,
        end_lon,
        end_lat,
        "FASTEST"
    )


    # --------------------------------------------------------
    # SAFEST
    # --------------------------------------------------------

    results["SAFEST"] = calculate_route(
        start_lon,
        start_lat,
        end_lon,
        end_lat,
        "SAFEST"
    )


    # ========================================================
    # COMPARISON
    # ========================================================

    print("\n")
    print("========================================")
    print("ROUTE COMPARISON")
    print("========================================")


    for mode in [
        "FASTEST",
        "SAFEST"
    ]:

        result = results[mode]

        print("\n" + mode)

        print("--------------------------------")

        if result is None:

            print("NO ROUTE")

            continue


        print(
            "Distance:",
            f"{result['distance_m'] / 1000:.3f} km"
        )

        print(
            "Road nodes:",
            len(result["path"])
        )

        print(
            "Normal:",
            result["risk_counts"][1.0]
        )

        print(
            "Low:",
            result["risk_counts"][2.0]
        )

        print(
            "Significant:",
            result["risk_counts"][4.0]
        )

        print(
            "Extreme:",
            result["risk_counts"][8.0]
        )


    # ========================================================
    # ROUTE DIFFERENCE
    # ========================================================

    valid_results = [
        results["FASTEST"],
        results["SAFEST"]
    ]

    valid_results = [
        r for r in valid_results
        if r is not None
    ]


    if len(valid_results) >= 2:

        fastest_path = set(
            results["FASTEST"]["path"]
        )

        safest_path = set(
            results["SAFEST"]["path"]
        )


        print("\n")
        print("========================================")
        print("ROUTE DIFFERENCE")
        print("========================================")


        if fastest_path == safest_path:

            print(
                "FASTEST and SAFEST use the same"
                " road-node set."
            )

        else:

            print(
                "FASTEST and SAFEST use DIFFERENT"
                " routes."
            )


    # ========================================================
    # COMPLETE
    # ========================================================

    print("\n")
    print("========================================")
    print("TEST COMPLETE")
    print("========================================")