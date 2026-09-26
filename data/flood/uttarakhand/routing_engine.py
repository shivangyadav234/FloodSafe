import os
import json
import math
import re

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

import ctypes

import numpy as np

from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree


# ============================================================
# MEMORY
#
# Each route builds a few full-graph arrays of ~31 MiB, just under
# glibc's largest automatic mmap threshold (32 MiB). Such blocks come
# from the heap, and with the server's worker threads each getting its
# own malloc arena, freed blocks stayed in the process instead of going
# back to the OS: resident memory crept up route by route until Render
# restarted the 512 MB instance for exceeding its limit. A fixed 4 MiB
# threshold makes every large array its own mapping, returned on free,
# and two arenas bound what idle threads can hold on to. Linux/glibc
# only; elsewhere (a teammate's Windows machine) this is skipped.
# ============================================================

try:
    _libc = ctypes.CDLL("libc.so.6")
    _libc.mallopt(-3, 4 * 1024 * 1024)  # M_MMAP_THRESHOLD
    _libc.mallopt(-8, 2)                # M_ARENA_MAX
except (OSError, AttributeError):
    pass


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

# Each edge's risk as an index into RISK_LEVELS, so a route's
# full-graph weights are one table lookup (see _full_weights).
RISK_LEVELS = np.array([1.0, 2.0, 4.0, 8.0])

if not np.isin(risk, RISK_LEVELS).all():

    raise RuntimeError(
        "ERROR: Risk array has values other than 1, 2, 4 and 8!"
    )

risk_level = np.searchsorted(RISK_LEVELS, risk).astype(np.uint8)


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
# OFF-NETWORK CHECK
#
# calculate_route snaps each end to the nearest road node however far
# away it is, so a start point in Delhi silently became a route from
# the nearest Uttarakhand road -- reported as "ok", with no hint the
# start had moved ~200 km. Callers check this first and refuse points
# that are not near the road network at all.
# ============================================================

MAX_SNAP_KM = 5.0


def road_snap_km(lat, lon):
    """
    Approximate distance in km from a point to the nearest road node.
    The tree is in raw degrees, so this treats a degree of longitude as
    111 km; at Uttarakhand's latitude that overstates east-west distance
    by about 15%, which only makes the check slightly stricter.
    """

    distance_deg, _ = tree.query([lon, lat])
    return float(distance_deg) * 111.0


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
    live_rain_mm=None,
    dijkstra_cache=None
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
    dijkstra_cache: optional dict shared across calls with the same
        start, mode, report_points and live_rain_mm, so the searches
        are reused instead of rerun (see _shortest_path). Only valid
        for that one batch; don't keep it beyond.
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

    if mode not in ("FASTEST", "SAFEST"):
        raise ValueError(
            "Unknown routing mode: "
            + str(mode)
        )

    # Anything that depends only on the reports and the rain -- not on
    # the destination -- is shared across a batch (see dijkstra_cache).
    batch = dijkstra_cache if dijkstra_cache is not None else {}

    # Finding the reported edges scans all 4M edges once per report set.
    if "report_edges" not in batch:
        batch["report_edges"] = _edges_near_points(report_points, REPORT_RADIUS_M)

    report_edges = batch["report_edges"]

    live_escalate = (
        live_rain_mm is not None
        and live_rain_mm >= LIVE_RAIN_ESCALATION_THRESHOLD_MM
    )

    EXTREME_RISK_VALUE = 8.0
    EXTREME_BLOCK_MULTIPLIER = 1_000_000.0


    # --------------------------------------------------------
    # RISK AND WEIGHTS, FOR EVERY EDGE OR JUST SOME
    #
    # Full-graph weights (4M edges, 33 MB) are only needed when
    # Dijkstra actually runs (_full_weights). Everything else -- the
    # route's risk breakdown, its cost, the detour-cap check -- reads
    # the few thousand edges on one path, so those formulas take an
    # index array of just those edges, and a route served from a
    # cached search is described by exactly the same formulas as a
    # fresh one. Rebuilding the full arrays for each of five
    # evacuation candidates was most of /evacuate's time on Render's
    # ~0.1-CPU free tier.
    # --------------------------------------------------------

    def _effective_risk(edges):
        # Static hazard-map risk with crowdsourced report edges forced
        # to EXTREME. This is what gets reported back in
        # risk_counts/segment_risks, since it reflects real, named
        # hazards rather than a temporary weather nudge.

        values = risk[edges]

        if report_edges.size > 0:
            values[np.isin(edges, report_edges)] = 8.0

        return values

    def _escalate(values):
        # One tier up when live rain crosses the threshold. Only
        # steers SAFEST; never reported.

        if not live_escalate:
            return values

        escalated = np.where(values == 2.0, 4.0, values)
        return np.where(values == 4.0, 8.0, escalated)

    def _routing_risk(edges):
        return _escalate(_effective_risk(edges))


    # --------------------------------------------------------
    # WEIGHTS
    #
    # "primary" is the mode's own weighting, "baseline" is plain
    # distance (SAFEST's yardstick for the detour cap), "fallback" is
    # SAFEST's milder penalty when the cap trips. Reported hazards are
    # hard-blocked in all three, for every mode.
    # --------------------------------------------------------

    def _plain_distance(kind):
        return kind == "baseline" or (kind == "primary" and mode == "FASTEST")

    def _multiplier(kind, routing):

        if kind == "primary":

            # SAFEST: strongly penalize flood-risk roads.
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

            return np.where(
                routing >= EXTREME_RISK_VALUE,
                EXTREME_BLOCK_MULTIPLIER,
                routing ** 2
            )

        # "fallback": a milder penalty than SAFEST's absolute
        # EXTREME block — used only as SAFEST's own fallback when
        # the fully-safe detour is disproportionately long (see
        # SAFEST_DETOUR_CAP). Not a user-selectable mode on its own.

        RELAXED_EXTREME_MULTIPLIER = 25.0

        return np.where(
            routing >= EXTREME_RISK_VALUE,
            RELAXED_EXTREME_MULTIPLIER,
            1.0 + 0.75 * (routing - 1.0)
        )

    def _weights(kind, edges):
        # Weights of just these edges, for a found path's cost.

        distance = graph_distance[edges]

        if _plain_distance(kind):
            values = distance.copy()
        else:
            values = distance * _multiplier(kind, _routing_risk(edges))

        if report_edges.size > 0:
            reported = np.isin(edges, report_edges)
            values[reported] = distance[reported] * REPORT_BLOCK_MULTIPLIER

        return values

    def _full_weights(kind):
        # The same weights for every edge, for Dijkstra, built in one
        # array. Chained expressions over all 4M edges held about five
        # full-size temporaries at once (~110 MB above the resident
        # graph), enough to push Render's 512 MB instance over its
        # limit. Risk takes only four values, so the multiplier is a
        # four-entry table looked up by each edge's risk level, then
        # scaled by distance in place. Reported edges are overwritten
        # below whatever their risk, so their forced EXTREME in
        # _effective_risk needs no counterpart here.

        if _plain_distance(kind):

            if report_edges.size == 0:
                # Read-only from here: csr_matrix and dijkstra don't
                # write to their data array.
                return graph_distance

            values = graph_distance.copy()

        else:

            table = _multiplier(kind, _escalate(RISK_LEVELS))
            values = np.take(table, risk_level)
            np.multiply(values, graph_distance, out=values)

        if report_edges.size > 0:
            values[report_edges] = graph_distance[report_edges] * REPORT_BLOCK_MULTIPLIER

        return values


    # --------------------------------------------------------
    # PATH HELPERS
    # --------------------------------------------------------

    def _path_edges(path_nodes):
        """Edge index for each hop, None where the lookup fails."""
        return [find_edge(path_nodes[i], path_nodes[i + 1]) for i in range(len(path_nodes) - 1)]

    def _found(edges):
        return np.array([e for e in edges if e is not None], dtype=np.int64)

    def _shortest_path(cache_key):

        # A single-source Dijkstra already yields the path to *every*
        # node, and it depends only on the start and the weights, not
        # on end_node. /evacuate and /nearest-hospital route from one
        # start to five candidates, so the search runs once per
        # weighting and is reused -- and the full weight array is only
        # built when it does run.
        predecessors = batch.get(cache_key)

        if predecessors is None:

            weighted_graph = csr_matrix(
                (_full_weights(cache_key), indices, indptr),
                shape=(num_nodes, num_nodes)
            )

            _, predecessors = dijkstra(
                weighted_graph,
                directed=True,
                indices=int(start_node),
                return_predecessors=True
            )

            # Only the predecessor array (int32, ~8 MB) is kept: the
            # server runs close to Render's 512 MB cap.
            batch[cache_key] = predecessors

        if int(end_node) != int(start_node) and predecessors[int(end_node)] < 0:
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

        edges = _found(_path_edges(found_path))
        cost = sum(float(w) for w in _weights(cache_key, edges)) if edges.size else 0.0

        return found_path, cost

    def _path_real_distance(path_nodes):

        edges = _found(_path_edges(path_nodes))
        return sum(float(d) for d in graph_distance[edges]) if edges.size else 0.0

    def _path_extreme_count(path_nodes):

        edges = _found(_path_edges(path_nodes))
        return int(np.count_nonzero(_effective_risk(edges) >= 8.0)) if edges.size else 0


    # --------------------------------------------------------
    # DIJKSTRA
    # --------------------------------------------------------

    print("Running Dijkstra...")

    path, route_cost = _shortest_path("primary")

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

        baseline_path, _ = _shortest_path("baseline")

        if baseline_path is not None:

            baseline_distance = _path_real_distance(baseline_path)
            safest_distance = _path_real_distance(path)

            if (
                baseline_distance > 0
                and safest_distance > SAFEST_DETOUR_CAP * baseline_distance
            ):

                fallback_path, fallback_cost = _shortest_path("fallback")

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

    hop_edges = _path_edges(path)
    path_found_edges = _found(hop_edges)
    path_risk = dict(zip(
        path_found_edges.tolist(),
        _effective_risk(path_found_edges).tolist() if path_found_edges.size else []
    ))

    for edge in hop_edges:

        if edge is None:

            # Edge lookup failed unexpectedly (shouldn't normally happen,
            # since the path came from this same graph) — fall back to
            # "Normal" risk rather than dropping the entry, so
            # segment_risks never falls out of sync with the path.
            segment_risks.append(1.0)

            continue

        route_edges.append(edge)

        total_distance += graph_distance[edge]

        edge_risk = path_risk[int(edge)]

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
# OpenStreetMap's "shelter" and "social_facility" also cover caves,
# gazebos, shepherds' and trekkers' huts, nursing homes, retirement
# villas and a boat club -- evacuation from central Dehradun was sent
# to a nursing home, from Uttarkashi to a cave. Those stay on the map
# but are not evacuation targets. Community halls, panchayat bhawans,
# auditoriums, rain baseras and ashrams are kept.
_UNSUITABLE_SHELTER = re.compile(
    r"\b(caves?|gazebo|metal roof|shepherd|campsite|rain shelter|bugyal|nursing|senior living|"
    r"villas?|apartments?|boat club|house|homeopathic|hospital|gaushala)\b",
    re.IGNORECASE)


def is_evacuation_shelter(point):
    return point.get("kind") == "shelter" and not _UNSUITABLE_SHELTER.search(point.get("name") or "")


_evacuation_targets = [s for s in shelters if is_evacuation_shelter(s)]

# OpenStreetMap tags eye, dental, ENT, IVF and veterinary practices as
# amenity=hospital too, so "nearest hospital" from central Dehradun was
# an ENT and dental clinic. Those can't take a flood casualty; they stay
# on the map but are not routing targets. A name that also says general
# or maternity care, or a hospital with a diagnostic wing, is kept.
_SPECIALIST_ONLY = re.compile(r"\b(dental|dentist|eye|eyes|ent|laser|ivf|fertility|veterinary|vet|optical)\b",
                              re.IGNORECASE)
_TESTS_ONLY = re.compile(r"\b(diagnostics?|pathology|labs?|laboratory|imaging|x-?ray)\b", re.IGNORECASE)
_GENERAL_CARE = re.compile(r"\b(general|maternity|multi-?speciality|medical college|district|civil|combined)\b",
                           re.IGNORECASE)


def is_emergency_hospital(name):
    name = name or ""
    if _GENERAL_CARE.search(name):
        return True
    if _SPECIALIST_ONLY.search(name):
        return False
    # "X Diagnostics" is a test lab; "Raj Hospital and Diagnostic Centre"
    # is a hospital with one.
    return not (_TESTS_ONLY.search(name) and not re.search(r"\bhospital\b", name, re.IGNORECASE))


# OpenStreetMap entries tagged amenity=hospital that are not hospitals
# at all, found by listing every name with no healthcare word in it.
# The rest of that list were real (CMI, DH Pauri, "... Chikitsalay").
_NOT_HOSPITALS = {
    "osm-4255814491",   # "wedding by fourth munky", central Dehradun
    "osm-12593415466",  # "Tulas owner"
}

_hospital_targets = [s for s in shelters
                     if s.get("kind") == "hospital" and s.get("id") not in _NOT_HOSPITALS
                     and is_emergency_hospital(s.get("name"))]


# A degree of longitude is only ~96-97 km at Uttarakhand's latitude
# (~29-31N), not the ~111 km a degree of latitude covers, so a KD-tree
# built directly on raw [lon, lat] pairs distorts distance ranking
# (see the same fix already applied to _edges_near_points above).
# Scaling longitude by cos(reference latitude) before building/querying
# the tree makes Euclidean distance in the tree's coordinate space
# approximate real-world distance across this region.
_SHELTER_TREE_REF_LAT = float(np.mean(coordinates[:, 1])) if len(coordinates) else 30.0
_SHELTER_TREE_LON_SCALE = math.cos(math.radians(_SHELTER_TREE_REF_LAT))


def _build_tree(targets):

    if not targets:
        return None

    coords = np.array([
        [t["lon"] * _SHELTER_TREE_LON_SCALE, t["lat"]] for t in targets
    ])

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
        [start_lon * _SHELTER_TREE_LON_SCALE, start_lat],
        k=k
    )

    indexes = np.atleast_1d(indexes)

    best_target = None
    best_result = None

    # Same start, mode, reports and rain for every candidate, so each
    # distinct Dijkstra runs once and is reused. Local to this call.
    dijkstra_cache = {}

    for idx in indexes:

        target = targets[int(idx)]

        result = calculate_route(
            start_lon=start_lon,
            start_lat=start_lat,
            end_lon=target["lon"],
            end_lat=target["lat"],
            mode=mode,
            report_points=report_points,
            live_rain_mm=live_rain_mm,
            dijkstra_cache=dijkstra_cache
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