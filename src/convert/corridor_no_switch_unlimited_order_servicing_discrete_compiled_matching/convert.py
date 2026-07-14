import os
import json
import logging
import argparse
from collections import deque
from fractions import Fraction
import unified_planning.shortcuts as up
from unified_planning.io import PDDLWriter


parser = argparse.ArgumentParser()
parser.add_argument("-p", "--path-to-folder", required=False, default=None)
parser.add_argument("-s", "--scenario-file", required=False, default="scenario_solver_example1.json")
parser.add_argument("-l", "--location-file", required=False, default="location_solver.json")
parser.add_argument("-o", "--output-file", required=False, default=None)
parser.add_argument("-d", "--domain-file", required=False, default=None)
parser.add_argument("--log-level", default="ERROR", required=False)
parser.add_argument("--precompute-matching", action="store_true")
parser.add_argument("--matching-variant", type=int, default=0)
parser.add_argument(
    "--matching-strategy",
    choices=("stable", "order_preserving", "order_preserving_auto"),
    default="stable",
)


CORRIDOR_EXPAND_HOPS = 3


def _build_adjacency(location_object):
    # Undirected graph: each trackpart id maps to the set of ids it shares an aSide/bSide connection with.
    adjacency = {tp["id"]: set() for tp in location_object["trackParts"]}
    for tp in location_object["trackParts"]:
        for nb_id in tp.get("aSide", []) + tp.get("bSide", []):
            if nb_id in adjacency:
                adjacency[tp["id"]].add(nb_id)
                adjacency[nb_id].add(tp["id"])
    return adjacency


def _bfs_from(adjacency, start_ids):
    # Returns hop-distance from any of the start nodes to every reachable node.
    dist = {}
    queue = deque()
    for t_id in start_ids:
        if t_id in adjacency and t_id not in dist:
            dist[t_id] = 0
            queue.append(t_id)
    while queue:
        current = queue.popleft()
        for nb in adjacency[current]:
            if nb not in dist:
                dist[nb] = dist[current] + 1
                queue.append(nb)
    return dist


def _departure_exit_ids(scenario_object, location_object):
    # The departure track is where outbound trains leave the yard — the BFS root for entry_distance.
    # Falls back to inbound entry tracks if no outbound requests are present in the scenario.
    ids = [req["leaveTrackPart"] for req in scenario_object.get("out", {}).get("trainRequests", []) if "leaveTrackPart" in req]
    if not ids:
        ids = [t["entryTrackPart"] for t in scenario_object.get("in", {}).get("trains", []) if "entryTrackPart" in t]

    track_parts = location_object.get("trackParts", [])
    ids_aside = {tp["id"] for tp in track_parts if tp["id"] in ids and tp.get("bSide")}
    ids_bside = {tp["id"] for tp in track_parts if tp["id"] in ids and tp.get("aSide")}

    return ids_aside, ids_bside


def train_unit_type_key(train_unit):
    # Normalized train-unit identity used to match available units to request slots.
    unit_type = train_unit["type"]
    return (
        unit_type.get("displayName"),
        int(unit_type.get("carriages", 0)),
        float(unit_type.get("length", 0.0)),
    )


def all_trains_with_source(scenario_object):
    # Keep source/index so train units can be linked back to their physical train.
    for index, train in enumerate(scenario_object.get("in", {}).get("trains", [])):
        yield "in", index, train
    for index, train in enumerate(scenario_object.get("inStanding", {}).get("trains", [])):
        yield "inStanding", index, train


def _coupling_track_ids_for_request(request, location_object, candidate_track_ids):
    # Prefer request-specific parking/departure information, otherwise use nearby coupling tracks.
    candidate_track_ids = {str(track_id) for track_id in candidate_track_ids}
    preferred_ids = [request.get("lastParkingTrackPart"), request.get("leaveTrackPart")]
    preferred_ids = [str(track_id) for track_id in preferred_ids if track_id is not None and str(track_id) in candidate_track_ids]
    if preferred_ids:
        return preferred_ids[:1]

    leave_track_id = request.get("leaveTrackPart")
    adjacency = _build_adjacency(location_object)
    distances = _bfs_from(adjacency, [leave_track_id] if leave_track_id else [])
    reachable_candidates = [
        (distances[track_id], track_id)
        for track_id in candidate_track_ids
        if track_id in distances
    ]
    if reachable_candidates:
        return [track_id for _, track_id in sorted(reachable_candidates)[:1]]

    return sorted(candidate_track_ids)[:1]


def _shortest_path(adjacency, start_id, goal_id):
    # BFS shortest path (inclusive list of node ids) over executable movement edges.
    if start_id is None or goal_id is None:
        return []
    if start_id == goal_id:
        return [start_id]
    visited = {start_id}
    queue = deque([[start_id]])
    while queue:
        path = queue.popleft()
        for nb in sorted(adjacency.get(path[-1], ())):
            if nb in visited:
                continue
            if nb == goal_id:
                return path + [nb]
            visited.add(nb)
            queue.append(path + [nb])
    return []


def _train_unit_type_keys(train):
    return {train_unit_type_key(member["trainUnit"]) for member in train.get("members", [])}


def _request_type_keys(request):
    return {train_unit_type_key(train_unit) for train_unit in request.get("trainUnits", [])}


def _train_task_types(train):
    return {
        task["type"]["other"]
        for member in train.get("members", [])
        for task in member.get("tasks", [])
        if task.get("type", {}).get("other")
    }


def _unit_source_positions(scenario_object):
    positions = {}
    for _, _, train in all_trains_with_source(scenario_object):
        members = train.get("members", [])
        for index, member in enumerate(members):
            positions[member["trainUnit"]["id"]] = (index, len(members))
    return positions


def _has_order_sensitive_matching(scenario_object):
    source_types = {
        train_unit_type_key(member["trainUnit"])
        for _, _, train in all_trains_with_source(scenario_object)
        if len(train.get("members", [])) > 1
        for member in train["members"]
    }
    request_types = {
        train_unit_type_key(unit)
        for request in scenario_object.get("out", {}).get("trainRequests", [])
        if len(request.get("trainUnits", [])) > 1
        for unit in request["trainUnits"]
    }
    return bool(source_types & request_types)


def _matching_order_cost(unit_id, slot_index, slot_records, unit_positions):
    source_index, source_size = unit_positions.get(unit_id, (0, 1))
    _, _, target_index, target_size = slot_records[slot_index]
    if source_size <= 1 or target_size <= 1:
        return Fraction(0)
    source_position = Fraction(source_index, source_size - 1)
    target_position = Fraction(target_index, target_size - 1)
    return abs(source_position - target_position)


def _minimum_assignment_cost(slot_indices, unit_ids, slot_records, unit_positions):
    if not slot_indices:
        return Fraction(0)
    if len(slot_indices) > len(unit_ids):
        return None

    costs = [
        [
            _matching_order_cost(unit_id, slot_index, slot_records, unit_positions)
            for unit_id in unit_ids
        ]
        for slot_index in slot_indices
    ]
    row_count = len(costs)
    column_count = len(costs[0])
    row_potential = [Fraction(0)] * (row_count + 1)
    column_potential = [Fraction(0)] * (column_count + 1)
    matched_row = [0] * (column_count + 1)
    predecessor = [0] * (column_count + 1)

    # Hungarian assignment; rows are request slots and columns are compatible units.
    for row in range(1, row_count + 1):
        matched_row[0] = row
        column = 0
        minimum = [None] * (column_count + 1)
        used = [False] * (column_count + 1)
        while True:
            used[column] = True
            current_row = matched_row[column]
            delta = None
            next_column = 0
            for candidate in range(1, column_count + 1):
                if used[candidate]:
                    continue
                reduced_cost = (
                    costs[current_row - 1][candidate - 1]
                    - row_potential[current_row]
                    - column_potential[candidate]
                )
                if minimum[candidate] is None or reduced_cost < minimum[candidate]:
                    minimum[candidate] = reduced_cost
                    predecessor[candidate] = column
                if delta is None or minimum[candidate] < delta:
                    delta = minimum[candidate]
                    next_column = candidate
            for candidate in range(column_count + 1):
                if used[candidate]:
                    row_potential[matched_row[candidate]] += delta
                    column_potential[candidate] -= delta
                elif minimum[candidate] is not None:
                    minimum[candidate] -= delta
            column = next_column
            if matched_row[column] == 0:
                break
        while True:
            previous = predecessor[column]
            matched_row[column] = matched_row[previous]
            column = previous
            if column == 0:
                break

    assignment = [None] * row_count
    for column in range(1, column_count + 1):
        if matched_row[column] != 0:
            assignment[matched_row[column] - 1] = column - 1
    return sum(costs[row][column] for row, column in enumerate(assignment))


def _select_order_preserving_matching(unit_type_by_id, slot_records, unit_positions):
    assignments = []
    unit_order = list(unit_type_by_id)
    requested_types = list(dict.fromkeys(record[1] for record in slot_records))

    # Compatibility is exact by train-unit type, so each type can be optimized independently.
    for requested_type in requested_types:
        slots = [index for index, record in enumerate(slot_records) if record[1] == requested_type]
        units = [unit_id for unit_id in unit_order if unit_type_by_id[unit_id] == requested_type]
        optimum = _minimum_assignment_cost(slots, units, slot_records, unit_positions)
        if optimum is None:
            raise ValueError(f"No complete precomputed matching exists for type {requested_type}")

        remaining_slots = list(slots)
        remaining_units = list(units)
        while remaining_slots:
            slot_index = remaining_slots.pop(0)
            selected_unit = None
            for unit_id in remaining_units:
                remainder_units = [candidate for candidate in remaining_units if candidate != unit_id]
                remainder_cost = _minimum_assignment_cost(
                    remaining_slots, remainder_units, slot_records, unit_positions
                )
                if remainder_cost is None:
                    continue
                current_cost = _matching_order_cost(
                    unit_id, slot_index, slot_records, unit_positions
                )
                if current_cost + remainder_cost == optimum:
                    selected_unit = unit_id
                    optimum -= current_cost
                    break
            if selected_unit is None:
                raise ValueError("Could not reconstruct an optimal precomputed matching")
            assignments.append((selected_unit, slot_index))
            remaining_units.remove(selected_unit)

    return sorted(assignments, key=lambda item: item[1])


def _select_precomputed_matching(
    unit_type_by_id,
    slot_records,
    matching_variant,
    matching_strategy="stable",
    unit_positions=None,
):
    if matching_variant < 0:
        raise ValueError("matching_variant must be non-negative")

    if matching_strategy == "order_preserving" and matching_variant == 0:
        return _select_order_preserving_matching(
            unit_type_by_id, slot_records, unit_positions or {}
        )

    unit_ids = list(unit_type_by_id)
    completed_assignments = []

    def search(slot_index, used_units, assignment):
        if slot_index == len(slot_records):
            completed_assignments.append(list(assignment))
            return (
                matching_strategy == "stable"
                and len(completed_assignments) > matching_variant
            )

        _, requested_key, _, _ = slot_records[slot_index]
        for unit_id in unit_ids:
            if unit_id in used_units or unit_type_by_id[unit_id] != requested_key:
                continue
            used_units.add(unit_id)
            assignment.append((unit_id, slot_index))
            if search(slot_index + 1, used_units, assignment):
                return True
            assignment.pop()
            used_units.remove(unit_id)
        return False

    search(0, set(), [])
    if matching_strategy == "order_preserving":
        unit_positions = unit_positions or {}

        def order_penalty(assignment):
            penalty = Fraction(0)
            for unit_id, slot_index in assignment:
                source_index, source_size = unit_positions.get(unit_id, (0, 1))
                _, _, target_index, target_size = slot_records[slot_index]
                if source_size > 1 and target_size > 1:
                    source_position = Fraction(source_index, source_size - 1)
                    target_position = Fraction(target_index, target_size - 1)
                    penalty += abs(source_position - target_position)
            return penalty

        completed_assignments.sort(
            key=lambda assignment: (order_penalty(assignment), assignment)
        )

    if matching_variant >= len(completed_assignments):
        raise ValueError(
            f"No complete precomputed matching exists for variant {matching_variant}; "
            f"found {len(completed_assignments)} matching variant(s)"
        )
    return completed_assignments[matching_variant]


def _build_service_track_ids(location_object):
    # Service tracks come from facilities[].relatedTrackParts for facilities with taskTypes.
    service_tracks = {}
    for facility in location_object.get("facilities", []):
        if facility.get("taskTypes"):
            for tp_id in facility.get("relatedTrackParts", []):
                service_tracks[str(tp_id)] = {
                    "type": facility["type"],
                    "capacity": facility.get("simultaneousUsageCount", 1),
                }
    return service_tracks


def _relevant_corridor_nodes(scenario_object, location_object, known_track_ids, coupling_candidate_track_ids, expand_hops=CORRIDOR_EXPAND_HOPS):
    # Restrict movement connectivity to the tracks that matter for this scenario: the nodes
    # on each type-compatible train's start -> coupling track -> exit/parking route, plus an
    # `expand_hops` neighborhood for maneuvering.
    _, adjacency = _build_side_aware_track_graph(location_object, known_track_ids)
    service_track_ids = _build_service_track_ids(location_object)
    path_nodes = set()

    def add_path(a, b):
        a = None if a is None else str(a)
        b = None if b is None else str(b)
        path_nodes.update(_shortest_path(adjacency, a, b))

    trains_with_starts = []
    for source, _, train in all_trains_with_source(scenario_object):
        preferred_keys = ["firstParkingTrackPart", "entryTrackPart"] if source == "inStanding" else ["entryTrackPart", "firstParkingTrackPart"]
        start_id = _train_initial_track_id(train, preferred_keys)
        if start_id is not None:
            trains_with_starts.append((train, str(start_id)))

    for request in scenario_object.get("out", {}).get("trainRequests", []):
        request_keys = _request_type_keys(request)
        coupling_ids = [str(c) for c in _coupling_track_ids_for_request(request, location_object, coupling_candidate_track_ids)]
        route_targets = [str(t) for t in [request.get("leaveTrackPart"), request.get("lastParkingTrackPart")] if t is not None]
        for train, start_id in trains_with_starts:
            if _train_unit_type_keys(train).isdisjoint(request_keys):
                continue
            service_ids = [
                track_id
                for track_id, info in service_track_ids.items()
                if info["type"] in _train_task_types(train)
            ]
            for coupling_id in coupling_ids:
                add_path(start_id, coupling_id)
                for service_id in service_ids:
                    add_path(start_id, service_id)
                    add_path(service_id, coupling_id)
                for target_id in route_targets:
                    add_path(coupling_id, target_id)

    for request in scenario_object.get("outStanding", {}).get("trainRequests", []):
        target_id = request.get("lastParkingTrackPart")
        if target_id is None:
            continue
        request_keys = _request_type_keys(request)
        for train, start_id in trains_with_starts:
            if _train_unit_type_keys(train).isdisjoint(request_keys):
                continue
            service_ids = [
                track_id
                for track_id, info in service_track_ids.items()
                if info["type"] in _train_task_types(train)
            ]
            add_path(start_id, str(target_id))
            for service_id in service_ids:
                add_path(start_id, service_id)
                add_path(service_id, str(target_id))

    if not path_nodes:
        return None

    # Count expansion hops on raw physical connections. Counting on the
    # switch-collapsed graph would make three hops cover almost the whole yard.
    raw_neighborhood = _build_adjacency(location_object)
    neighborhood = {
        str(node): {str(neighbor) for neighbor in neighbors}
        for node, neighbors in raw_neighborhood.items()
    }

    reached = set(path_nodes)
    frontier = set(path_nodes)
    for _ in range(expand_hops):
        nxt = set()
        for n in frontier:
            for m in neighborhood.get(n, ()):
                if m not in reached:
                    reached.add(m)
                    nxt.add(m)
        frontier = nxt
    known = {str(track_id) for track_id in known_track_ids}
    return reached.intersection(known)


def _train_total_length(train):
    # Sum the physical length of every unit in an arriving composition or outgoing request.
    total_length = Fraction(0)
    if "members" in train:
        for member in train.get("members", []):
            total_length += Fraction(str(member["trainUnit"]["type"]["length"]))
    elif "trainUnits" in train:
        for tu in train.get("trainUnits", []):
            total_length += Fraction(str(tu.get("type", {}).get("length", 0)))
    return total_length


def _train_unit_length(train_unit):
    # Physical length of one atomic train unit, used for single-unit shunting units.
    return Fraction(str(train_unit["type"]["length"]))


def _train_initial_track_id(train, preferred_keys):
    # Pick the first available track field for trains whose JSON shape differs by scenario block.
    for key in preferred_keys:
        if train.get(key) is not None:
            return train.get(key)
    return None


def _track_part_neighbors(track_part):
    # Return neighboring track-part ids from both sides in input order.
    neighbors = []
    seen = set()
    for side_key in ("aSide", "bSide"):
        for nb_id in track_part.get(side_key, []):
            if nb_id not in seen:
                seen.add(nb_id)
                neighbors.append(nb_id)
    return neighbors


def _is_switch_like_track_part(track_part):
    # No-switch modelling removes zero-length connector nodes and reconnects their boundaries.
    if track_part.get("parkingAllowed", False):
        return False
    try:
        length = Fraction(str(track_part.get("length", 0)))
    except Exception:
        length = Fraction(0)
    return length == 0 and len(_track_part_neighbors(track_part)) >= 2


def _build_directed_adj(location_object, side_key):
    adj = {tp["id"]: [] for tp in location_object["trackParts"]}
    for tp in location_object["trackParts"]:
        for nb_id in tp.get(side_key, []):
            if nb_id in adj:
                adj[tp["id"]].append(nb_id)
    return adj


def _bfs_through_switches(adj, start, switch_ids, allowed_ids):
    visited = {start}
    queue = deque([start])
    reachable = set()
    while queue:
        node = queue.popleft()
        for neighbor in adj.get(node, []):
            if neighbor in allowed_ids and neighbor != start:
                reachable.add(neighbor)
            elif neighbor in switch_ids and neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)
    return reachable


def _build_side_aware_track_graph(location_object, allowed_track_ids=None):
    """Build the exact no-switch movement graph while retaining A/B edge labels."""
    switch_ids = {
        tp["id"]
        for tp in location_object["trackParts"]
        if _is_switch_like_track_part(tp)
    }
    non_switch_ids = {
        tp["id"]
        for tp in location_object["trackParts"]
        if tp["id"] not in switch_ids
    }
    if allowed_track_ids is not None:
        allowed = {str(track_id) for track_id in allowed_track_ids}
        non_switch_ids = {
            track_id for track_id in non_switch_ids if str(track_id) in allowed
        }

    a_adj = _build_directed_adj(location_object, "aSide")
    b_adj = _build_directed_adj(location_object, "bSide")
    side_graph = {
        str(track_id): {"a": set(), "b": set()}
        for track_id in non_switch_ids
    }

    # These are the same switch paths later emitted as connected_aside/bside facts.
    for source_id in non_switch_ids:
        source = str(source_id)
        side_graph[source]["a"].update(
            str(target_id)
            for target_id in _bfs_through_switches(
                a_adj, source_id, switch_ids, non_switch_ids
            )
            if target_id != source_id
        )
        side_graph[source]["b"].update(
            str(target_id)
            for target_id in _bfs_through_switches(
                b_adj, source_id, switch_ids, non_switch_ids
            )
            if target_id != source_id
        )

    movement_graph = {
        source: sides["a"] | sides["b"]
        for source, sides in side_graph.items()
    }
    return side_graph, movement_graph


def _train_object_name(source, index, train):
    # Reuse the routing branch's standing-train naming convention.
    if source == "inStanding":
        return f"train_in_standing_{index}"
    return "train" + train["id"]


def create_instance_from_scenario(path_to_folder=None, scenario_file=None, location_file=None, output_file=None, domain_file=None, precompute_matching=False, matching_variant=0, matching_strategy="stable"):
    if path_to_folder is None:
        path_to_folder = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "scenario-planning-inputs", "Location_KleineBinckhorst")

    if location_file is None:
        location_file = os.path.join(path_to_folder, "location_solver.json")
    elif not os.sep in location_file:
        location_file = os.path.join(path_to_folder, location_file)

    if scenario_file is None:
        scenario_file = os.path.join(path_to_folder, "scenarios", "scenario_solver_example1.json")
        scenario_name = "scenario_solver_example1"
    elif os.sep not in scenario_file:
        scenario_name = scenario_file.replace(".json", "")
        scenario_file = os.path.join(path_to_folder, "scenarios", scenario_file)
    else:
        scenario_name = scenario_file.split(os.sep)[-1].replace(".json", "")

    location_object = json.load(open(location_file))
    scenario_object = json.load(open(scenario_file))

    if precompute_matching and matching_strategy == "order_preserving_auto":
        precompute_matching = _has_order_sensitive_matching(scenario_object)
        matching_strategy = "order_preserving"
        mode = "precomputed" if precompute_matching else "planner-controlled"
        print(f"Matching mode: {mode}")

    problem = up.Problem(scenario_name)
    track_part_type = up.UserType("trackpart")
    train_unit_type = up.UserType("trainunit")
    departure_request_type = up.UserType("departurerequest")
    request_slot_type = up.UserType("requestslot")
    arrival_composition_type = up.UserType("arrivalcomposition")
    shunting_unit_type = up.UserType("shuntingunit")
    parking_request_type = up.UserType("parkingrequest")
    parking_slot_type = up.UserType("parkingslot")
    shunting_composition_type = up.UserType("shuntingcomposition")

    parking_allowed = problem.add_fluent(up.Fluent("parking_allowed", up.BoolType(), trackpart=track_part_type),                         default_initial_value=False)
    turning_allowed = problem.add_fluent(up.Fluent("turning_allowed", up.BoolType(), trackpart=track_part_type),                         default_initial_value=False)
    connected_aside = problem.add_fluent(up.Fluent("connected_aside", up.BoolType(), from_=track_part_type, to=track_part_type),           default_initial_value=False)
    connected_bside = problem.add_fluent(up.Fluent("connected_bside", up.BoolType(), from_=track_part_type, to=track_part_type),           default_initial_value=False)
    departure_exit_a = problem.add_fluent(up.Fluent("departure_exit_a", up.BoolType(), trackpart=track_part_type),                          default_initial_value=False)
    departure_exit_b = problem.add_fluent(up.Fluent("departure_exit_b", up.BoolType(), trackpart=track_part_type),                          default_initial_value=False)
    entry_distance = problem.add_fluent(up.Fluent("entry_distance", up.IntType(),  trackpart=track_part_type),                          default_initial_value=up.Int(0))
    number_of_parked_trains = problem.add_fluent(up.Fluent("number_of_parked_trains", up.IntType(), trackpart=track_part_type),                 default_initial_value=up.Int(0))
    number_of_trains_on_track = problem.add_fluent(up.Fluent("number_of_trains_on_track", up.IntType(), trackpart=track_part_type),                 default_initial_value=up.Int(0))
    num_of_departed_trains = problem.add_fluent(up.Fluent("num_of_departed_trains", up.IntType()),                                      default_initial_value=up.Int(0))
    track_length = problem.add_fluent(up.Fluent("track_length", up.RealType(), trackpart=track_part_type),                          default_initial_value=up.Real(Fraction(0)))
    concurrent_movements = problem.add_fluent(up.Fluent("concurrent_movements", up.IntType()), default_initial_value=up.Int(0))
    max_concurrent_movements = 1

    available      = problem.add_fluent(up.Fluent("available",      up.BoolType(), unit=train_unit_type),                               default_initial_value=False)
    request_open   = problem.add_fluent(up.Fluent("request_open",   up.BoolType(), request=departure_request_type),                     default_initial_value=False)
    slot_open      = problem.add_fluent(up.Fluent("slot_open",      up.BoolType(), slot=request_slot_type),                             default_initial_value=False)
    slot_filled    = problem.add_fluent(up.Fluent("slot_filled",    up.BoolType(), slot=request_slot_type),                             default_initial_value=False)
    compatible     = problem.add_fluent(up.Fluent("compatible",     up.BoolType(), unit=train_unit_type, slot=request_slot_type),        default_initial_value=False)
    matched        = problem.add_fluent(up.Fluent("matched",        up.BoolType(), unit=train_unit_type, slot=request_slot_type),        default_initial_value=False)
    slot_for_request = problem.add_fluent(up.Fluent("slot_for_request", up.BoolType(), slot=request_slot_type, request=departure_request_type), default_initial_value=False)
    slot_before = problem.add_fluent(up.Fluent("slot_before", up.BoolType(), first=request_slot_type, second=request_slot_type), default_initial_value=False)
    unit_before = problem.add_fluent(up.Fluent("unit_before", up.BoolType(), first=train_unit_type, second=train_unit_type), default_initial_value=False)
    coupling_allowed = problem.add_fluent(up.Fluent("coupling_allowed", up.BoolType(), trackpart=track_part_type), default_initial_value=False)
    coupling_track_for_request = problem.add_fluent(up.Fluent("coupling_track_for_request", up.BoolType(), request=departure_request_type, trackpart=track_part_type), default_initial_value=False)

    active_su        = problem.add_fluent(up.Fluent("active_su", up.BoolType(), shunting_unit=shunting_unit_type), default_initial_value=False)
    contains_su      = problem.add_fluent(up.Fluent("contains_su", up.BoolType(), shunting_unit=shunting_unit_type, unit=train_unit_type), default_initial_value=False)
    at_su            = problem.add_fluent(up.Fluent("at_su", up.BoolType(), shunting_unit=shunting_unit_type, trackpart=track_part_type), default_initial_value=False)
    departed_su      = problem.add_fluent(up.Fluent("departed_su", up.BoolType(), shunting_unit=shunting_unit_type), default_initial_value=False)
    single_unit_su   = problem.add_fluent(up.Fluent("single_unit_su", up.BoolType(), shunting_unit=shunting_unit_type, unit=train_unit_type), default_initial_value=False)
    request_su_for_request = problem.add_fluent(up.Fluent("request_su_for_request", up.BoolType(), shunting_unit=shunting_unit_type, request=departure_request_type), default_initial_value=False)
    request_departed = problem.add_fluent(up.Fluent("request_departed", up.BoolType(), request=departure_request_type), default_initial_value=False)
    su_length        = problem.add_fluent(up.Fluent("su_length", up.RealType(), shunting_unit=shunting_unit_type), default_initial_value=up.Real(Fraction(0)))
    occupied_length = problem.add_fluent(up.Fluent("occupied_length", up.RealType(), trackpart=track_part_type), default_initial_value=up.Real(Fraction(0)))
    frontmost_a_su   = problem.add_fluent(up.Fluent("frontmost_a_su", up.BoolType(), shunting_unit=shunting_unit_type), default_initial_value=False)
    frontmost_b_su   = problem.add_fluent(up.Fluent("frontmost_b_su", up.BoolType(), shunting_unit=shunting_unit_type), default_initial_value=False)
    behind_su        = problem.add_fluent(up.Fluent("behind_su", up.BoolType(), back=shunting_unit_type, front=shunting_unit_type), default_initial_value=False)
    allowed_to_move_su = problem.add_fluent(up.Fluent("allowed_to_move_su", up.BoolType(), shunting_unit=shunting_unit_type), default_initial_value=False)
    su_may_move       = problem.add_fluent(up.Fluent("su_may_move", up.BoolType(), shunting_unit=shunting_unit_type), default_initial_value=False)
    must_depart_su    = problem.add_fluent(up.Fluent("must_depart_su", up.BoolType(), shunting_unit=shunting_unit_type), default_initial_value=False)
    parked_su         = problem.add_fluent(up.Fluent("parked_su", up.BoolType(), shunting_unit=shunting_unit_type), default_initial_value=False)
    su_has_arrived = problem.add_fluent(up.Fluent("su_has_arrived", up.BoolType(), shunting_unit=shunting_unit_type), default_initial_value=True)
    su_previous_arrived = problem.add_fluent(up.Fluent("su_previous_arrived", up.BoolType(), shunting_unit=shunting_unit_type), default_initial_value=False)
    su_arrival_immediately_before = problem.add_fluent(up.Fluent("su_arrival_immediately_before", up.BoolType(), first=shunting_unit_type, second=shunting_unit_type), default_initial_value=False)

    phantom_track = problem.add_object("phantom", track_part_type)
    su_arrival_track = problem.add_fluent(up.Fluent("su_arrival_track", up.BoolType(), su=shunting_unit_type, track=track_part_type), default_initial_value=False)

    parking_slot_for_request = problem.add_fluent(up.Fluent("parking_slot_for_request", up.BoolType(), slot=parking_slot_type, request=parking_request_type), default_initial_value=False)
    parking_slot_track = problem.add_fluent(up.Fluent("parking_slot_track", up.BoolType(), slot=parking_slot_type, track=track_part_type), default_initial_value=False)
    parking_compatible = problem.add_fluent(up.Fluent("parking_compatible", up.BoolType(), unit=train_unit_type, slot=parking_slot_type), default_initial_value=False)
    parking_slot_fulfilled = problem.add_fluent(up.Fluent("parking_slot_fulfilled", up.BoolType(), slot=parking_slot_type), default_initial_value=False)
    parked_unit_used = problem.add_fluent(up.Fluent("parked_unit_used", up.BoolType(), unit=train_unit_type), default_initial_value=False)

    service_track_ids = _build_service_track_ids(location_object)
    facility_type_type = up.UserType("facilitytype")
    service_allowed   = problem.add_fluent(up.Fluent("service_allowed", up.BoolType(), trackpart=track_part_type), default_initial_value=False)
    facility_type     = problem.add_fluent(up.Fluent("facility_type", up.BoolType(), trackpart=track_part_type, ftype=facility_type_type), default_initial_value=False)
    requires_facility = problem.add_fluent(up.Fluent("requires_facility", up.BoolType(), shunting_unit=shunting_unit_type, ftype=facility_type_type), default_initial_value=False)
    serviced          = problem.add_fluent(up.Fluent("serviced", up.BoolType(), shunting_unit=shunting_unit_type), default_initial_value=True)


    front_of = problem.add_fluent(up.Fluent("front_of", up.BoolType(), unit=train_unit_type, su=shunting_unit_type),default_initial_value=False)
    back_of = problem.add_fluent(up.Fluent("back_of", up.BoolType(), unit=train_unit_type, su=shunting_unit_type), default_initial_value=False)
    next_in_su = problem.add_fluent(up.Fluent("next_in_su", up.BoolType(), front=train_unit_type, back=train_unit_type, su=shunting_unit_type),default_initial_value=False,)
    front_slot = problem.add_fluent(up.Fluent("front_slot", up.BoolType(), slot=request_slot_type, su=shunting_unit_type), default_initial_value=False)
    back_slot = problem.add_fluent(up.Fluent("back_slot", up.BoolType(), slot=request_slot_type, su=shunting_unit_type), default_initial_value=False)
    su_unit_count = problem.add_fluent(up.Fluent("su_unit_count", up.IntType(), shunting_unit=shunting_unit_type), default_initial_value=up.Int(0))
    request_size = problem.add_fluent(up.Fluent("request_size", up.IntType(), request=departure_request_type), default_initial_value=up.Int(0))

    id_to_facility_type = {ftype_str: problem.add_object(ftype_str.lower(), facility_type_type)
                           for ftype_str in {info["type"] for info in service_track_ids.values()}}

    startMoveSu = up.InstantaneousAction('start_move_su', su=shunting_unit_type)
    startMoveSu.add_precondition(active_su(startMoveSu.su))
    startMoveSu.add_precondition(up.Not(allowed_to_move_su(startMoveSu.su)))
    startMoveSu.add_precondition(concurrent_movements < max_concurrent_movements)
    startMoveSu.add_precondition(su_may_move(startMoveSu.su))
    startMoveSu.add_precondition(up.Not(parked_su(startMoveSu.su)))
    startMoveSu.add_precondition(su_has_arrived(startMoveSu.su))
    startMoveSu.add_effect(allowed_to_move_su(startMoveSu.su), True)
    startMoveSu.add_effect(concurrent_movements, concurrent_movements + 1)
    problem.add_action(startMoveSu)

    arrive_su = up.InstantaneousAction('arrive_su', su=shunting_unit_type, l=track_part_type)
    arrive_su.add_precondition(active_su(arrive_su.su))
    arrive_su.add_precondition(up.Not(su_has_arrived(arrive_su.su)))
    arrive_su.add_precondition(su_previous_arrived(arrive_su.su))
    arrive_su.add_precondition(at_su(arrive_su.su, phantom_track))
    arrive_su.add_precondition(concurrent_movements < max_concurrent_movements)
    arrive_su.add_precondition(su_arrival_track(arrive_su.su, arrive_su.l))
    arrive_su.add_effect(su_has_arrived(arrive_su.su), True)
    arrive_su.add_effect(allowed_to_move_su(arrive_su.su), True)
    arrive_su.add_effect(concurrent_movements, concurrent_movements + 1)
    arrive_su.add_effect(at_su(arrive_su.su, phantom_track), False)
    arrive_su.add_effect(at_su(arrive_su.su, arrive_su.l), True)
    pass
    arrive_su.add_effect(number_of_trains_on_track(arrive_su.l), number_of_trains_on_track(arrive_su.l) + 1)
    pass
    next_su = up.Variable("next_su", shunting_unit_type)
    arrive_su.add_effect(fluent=su_previous_arrived(next_su), value=True, condition=su_arrival_immediately_before(arrive_su.su, next_su), forall=[next_su])
    _arrq = up.Variable("arrq", shunting_unit_type)
    arrive_su.add_precondition(occupied_length(arrive_su.l) + su_length(arrive_su.su) <= track_length(arrive_su.l))
    arrive_su.add_effect(occupied_length(arrive_su.l), occupied_length(arrive_su.l) + su_length(arrive_su.su))
    arrive_su.add_effect(frontmost_b_su(arrive_su.su), True)
    arrive_su.add_effect(fluent=frontmost_a_su(arrive_su.su), value=True, condition=up.Equals(number_of_trains_on_track(arrive_su.l), 0))
    arrive_su.add_effect(fluent=frontmost_b_su(_arrq), value=False, condition=up.And(at_su(_arrq, arrive_su.l), frontmost_b_su(_arrq)), forall=[_arrq])
    arrive_su.add_effect(fluent=behind_su(arrive_su.su, _arrq), value=True, condition=up.And(at_su(_arrq, arrive_su.l), frontmost_b_su(_arrq)), forall=[_arrq])
    problem.add_action(arrive_su)

    park_su = up.InstantaneousAction('park_su', su=shunting_unit_type, l=track_part_type)
    park_su.add_precondition(active_su(park_su.su))
    park_su.add_precondition(allowed_to_move_su(park_su.su))
    park_su.add_precondition(at_su(park_su.su, park_su.l))
    park_su.add_precondition(parking_allowed(park_su.l))
    park_su.add_precondition(up.Not(must_depart_su(park_su.su)))
    park_su.add_precondition(up.Not(parked_su(park_su.su)))
    park_su.add_effect(parked_su(park_su.su), True)
    park_su.add_effect(number_of_parked_trains(park_su.l), number_of_parked_trains(park_su.l) + 1)
    park_su.add_effect(allowed_to_move_su(park_su.su), False)
    park_su.add_effect(concurrent_movements, concurrent_movements - 1)
    problem.add_action(park_su)

    endMoveSu = up.InstantaneousAction('end_move_su', su=shunting_unit_type, l=track_part_type)
    endMoveSu.add_precondition(active_su(endMoveSu.su))
    endMoveSu.add_precondition(allowed_to_move_su(endMoveSu.su))
    endMoveSu.add_precondition(at_su(endMoveSu.su, endMoveSu.l))
    endMoveSu.add_precondition(parking_allowed(endMoveSu.l))
    endMoveSu.add_precondition(up.Not(must_depart_su(endMoveSu.su)))
    endMoveSu.add_effect(allowed_to_move_su(endMoveSu.su), False)
    endMoveSu.add_effect(concurrent_movements, concurrent_movements - 1)
    problem.add_action(endMoveSu)

    move_aside_empty_su = up.InstantaneousAction('move_aside_empty_su', su=shunting_unit_type, l_from=track_part_type, l_to=track_part_type)
    move_aside_empty_su.add_precondition(active_su(move_aside_empty_su.su))
    move_aside_empty_su.add_precondition(allowed_to_move_su(move_aside_empty_su.su))
    move_aside_empty_su.add_precondition(at_su(move_aside_empty_su.su, move_aside_empty_su.l_from))
    move_aside_empty_su.add_precondition(connected_aside(move_aside_empty_su.l_from, move_aside_empty_su.l_to))
    move_aside_empty_su.add_precondition(occupied_length(move_aside_empty_su.l_to) + su_length(move_aside_empty_su.su) <= track_length(move_aside_empty_su.l_to))
    pass
    move_aside_empty_su.add_precondition(up.Equals(number_of_trains_on_track(move_aside_empty_su.l_to), 0))
    move_aside_empty_su.add_precondition(su_length(move_aside_empty_su.su) <= track_length(move_aside_empty_su.l_to))
    move_aside_empty_su.add_effect(number_of_trains_on_track(move_aside_empty_su.l_from), number_of_trains_on_track(move_aside_empty_su.l_from) - 1)
    move_aside_empty_su.add_effect(number_of_trains_on_track(move_aside_empty_su.l_to), 1)
    pass
    pass
    pass
    pass
    move_aside_empty_su.add_effect(occupied_length(move_aside_empty_su.l_from), occupied_length(move_aside_empty_su.l_from) - su_length(move_aside_empty_su.su))
    move_aside_empty_su.add_effect(occupied_length(move_aside_empty_su.l_to), occupied_length(move_aside_empty_su.l_to) + su_length(move_aside_empty_su.su))
    move_aside_empty_su.add_effect(at_su(move_aside_empty_su.su, move_aside_empty_su.l_to), True)
    move_aside_empty_su.add_effect(at_su(move_aside_empty_su.su, move_aside_empty_su.l_from), False)
    _maev = up.Variable("maev", shunting_unit_type)
    move_aside_empty_su.add_precondition(frontmost_a_su(move_aside_empty_su.su))
    move_aside_empty_su.add_effect(fluent=frontmost_a_su(_maev), value=True, condition=behind_su(_maev, move_aside_empty_su.su), forall=[_maev])
    move_aside_empty_su.add_effect(fluent=behind_su(_maev, move_aside_empty_su.su), value=False, condition=behind_su(_maev, move_aside_empty_su.su), forall=[_maev])
    move_aside_empty_su.add_effect(frontmost_a_su(move_aside_empty_su.su), True)
    move_aside_empty_su.add_effect(frontmost_b_su(move_aside_empty_su.su), True)
    problem.add_action(move_aside_empty_su)

    move_aside_occupied_su = up.InstantaneousAction('move_aside_occupied_su', su=shunting_unit_type, l_from=track_part_type, l_to=track_part_type)
    move_aside_occupied_su.add_precondition(active_su(move_aside_occupied_su.su))
    move_aside_occupied_su.add_precondition(allowed_to_move_su(move_aside_occupied_su.su))
    move_aside_occupied_su.add_precondition(at_su(move_aside_occupied_su.su, move_aside_occupied_su.l_from))
    move_aside_occupied_su.add_precondition(connected_aside(move_aside_occupied_su.l_from, move_aside_occupied_su.l_to))
    move_aside_occupied_su.add_precondition(occupied_length(move_aside_occupied_su.l_to) + su_length(move_aside_occupied_su.su) <= track_length(move_aside_occupied_su.l_to))
    pass
    move_aside_occupied_su.add_precondition(number_of_trains_on_track(move_aside_occupied_su.l_to) > 0)
    pass
    move_aside_occupied_su.add_effect(number_of_trains_on_track(move_aside_occupied_su.l_from), number_of_trains_on_track(move_aside_occupied_su.l_from) - 1)
    move_aside_occupied_su.add_effect(number_of_trains_on_track(move_aside_occupied_su.l_to), number_of_trains_on_track(move_aside_occupied_su.l_to) + 1)
    pass
    pass
    pass
    move_aside_occupied_su.add_effect(occupied_length(move_aside_occupied_su.l_from), occupied_length(move_aside_occupied_su.l_from) - su_length(move_aside_occupied_su.su))
    move_aside_occupied_su.add_effect(occupied_length(move_aside_occupied_su.l_to), occupied_length(move_aside_occupied_su.l_to) + su_length(move_aside_occupied_su.su))
    move_aside_occupied_su.add_effect(at_su(move_aside_occupied_su.su, move_aside_occupied_su.l_to), True)
    move_aside_occupied_su.add_effect(at_su(move_aside_occupied_su.su, move_aside_occupied_su.l_from), False)
    _maov = up.Variable("maov", shunting_unit_type)
    _maop = up.Variable("maop", shunting_unit_type)
    move_aside_occupied_su.add_precondition(frontmost_a_su(move_aside_occupied_su.su))
    move_aside_occupied_su.add_effect(fluent=frontmost_a_su(_maov), value=True, condition=behind_su(_maov, move_aside_occupied_su.su), forall=[_maov])
    move_aside_occupied_su.add_effect(fluent=behind_su(_maov, move_aside_occupied_su.su), value=False, condition=behind_su(_maov, move_aside_occupied_su.su), forall=[_maov])
    move_aside_occupied_su.add_effect(fluent=frontmost_a_su(_maop), value=False, condition=up.And(at_su(_maop, move_aside_occupied_su.l_to), frontmost_a_su(_maop)), forall=[_maop])
    move_aside_occupied_su.add_effect(fluent=behind_su(_maop, move_aside_occupied_su.su), value=True, condition=up.And(at_su(_maop, move_aside_occupied_su.l_to), frontmost_a_su(_maop)), forall=[_maop])
    move_aside_occupied_su.add_effect(frontmost_a_su(move_aside_occupied_su.su), True)
    move_aside_occupied_su.add_effect(frontmost_b_su(move_aside_occupied_su.su), False)
    problem.add_action(move_aside_occupied_su)

    move_bside_empty_su = up.InstantaneousAction('move_bside_empty_su', su=shunting_unit_type, l_from=track_part_type, l_to=track_part_type)
    move_bside_empty_su.add_precondition(active_su(move_bside_empty_su.su))
    move_bside_empty_su.add_precondition(allowed_to_move_su(move_bside_empty_su.su))
    move_bside_empty_su.add_precondition(at_su(move_bside_empty_su.su, move_bside_empty_su.l_from))
    move_bside_empty_su.add_precondition(connected_bside(move_bside_empty_su.l_from, move_bside_empty_su.l_to))
    move_bside_empty_su.add_precondition(occupied_length(move_bside_empty_su.l_to) + su_length(move_bside_empty_su.su) <= track_length(move_bside_empty_su.l_to))
    pass
    move_bside_empty_su.add_precondition(up.Equals(number_of_trains_on_track(move_bside_empty_su.l_to), 0))
    move_bside_empty_su.add_precondition(su_length(move_bside_empty_su.su) <= track_length(move_bside_empty_su.l_to))
    move_bside_empty_su.add_effect(number_of_trains_on_track(move_bside_empty_su.l_from), number_of_trains_on_track(move_bside_empty_su.l_from) - 1)
    move_bside_empty_su.add_effect(number_of_trains_on_track(move_bside_empty_su.l_to), 1)
    pass
    pass
    pass
    pass
    move_bside_empty_su.add_effect(occupied_length(move_bside_empty_su.l_from), occupied_length(move_bside_empty_su.l_from) - su_length(move_bside_empty_su.su))
    move_bside_empty_su.add_effect(occupied_length(move_bside_empty_su.l_to), occupied_length(move_bside_empty_su.l_to) + su_length(move_bside_empty_su.su))
    move_bside_empty_su.add_effect(at_su(move_bside_empty_su.su, move_bside_empty_su.l_to), True)
    move_bside_empty_su.add_effect(at_su(move_bside_empty_su.su, move_bside_empty_su.l_from), False)
    _mbev = up.Variable("mbev", shunting_unit_type)
    move_bside_empty_su.add_precondition(frontmost_b_su(move_bside_empty_su.su))
    move_bside_empty_su.add_effect(fluent=frontmost_b_su(_mbev), value=True, condition=behind_su(move_bside_empty_su.su, _mbev), forall=[_mbev])
    move_bside_empty_su.add_effect(fluent=behind_su(move_bside_empty_su.su, _mbev), value=False, condition=behind_su(move_bside_empty_su.su, _mbev), forall=[_mbev])
    move_bside_empty_su.add_effect(frontmost_a_su(move_bside_empty_su.su), True)
    move_bside_empty_su.add_effect(frontmost_b_su(move_bside_empty_su.su), True)
    problem.add_action(move_bside_empty_su)

    move_bside_occupied_su = up.InstantaneousAction('move_bside_occupied_su', su=shunting_unit_type, l_from=track_part_type, l_to=track_part_type)
    move_bside_occupied_su.add_precondition(active_su(move_bside_occupied_su.su))
    move_bside_occupied_su.add_precondition(allowed_to_move_su(move_bside_occupied_su.su))
    move_bside_occupied_su.add_precondition(at_su(move_bside_occupied_su.su, move_bside_occupied_su.l_from))
    move_bside_occupied_su.add_precondition(connected_bside(move_bside_occupied_su.l_from, move_bside_occupied_su.l_to))
    move_bside_occupied_su.add_precondition(occupied_length(move_bside_occupied_su.l_to) + su_length(move_bside_occupied_su.su) <= track_length(move_bside_occupied_su.l_to))
    pass
    move_bside_occupied_su.add_precondition(number_of_trains_on_track(move_bside_occupied_su.l_to) > 0)
    pass
    move_bside_occupied_su.add_effect(number_of_trains_on_track(move_bside_occupied_su.l_from), number_of_trains_on_track(move_bside_occupied_su.l_from) - 1)
    move_bside_occupied_su.add_effect(number_of_trains_on_track(move_bside_occupied_su.l_to), number_of_trains_on_track(move_bside_occupied_su.l_to) + 1)
    pass
    pass
    pass
    move_bside_occupied_su.add_effect(occupied_length(move_bside_occupied_su.l_from), occupied_length(move_bside_occupied_su.l_from) - su_length(move_bside_occupied_su.su))
    move_bside_occupied_su.add_effect(occupied_length(move_bside_occupied_su.l_to), occupied_length(move_bside_occupied_su.l_to) + su_length(move_bside_occupied_su.su))
    move_bside_occupied_su.add_effect(at_su(move_bside_occupied_su.su, move_bside_occupied_su.l_to), True)
    move_bside_occupied_su.add_effect(at_su(move_bside_occupied_su.su, move_bside_occupied_su.l_from), False)
    _mbov = up.Variable("mbov", shunting_unit_type)
    _mbop = up.Variable("mbop", shunting_unit_type)
    move_bside_occupied_su.add_precondition(frontmost_b_su(move_bside_occupied_su.su))
    move_bside_occupied_su.add_effect(fluent=frontmost_b_su(_mbov), value=True, condition=behind_su(move_bside_occupied_su.su, _mbov), forall=[_mbov])
    move_bside_occupied_su.add_effect(fluent=behind_su(move_bside_occupied_su.su, _mbov), value=False, condition=behind_su(move_bside_occupied_su.su, _mbov), forall=[_mbov])
    move_bside_occupied_su.add_effect(fluent=frontmost_b_su(_mbop), value=False, condition=up.And(at_su(_mbop, move_bside_occupied_su.l_to), frontmost_b_su(_mbop)), forall=[_mbop])
    move_bside_occupied_su.add_effect(fluent=behind_su(move_bside_occupied_su.su, _mbop), value=True, condition=up.And(at_su(_mbop, move_bside_occupied_su.l_to), frontmost_b_su(_mbop)), forall=[_mbop])
    move_bside_occupied_su.add_effect(frontmost_b_su(move_bside_occupied_su.su), True)
    move_bside_occupied_su.add_effect(frontmost_a_su(move_bside_occupied_su.su), False)
    problem.add_action(move_bside_occupied_su)

    depart_aside_su = up.InstantaneousAction('depart_aside_su', su=shunting_unit_type, l=track_part_type)
    depart_aside_su.add_precondition(active_su(depart_aside_su.su))
    depart_aside_su.add_precondition(allowed_to_move_su(depart_aside_su.su))
    depart_aside_su.add_precondition(at_su(depart_aside_su.su, depart_aside_su.l))
    depart_aside_su.add_precondition(departure_exit_a(depart_aside_su.l))
    pass
    depart_aside_su.add_effect(active_su(depart_aside_su.su), False)
    depart_aside_su.add_effect(at_su(depart_aside_su.su, depart_aside_su.l), False)
    depart_aside_su.add_effect(at_su(depart_aside_su.su, phantom_track), True)
    depart_aside_su.add_effect(departed_su(depart_aside_su.su), True)
    depart_aside_su.add_effect(occupied_length(depart_aside_su.l), occupied_length(depart_aside_su.l) - su_length(depart_aside_su.su))
    depart_aside_su.add_effect(number_of_trains_on_track(depart_aside_su.l), number_of_trains_on_track(depart_aside_su.l) - 1)
    pass
    pass
    depart_aside_su.add_effect(concurrent_movements, concurrent_movements - 1)
    depart_aside_su.add_effect(allowed_to_move_su(depart_aside_su.su), False)
    depart_aside_su.add_effect(num_of_departed_trains(), num_of_departed_trains() + 1)
    _v_depart_aside_su = up.Variable("v_depart_aside_su", shunting_unit_type)
    depart_aside_su.add_precondition(frontmost_a_su(depart_aside_su.su))
    depart_aside_su.add_effect(fluent=frontmost_a_su(_v_depart_aside_su), value=True, condition=behind_su(_v_depart_aside_su, depart_aside_su.su), forall=[_v_depart_aside_su])
    depart_aside_su.add_effect(fluent=behind_su(_v_depart_aside_su, depart_aside_su.su), value=False, condition=behind_su(_v_depart_aside_su, depart_aside_su.su), forall=[_v_depart_aside_su])
    depart_aside_su.add_effect(frontmost_a_su(depart_aside_su.su), False)
    problem.add_action(depart_aside_su)

    depart_bside_su = up.InstantaneousAction('depart_bside_su', su=shunting_unit_type, l=track_part_type)
    depart_bside_su.add_precondition(active_su(depart_bside_su.su))
    depart_bside_su.add_precondition(allowed_to_move_su(depart_bside_su.su))
    depart_bside_su.add_precondition(at_su(depart_bside_su.su, depart_bside_su.l))
    depart_bside_su.add_precondition(departure_exit_b(depart_bside_su.l))
    pass
    depart_bside_su.add_effect(active_su(depart_bside_su.su), False)
    depart_bside_su.add_effect(at_su(depart_bside_su.su, depart_bside_su.l), False)
    depart_bside_su.add_effect(at_su(depart_bside_su.su, phantom_track), True)
    depart_bside_su.add_effect(departed_su(depart_bside_su.su), True)
    depart_bside_su.add_effect(occupied_length(depart_bside_su.l), occupied_length(depart_bside_su.l) - su_length(depart_bside_su.su))
    depart_bside_su.add_effect(number_of_trains_on_track(depart_bside_su.l), number_of_trains_on_track(depart_bside_su.l) - 1)
    pass
    pass
    depart_bside_su.add_effect(concurrent_movements, concurrent_movements - 1)
    depart_bside_su.add_effect(allowed_to_move_su(depart_bside_su.su), False)
    depart_bside_su.add_effect(num_of_departed_trains(), num_of_departed_trains() + 1)
    _v_depart_bside_su = up.Variable("v_depart_bside_su", shunting_unit_type)
    depart_bside_su.add_precondition(frontmost_b_su(depart_bside_su.su))
    depart_bside_su.add_effect(fluent=frontmost_b_su(_v_depart_bside_su), value=True, condition=behind_su(depart_bside_su.su, _v_depart_bside_su), forall=[_v_depart_bside_su])
    depart_bside_su.add_effect(fluent=behind_su(depart_bside_su.su, _v_depart_bside_su), value=False, condition=behind_su(depart_bside_su.su, _v_depart_bside_su), forall=[_v_depart_bside_su])
    depart_bside_su.add_effect(frontmost_b_su(depart_bside_su.su), False)
    problem.add_action(depart_bside_su)

    depart_aside_su_for_request = up.InstantaneousAction(
        'depart_aside_su_for_request',
        su=shunting_unit_type,
        unit=train_unit_type,
        slot=request_slot_type,
        request=departure_request_type,
        l=track_part_type,
    )
    depart_aside_su_for_request.add_precondition(active_su(depart_aside_su_for_request.su))
    depart_aside_su_for_request.add_precondition(allowed_to_move_su(depart_aside_su_for_request.su))
    depart_aside_su_for_request.add_precondition(contains_su(depart_aside_su_for_request.su, depart_aside_su_for_request.unit))
    depart_aside_su_for_request.add_precondition(single_unit_su(depart_aside_su_for_request.su, depart_aside_su_for_request.unit))
    depart_aside_su_for_request.add_precondition(matched(depart_aside_su_for_request.unit, depart_aside_su_for_request.slot))
    depart_aside_su_for_request.add_precondition(slot_for_request(depart_aside_su_for_request.slot, depart_aside_su_for_request.request))
    depart_aside_su_for_request.add_precondition(at_su(depart_aside_su_for_request.su, depart_aside_su_for_request.l))
    depart_aside_su_for_request.add_precondition(departure_exit_a(depart_aside_su_for_request.l))
    pass
    depart_aside_su_for_request.add_effect(active_su(depart_aside_su_for_request.su), False)
    depart_aside_su_for_request.add_effect(at_su(depart_aside_su_for_request.su, depart_aside_su_for_request.l), False)
    depart_aside_su_for_request.add_effect(at_su(depart_aside_su_for_request.su, phantom_track), True)
    depart_aside_su_for_request.add_effect(departed_su(depart_aside_su_for_request.su), True)
    depart_aside_su_for_request.add_effect(occupied_length(depart_aside_su_for_request.l), occupied_length(depart_aside_su_for_request.l) - su_length(depart_aside_su_for_request.su))
    depart_aside_su_for_request.add_effect(request_departed(depart_aside_su_for_request.request), True)
    depart_aside_su_for_request.add_effect(num_of_departed_trains(), num_of_departed_trains() + 1)
    depart_aside_su_for_request.add_effect(number_of_trains_on_track(depart_aside_su_for_request.l), number_of_trains_on_track(depart_aside_su_for_request.l) - 1)
    pass
    pass
    depart_aside_su_for_request.add_effect(concurrent_movements, concurrent_movements - 1)
    depart_aside_su_for_request.add_effect(allowed_to_move_su(depart_aside_su_for_request.su), False)
    _v_depart_aside_su_for_request = up.Variable("v_depart_aside_su_for_request", shunting_unit_type)
    depart_aside_su_for_request.add_precondition(frontmost_a_su(depart_aside_su_for_request.su))
    depart_aside_su_for_request.add_effect(fluent=frontmost_a_su(_v_depart_aside_su_for_request), value=True, condition=behind_su(_v_depart_aside_su_for_request, depart_aside_su_for_request.su), forall=[_v_depart_aside_su_for_request])
    depart_aside_su_for_request.add_effect(fluent=behind_su(_v_depart_aside_su_for_request, depart_aside_su_for_request.su), value=False, condition=behind_su(_v_depart_aside_su_for_request, depart_aside_su_for_request.su), forall=[_v_depart_aside_su_for_request])
    depart_aside_su_for_request.add_effect(frontmost_a_su(depart_aside_su_for_request.su), False)
    problem.add_action(depart_aside_su_for_request)

    depart_bside_su_for_request = up.InstantaneousAction(
        'depart_bside_su_for_request',
        su=shunting_unit_type,
        unit=train_unit_type,
        slot=request_slot_type,
        request=departure_request_type,
        l=track_part_type,
    )
    depart_bside_su_for_request.add_precondition(active_su(depart_bside_su_for_request.su))
    depart_bside_su_for_request.add_precondition(allowed_to_move_su(depart_bside_su_for_request.su))
    depart_bside_su_for_request.add_precondition(contains_su(depart_bside_su_for_request.su, depart_bside_su_for_request.unit))
    depart_bside_su_for_request.add_precondition(single_unit_su(depart_bside_su_for_request.su, depart_bside_su_for_request.unit))
    depart_bside_su_for_request.add_precondition(matched(depart_bside_su_for_request.unit, depart_bside_su_for_request.slot))
    depart_bside_su_for_request.add_precondition(slot_for_request(depart_bside_su_for_request.slot, depart_bside_su_for_request.request))
    depart_bside_su_for_request.add_precondition(at_su(depart_bside_su_for_request.su, depart_bside_su_for_request.l))
    depart_bside_su_for_request.add_precondition(departure_exit_b(depart_bside_su_for_request.l))
    pass
    depart_bside_su_for_request.add_effect(active_su(depart_bside_su_for_request.su), False)
    depart_bside_su_for_request.add_effect(at_su(depart_bside_su_for_request.su, depart_bside_su_for_request.l), False)
    depart_bside_su_for_request.add_effect(at_su(depart_bside_su_for_request.su, phantom_track), True)
    depart_bside_su_for_request.add_effect(departed_su(depart_bside_su_for_request.su), True)
    depart_bside_su_for_request.add_effect(occupied_length(depart_bside_su_for_request.l), occupied_length(depart_bside_su_for_request.l) - su_length(depart_bside_su_for_request.su))
    depart_bside_su_for_request.add_effect(request_departed(depart_bside_su_for_request.request), True)
    depart_bside_su_for_request.add_effect(num_of_departed_trains(), num_of_departed_trains() + 1)
    depart_bside_su_for_request.add_effect(number_of_trains_on_track(depart_bside_su_for_request.l), number_of_trains_on_track(depart_bside_su_for_request.l) - 1)
    pass
    pass
    depart_bside_su_for_request.add_effect(concurrent_movements, concurrent_movements - 1)
    depart_bside_su_for_request.add_effect(allowed_to_move_su(depart_bside_su_for_request.su), False)
    _v_depart_bside_su_for_request = up.Variable("v_depart_bside_su_for_request", shunting_unit_type)
    depart_bside_su_for_request.add_precondition(frontmost_b_su(depart_bside_su_for_request.su))
    depart_bside_su_for_request.add_effect(fluent=frontmost_b_su(_v_depart_bside_su_for_request), value=True, condition=behind_su(depart_bside_su_for_request.su, _v_depart_bside_su_for_request), forall=[_v_depart_bside_su_for_request])
    depart_bside_su_for_request.add_effect(fluent=behind_su(depart_bside_su_for_request.su, _v_depart_bside_su_for_request), value=False, condition=behind_su(depart_bside_su_for_request.su, _v_depart_bside_su_for_request), forall=[_v_depart_bside_su_for_request])
    depart_bside_su_for_request.add_effect(frontmost_b_su(depart_bside_su_for_request.su), False)
    problem.add_action(depart_bside_su_for_request)

    part_of_composition = problem.add_fluent(up.Fluent("part_of_composition", up.BoolType(), unit=train_unit_type, composition=arrival_composition_type), default_initial_value=False)
    composition_needs_uncoupling = problem.add_fluent(up.Fluent("composition_needs_uncoupling", up.BoolType(), composition=arrival_composition_type), default_initial_value=False)
    uncouple = up.InstantaneousAction("uncouple", unit=train_unit_type, composition=arrival_composition_type)
    uncouple.add_precondition(part_of_composition(uncouple.unit, uncouple.composition))
    uncouple.add_precondition(composition_needs_uncoupling(uncouple.composition))
    uncouple.add_effect(available(uncouple.unit), True)
    uncouple.add_effect(part_of_composition(uncouple.unit, uncouple.composition), False)
    problem.add_action(uncouple)

    uncouple_front_su = up.InstantaneousAction(
        "uncouple_front_su",
        parent_su=shunting_unit_type,
        front_su=shunting_unit_type,
        front_unit=train_unit_type,
        next_unit=train_unit_type,
        composition=arrival_composition_type,
        track=track_part_type,
    )
    uncouple_front_su.add_precondition(active_su(uncouple_front_su.parent_su))
    uncouple_front_su.add_precondition(allowed_to_move_su(uncouple_front_su.parent_su))
    uncouple_front_su.add_precondition(up.Not(active_su(uncouple_front_su.front_su)))
    uncouple_front_su.add_precondition(contains_su(uncouple_front_su.parent_su, uncouple_front_su.front_unit))
    uncouple_front_su.add_precondition(contains_su(uncouple_front_su.front_su, uncouple_front_su.front_unit))
    uncouple_front_su.add_precondition(single_unit_su(uncouple_front_su.front_su, uncouple_front_su.front_unit))
    uncouple_front_su.add_precondition(front_of(uncouple_front_su.front_unit, uncouple_front_su.parent_su))
    uncouple_front_su.add_precondition(next_in_su(uncouple_front_su.front_unit, uncouple_front_su.next_unit, uncouple_front_su.parent_su))
    uncouple_front_su.add_precondition(part_of_composition(uncouple_front_su.front_unit, uncouple_front_su.composition))
    uncouple_front_su.add_precondition(composition_needs_uncoupling(uncouple_front_su.composition))
    uncouple_front_su.add_precondition(at_su(uncouple_front_su.parent_su, uncouple_front_su.track))
    uncouple_front_su.add_effect(active_su(uncouple_front_su.front_su), True)
    uncouple_front_su.add_effect(su_may_move(uncouple_front_su.parent_su), True)
    uncouple_front_su.add_effect(su_may_move(uncouple_front_su.front_su), True)
    uncouple_front_su.add_effect(allowed_to_move_su(uncouple_front_su.parent_su), False)
    uncouple_front_su.add_effect(concurrent_movements, concurrent_movements - 1)
    uncouple_front_su.add_effect(at_su(uncouple_front_su.front_su, uncouple_front_su.track), True)
    pass
    pass
    uncouple_front_su.add_effect(su_length(uncouple_front_su.parent_su), su_length(uncouple_front_su.parent_su) - su_length(uncouple_front_su.front_su))
    uncouple_front_su.add_effect(su_unit_count(uncouple_front_su.parent_su), su_unit_count(uncouple_front_su.parent_su) - 1)
    uncouple_front_su.add_effect(su_unit_count(uncouple_front_su.front_su), 1)
    uncouple_front_su.add_effect(number_of_trains_on_track(uncouple_front_su.track), number_of_trains_on_track(uncouple_front_su.track) + 1)
    uncouple_front_su.add_effect(contains_su(uncouple_front_su.parent_su, uncouple_front_su.front_unit), False)
    uncouple_front_su.add_effect(front_of(uncouple_front_su.front_unit, uncouple_front_su.parent_su), False)
    uncouple_front_su.add_effect(front_of(uncouple_front_su.next_unit, uncouple_front_su.parent_su), True)
    uncouple_front_su.add_effect(front_of(uncouple_front_su.front_unit, uncouple_front_su.front_su), True)
    uncouple_front_su.add_effect(back_of(uncouple_front_su.front_unit, uncouple_front_su.front_su), True)
    uncouple_front_su.add_effect(next_in_su(uncouple_front_su.front_unit, uncouple_front_su.next_unit, uncouple_front_su.parent_su), False)
    uncouple_front_su.add_effect(available(uncouple_front_su.front_unit), True)
    uncouple_front_su.add_effect(part_of_composition(uncouple_front_su.front_unit, uncouple_front_su.composition), False)
    _ux_uncouple_front_su = up.Variable("ux_uncouple_front_su", shunting_unit_type)
    uncouple_front_su.add_effect(fluent=frontmost_a_su(uncouple_front_su.front_su), value=True, condition=frontmost_a_su(uncouple_front_su.parent_su))
    uncouple_front_su.add_effect(frontmost_a_su(uncouple_front_su.parent_su), False)
    uncouple_front_su.add_effect(behind_su(uncouple_front_su.parent_su, uncouple_front_su.front_su), True)
    uncouple_front_su.add_effect(fluent=behind_su(uncouple_front_su.front_su, _ux_uncouple_front_su), value=True, condition=behind_su(uncouple_front_su.parent_su, _ux_uncouple_front_su), forall=[_ux_uncouple_front_su])
    uncouple_front_su.add_effect(fluent=behind_su(uncouple_front_su.parent_su, _ux_uncouple_front_su), value=False, condition=behind_su(uncouple_front_su.parent_su, _ux_uncouple_front_su), forall=[_ux_uncouple_front_su])
    problem.add_action(uncouple_front_su)

    uncouple_back_su = up.InstantaneousAction(
        "uncouple_back_su",
        parent_su=shunting_unit_type,
        back_su=shunting_unit_type,
        previous_unit=train_unit_type,
        back_unit=train_unit_type,
        composition=arrival_composition_type,
        track=track_part_type,
    )
    uncouple_back_su.add_precondition(active_su(uncouple_back_su.parent_su))
    uncouple_back_su.add_precondition(allowed_to_move_su(uncouple_back_su.parent_su))
    uncouple_back_su.add_precondition(up.Not(active_su(uncouple_back_su.back_su)))
    uncouple_back_su.add_precondition(contains_su(uncouple_back_su.parent_su, uncouple_back_su.back_unit))
    uncouple_back_su.add_precondition(contains_su(uncouple_back_su.back_su, uncouple_back_su.back_unit))
    uncouple_back_su.add_precondition(single_unit_su(uncouple_back_su.back_su, uncouple_back_su.back_unit))
    uncouple_back_su.add_precondition(back_of(uncouple_back_su.back_unit, uncouple_back_su.parent_su))
    uncouple_back_su.add_precondition(next_in_su(uncouple_back_su.previous_unit, uncouple_back_su.back_unit, uncouple_back_su.parent_su))
    uncouple_back_su.add_precondition(part_of_composition(uncouple_back_su.back_unit, uncouple_back_su.composition))
    uncouple_back_su.add_precondition(composition_needs_uncoupling(uncouple_back_su.composition))
    uncouple_back_su.add_precondition(at_su(uncouple_back_su.parent_su, uncouple_back_su.track))
    uncouple_back_su.add_effect(active_su(uncouple_back_su.back_su), True)
    uncouple_back_su.add_effect(su_may_move(uncouple_back_su.parent_su), True)
    uncouple_back_su.add_effect(su_may_move(uncouple_back_su.back_su), True)
    uncouple_back_su.add_effect(allowed_to_move_su(uncouple_back_su.parent_su), False)
    uncouple_back_su.add_effect(concurrent_movements, concurrent_movements - 1)
    uncouple_back_su.add_effect(at_su(uncouple_back_su.back_su, uncouple_back_su.track), True)
    pass
    uncouple_back_su.add_effect(su_length(uncouple_back_su.parent_su), su_length(uncouple_back_su.parent_su) - su_length(uncouple_back_su.back_su))
    uncouple_back_su.add_effect(su_unit_count(uncouple_back_su.parent_su), su_unit_count(uncouple_back_su.parent_su) - 1)
    uncouple_back_su.add_effect(su_unit_count(uncouple_back_su.back_su), 1)
    uncouple_back_su.add_effect(number_of_trains_on_track(uncouple_back_su.track), number_of_trains_on_track(uncouple_back_su.track) + 1)
    uncouple_back_su.add_effect(contains_su(uncouple_back_su.parent_su, uncouple_back_su.back_unit), False)
    uncouple_back_su.add_effect(back_of(uncouple_back_su.back_unit, uncouple_back_su.parent_su), False)
    uncouple_back_su.add_effect(back_of(uncouple_back_su.previous_unit, uncouple_back_su.parent_su), True)
    uncouple_back_su.add_effect(front_of(uncouple_back_su.back_unit, uncouple_back_su.back_su), True)
    uncouple_back_su.add_effect(back_of(uncouple_back_su.back_unit, uncouple_back_su.back_su), True)
    uncouple_back_su.add_effect(next_in_su(uncouple_back_su.previous_unit, uncouple_back_su.back_unit, uncouple_back_su.parent_su), False)
    uncouple_back_su.add_effect(available(uncouple_back_su.back_unit), True)
    uncouple_back_su.add_effect(part_of_composition(uncouple_back_su.back_unit, uncouple_back_su.composition), False)
    _ux_uncouple_back_su = up.Variable("ux_uncouple_back_su", shunting_unit_type)
    uncouple_back_su.add_effect(fluent=frontmost_b_su(uncouple_back_su.back_su), value=True, condition=frontmost_b_su(uncouple_back_su.parent_su))
    uncouple_back_su.add_effect(frontmost_b_su(uncouple_back_su.parent_su), False)
    uncouple_back_su.add_effect(behind_su(uncouple_back_su.back_su, uncouple_back_su.parent_su), True)
    uncouple_back_su.add_effect(fluent=behind_su(_ux_uncouple_back_su, uncouple_back_su.back_su), value=True, condition=behind_su(_ux_uncouple_back_su, uncouple_back_su.parent_su), forall=[_ux_uncouple_back_su])
    uncouple_back_su.add_effect(fluent=behind_su(_ux_uncouple_back_su, uncouple_back_su.parent_su), value=False, condition=behind_su(_ux_uncouple_back_su, uncouple_back_su.parent_su), forall=[_ux_uncouple_back_su])
    problem.add_action(uncouple_back_su)

    uncouple_front_pair_su = up.InstantaneousAction(
        "uncouple_front_pair_su",
        parent_su=shunting_unit_type,
        front_su=shunting_unit_type,
        front_unit=train_unit_type,
        remaining_unit=train_unit_type,
        composition=arrival_composition_type,
        track=track_part_type,
    )
    uncouple_front_pair_su.add_precondition(active_su(uncouple_front_pair_su.parent_su))
    uncouple_front_pair_su.add_precondition(allowed_to_move_su(uncouple_front_pair_su.parent_su))
    uncouple_front_pair_su.add_precondition(up.Not(active_su(uncouple_front_pair_su.front_su)))
    uncouple_front_pair_su.add_precondition(contains_su(uncouple_front_pair_su.parent_su, uncouple_front_pair_su.front_unit))
    uncouple_front_pair_su.add_precondition(contains_su(uncouple_front_pair_su.parent_su, uncouple_front_pair_su.remaining_unit))
    uncouple_front_pair_su.add_precondition(contains_su(uncouple_front_pair_su.front_su, uncouple_front_pair_su.front_unit))
    uncouple_front_pair_su.add_precondition(single_unit_su(uncouple_front_pair_su.front_su, uncouple_front_pair_su.front_unit))
    uncouple_front_pair_su.add_precondition(front_of(uncouple_front_pair_su.front_unit, uncouple_front_pair_su.parent_su))
    uncouple_front_pair_su.add_precondition(back_of(uncouple_front_pair_su.remaining_unit, uncouple_front_pair_su.parent_su))
    uncouple_front_pair_su.add_precondition(next_in_su(uncouple_front_pair_su.front_unit, uncouple_front_pair_su.remaining_unit, uncouple_front_pair_su.parent_su))
    uncouple_front_pair_su.add_precondition(part_of_composition(uncouple_front_pair_su.front_unit, uncouple_front_pair_su.composition))
    uncouple_front_pair_su.add_precondition(part_of_composition(uncouple_front_pair_su.remaining_unit, uncouple_front_pair_su.composition))
    uncouple_front_pair_su.add_precondition(composition_needs_uncoupling(uncouple_front_pair_su.composition))
    uncouple_front_pair_su.add_precondition(at_su(uncouple_front_pair_su.parent_su, uncouple_front_pair_su.track))
    uncouple_front_pair_su.add_effect(active_su(uncouple_front_pair_su.front_su), True)
    uncouple_front_pair_su.add_effect(su_may_move(uncouple_front_pair_su.parent_su), True)
    uncouple_front_pair_su.add_effect(su_may_move(uncouple_front_pair_su.front_su), True)
    uncouple_front_pair_su.add_effect(allowed_to_move_su(uncouple_front_pair_su.parent_su), False)
    uncouple_front_pair_su.add_effect(concurrent_movements, concurrent_movements - 1)
    uncouple_front_pair_su.add_effect(at_su(uncouple_front_pair_su.front_su, uncouple_front_pair_su.track), True)
    pass
    pass
    uncouple_front_pair_su.add_effect(su_length(uncouple_front_pair_su.parent_su), su_length(uncouple_front_pair_su.parent_su) - su_length(uncouple_front_pair_su.front_su))
    uncouple_front_pair_su.add_effect(su_unit_count(uncouple_front_pair_su.parent_su), 1)
    uncouple_front_pair_su.add_effect(su_unit_count(uncouple_front_pair_su.front_su), 1)
    uncouple_front_pair_su.add_effect(number_of_trains_on_track(uncouple_front_pair_su.track), number_of_trains_on_track(uncouple_front_pair_su.track) + 1)
    uncouple_front_pair_su.add_effect(contains_su(uncouple_front_pair_su.parent_su, uncouple_front_pair_su.front_unit), False)
    uncouple_front_pair_su.add_effect(single_unit_su(uncouple_front_pair_su.parent_su, uncouple_front_pair_su.remaining_unit), True)
    uncouple_front_pair_su.add_effect(front_of(uncouple_front_pair_su.front_unit, uncouple_front_pair_su.parent_su), False)
    uncouple_front_pair_su.add_effect(front_of(uncouple_front_pair_su.remaining_unit, uncouple_front_pair_su.parent_su), True)
    uncouple_front_pair_su.add_effect(front_of(uncouple_front_pair_su.front_unit, uncouple_front_pair_su.front_su), True)
    uncouple_front_pair_su.add_effect(back_of(uncouple_front_pair_su.front_unit, uncouple_front_pair_su.front_su), True)
    uncouple_front_pair_su.add_effect(next_in_su(uncouple_front_pair_su.front_unit, uncouple_front_pair_su.remaining_unit, uncouple_front_pair_su.parent_su), False)
    uncouple_front_pair_su.add_effect(available(uncouple_front_pair_su.front_unit), True)
    uncouple_front_pair_su.add_effect(available(uncouple_front_pair_su.remaining_unit), True)
    uncouple_front_pair_su.add_effect(part_of_composition(uncouple_front_pair_su.front_unit, uncouple_front_pair_su.composition), False)
    uncouple_front_pair_su.add_effect(part_of_composition(uncouple_front_pair_su.remaining_unit, uncouple_front_pair_su.composition), False)
    _ux_uncouple_front_pair_su = up.Variable("ux_uncouple_front_pair_su", shunting_unit_type)
    uncouple_front_pair_su.add_effect(fluent=frontmost_a_su(uncouple_front_pair_su.front_su), value=True, condition=frontmost_a_su(uncouple_front_pair_su.parent_su))
    uncouple_front_pair_su.add_effect(frontmost_a_su(uncouple_front_pair_su.parent_su), False)
    uncouple_front_pair_su.add_effect(behind_su(uncouple_front_pair_su.parent_su, uncouple_front_pair_su.front_su), True)
    uncouple_front_pair_su.add_effect(fluent=behind_su(uncouple_front_pair_su.front_su, _ux_uncouple_front_pair_su), value=True, condition=behind_su(uncouple_front_pair_su.parent_su, _ux_uncouple_front_pair_su), forall=[_ux_uncouple_front_pair_su])
    uncouple_front_pair_su.add_effect(fluent=behind_su(uncouple_front_pair_su.parent_su, _ux_uncouple_front_pair_su), value=False, condition=behind_su(uncouple_front_pair_su.parent_su, _ux_uncouple_front_pair_su), forall=[_ux_uncouple_front_pair_su])
    problem.add_action(uncouple_front_pair_su)

    uncouple_back_pair_su = up.InstantaneousAction(
        "uncouple_back_pair_su",
        parent_su=shunting_unit_type,
        back_su=shunting_unit_type,
        remaining_unit=train_unit_type,
        back_unit=train_unit_type,
        composition=arrival_composition_type,
        track=track_part_type,
    )
    uncouple_back_pair_su.add_precondition(active_su(uncouple_back_pair_su.parent_su))
    uncouple_back_pair_su.add_precondition(allowed_to_move_su(uncouple_back_pair_su.parent_su))
    uncouple_back_pair_su.add_precondition(up.Not(active_su(uncouple_back_pair_su.back_su)))
    uncouple_back_pair_su.add_precondition(contains_su(uncouple_back_pair_su.parent_su, uncouple_back_pair_su.remaining_unit))
    uncouple_back_pair_su.add_precondition(contains_su(uncouple_back_pair_su.parent_su, uncouple_back_pair_su.back_unit))
    uncouple_back_pair_su.add_precondition(contains_su(uncouple_back_pair_su.back_su, uncouple_back_pair_su.back_unit))
    uncouple_back_pair_su.add_precondition(single_unit_su(uncouple_back_pair_su.back_su, uncouple_back_pair_su.back_unit))
    uncouple_back_pair_su.add_precondition(front_of(uncouple_back_pair_su.remaining_unit, uncouple_back_pair_su.parent_su))
    uncouple_back_pair_su.add_precondition(back_of(uncouple_back_pair_su.back_unit, uncouple_back_pair_su.parent_su))
    uncouple_back_pair_su.add_precondition(next_in_su(uncouple_back_pair_su.remaining_unit, uncouple_back_pair_su.back_unit, uncouple_back_pair_su.parent_su))
    uncouple_back_pair_su.add_precondition(part_of_composition(uncouple_back_pair_su.remaining_unit, uncouple_back_pair_su.composition))
    uncouple_back_pair_su.add_precondition(part_of_composition(uncouple_back_pair_su.back_unit, uncouple_back_pair_su.composition))
    uncouple_back_pair_su.add_precondition(composition_needs_uncoupling(uncouple_back_pair_su.composition))
    uncouple_back_pair_su.add_precondition(at_su(uncouple_back_pair_su.parent_su, uncouple_back_pair_su.track))
    uncouple_back_pair_su.add_effect(active_su(uncouple_back_pair_su.back_su), True)
    uncouple_back_pair_su.add_effect(su_may_move(uncouple_back_pair_su.parent_su), True)
    uncouple_back_pair_su.add_effect(su_may_move(uncouple_back_pair_su.back_su), True)
    uncouple_back_pair_su.add_effect(allowed_to_move_su(uncouple_back_pair_su.parent_su), False)
    uncouple_back_pair_su.add_effect(concurrent_movements, concurrent_movements - 1)
    uncouple_back_pair_su.add_effect(at_su(uncouple_back_pair_su.back_su, uncouple_back_pair_su.track), True)
    pass
    uncouple_back_pair_su.add_effect(su_length(uncouple_back_pair_su.parent_su), su_length(uncouple_back_pair_su.parent_su) - su_length(uncouple_back_pair_su.back_su))
    uncouple_back_pair_su.add_effect(su_unit_count(uncouple_back_pair_su.parent_su), 1)
    uncouple_back_pair_su.add_effect(su_unit_count(uncouple_back_pair_su.back_su), 1)
    uncouple_back_pair_su.add_effect(number_of_trains_on_track(uncouple_back_pair_su.track), number_of_trains_on_track(uncouple_back_pair_su.track) + 1)
    uncouple_back_pair_su.add_effect(contains_su(uncouple_back_pair_su.parent_su, uncouple_back_pair_su.back_unit), False)
    uncouple_back_pair_su.add_effect(single_unit_su(uncouple_back_pair_su.parent_su, uncouple_back_pair_su.remaining_unit), True)
    uncouple_back_pair_su.add_effect(back_of(uncouple_back_pair_su.back_unit, uncouple_back_pair_su.parent_su), False)
    uncouple_back_pair_su.add_effect(back_of(uncouple_back_pair_su.remaining_unit, uncouple_back_pair_su.parent_su), True)
    uncouple_back_pair_su.add_effect(front_of(uncouple_back_pair_su.back_unit, uncouple_back_pair_su.back_su), True)
    uncouple_back_pair_su.add_effect(back_of(uncouple_back_pair_su.back_unit, uncouple_back_pair_su.back_su), True)
    uncouple_back_pair_su.add_effect(next_in_su(uncouple_back_pair_su.remaining_unit, uncouple_back_pair_su.back_unit, uncouple_back_pair_su.parent_su), False)
    uncouple_back_pair_su.add_effect(available(uncouple_back_pair_su.remaining_unit), True)
    uncouple_back_pair_su.add_effect(available(uncouple_back_pair_su.back_unit), True)
    uncouple_back_pair_su.add_effect(part_of_composition(uncouple_back_pair_su.remaining_unit, uncouple_back_pair_su.composition), False)
    uncouple_back_pair_su.add_effect(part_of_composition(uncouple_back_pair_su.back_unit, uncouple_back_pair_su.composition), False)
    _ux_uncouple_back_pair_su = up.Variable("ux_uncouple_back_pair_su", shunting_unit_type)
    uncouple_back_pair_su.add_effect(fluent=frontmost_b_su(uncouple_back_pair_su.back_su), value=True, condition=frontmost_b_su(uncouple_back_pair_su.parent_su))
    uncouple_back_pair_su.add_effect(frontmost_b_su(uncouple_back_pair_su.parent_su), False)
    uncouple_back_pair_su.add_effect(behind_su(uncouple_back_pair_su.back_su, uncouple_back_pair_su.parent_su), True)
    uncouple_back_pair_su.add_effect(fluent=behind_su(_ux_uncouple_back_pair_su, uncouple_back_pair_su.back_su), value=True, condition=behind_su(_ux_uncouple_back_pair_su, uncouple_back_pair_su.parent_su), forall=[_ux_uncouple_back_pair_su])
    uncouple_back_pair_su.add_effect(fluent=behind_su(_ux_uncouple_back_pair_su, uncouple_back_pair_su.parent_su), value=False, condition=behind_su(_ux_uncouple_back_pair_su, uncouple_back_pair_su.parent_su), forall=[_ux_uncouple_back_pair_su])
    problem.add_action(uncouple_back_pair_su)

    split_two_unit_su = up.InstantaneousAction(
        "split_two_unit_su",
        parent_su=shunting_unit_type,
        left_su=shunting_unit_type,
        right_su=shunting_unit_type,
        unit_a=train_unit_type,
        unit_b=train_unit_type,
        composition=arrival_composition_type,
        track=track_part_type,
    )
    split_two_unit_su.add_precondition(active_su(split_two_unit_su.parent_su))
    split_two_unit_su.add_precondition(allowed_to_move_su(split_two_unit_su.parent_su))
    split_two_unit_su.add_precondition(up.Not(active_su(split_two_unit_su.left_su)))
    split_two_unit_su.add_precondition(up.Not(active_su(split_two_unit_su.right_su)))
    split_two_unit_su.add_precondition(contains_su(split_two_unit_su.parent_su, split_two_unit_su.unit_a))
    split_two_unit_su.add_precondition(contains_su(split_two_unit_su.parent_su, split_two_unit_su.unit_b))
    split_two_unit_su.add_precondition(contains_su(split_two_unit_su.left_su, split_two_unit_su.unit_a))
    split_two_unit_su.add_precondition(contains_su(split_two_unit_su.right_su, split_two_unit_su.unit_b))
    split_two_unit_su.add_precondition(single_unit_su(split_two_unit_su.left_su, split_two_unit_su.unit_a))
    split_two_unit_su.add_precondition(single_unit_su(split_two_unit_su.right_su, split_two_unit_su.unit_b))
    split_two_unit_su.add_precondition(part_of_composition(split_two_unit_su.unit_a, split_two_unit_su.composition))
    split_two_unit_su.add_precondition(part_of_composition(split_two_unit_su.unit_b, split_two_unit_su.composition))
    split_two_unit_su.add_precondition(composition_needs_uncoupling(split_two_unit_su.composition))
    split_two_unit_su.add_precondition(unit_before(split_two_unit_su.unit_a, split_two_unit_su.unit_b))
    split_two_unit_su.add_precondition(at_su(split_two_unit_su.parent_su, split_two_unit_su.track))
    split_two_unit_su.add_effect(active_su(split_two_unit_su.parent_su), False)
    split_two_unit_su.add_effect(allowed_to_move_su(split_two_unit_su.parent_su), False)
    split_two_unit_su.add_effect(concurrent_movements, concurrent_movements - 1)
    split_two_unit_su.add_effect(active_su(split_two_unit_su.left_su), True)
    split_two_unit_su.add_effect(active_su(split_two_unit_su.right_su), True)
    split_two_unit_su.add_effect(su_may_move(split_two_unit_su.left_su), True)
    split_two_unit_su.add_effect(su_may_move(split_two_unit_su.right_su), True)
    split_two_unit_su.add_effect(at_su(split_two_unit_su.parent_su, split_two_unit_su.track), False)
    split_two_unit_su.add_effect(at_su(split_two_unit_su.left_su, split_two_unit_su.track), True)
    split_two_unit_su.add_effect(at_su(split_two_unit_su.right_su, split_two_unit_su.track), True)
    pass
    pass
    split_two_unit_su.add_effect(number_of_trains_on_track(split_two_unit_su.track), number_of_trains_on_track(split_two_unit_su.track) + 1)
    split_two_unit_su.add_effect(available(split_two_unit_su.unit_a), True)
    split_two_unit_su.add_effect(available(split_two_unit_su.unit_b), True)
    split_two_unit_su.add_effect(part_of_composition(split_two_unit_su.unit_a, split_two_unit_su.composition), False)
    split_two_unit_su.add_effect(part_of_composition(split_two_unit_su.unit_b, split_two_unit_su.composition), False)
    # Fixed-size split actions are kept for reference but not registered; the
    # front/back uncoupling actions above are the active composition model.

    split_three_unit_su = up.InstantaneousAction(
        "split_three_unit_su",
        parent_su=shunting_unit_type,
        first_su=shunting_unit_type,
        second_su=shunting_unit_type,
        third_su=shunting_unit_type,
        unit_a=train_unit_type,
        unit_b=train_unit_type,
        unit_c=train_unit_type,
        composition=arrival_composition_type,
        track=track_part_type,
    )
    split_three_unit_su.add_precondition(active_su(split_three_unit_su.parent_su))
    split_three_unit_su.add_precondition(allowed_to_move_su(split_three_unit_su.parent_su))
    split_three_unit_su.add_precondition(up.Not(active_su(split_three_unit_su.first_su)))
    split_three_unit_su.add_precondition(up.Not(active_su(split_three_unit_su.second_su)))
    split_three_unit_su.add_precondition(up.Not(active_su(split_three_unit_su.third_su)))
    split_three_unit_su.add_precondition(contains_su(split_three_unit_su.parent_su, split_three_unit_su.unit_a))
    split_three_unit_su.add_precondition(contains_su(split_three_unit_su.parent_su, split_three_unit_su.unit_b))
    split_three_unit_su.add_precondition(contains_su(split_three_unit_su.parent_su, split_three_unit_su.unit_c))
    split_three_unit_su.add_precondition(contains_su(split_three_unit_su.first_su, split_three_unit_su.unit_a))
    split_three_unit_su.add_precondition(contains_su(split_three_unit_su.second_su, split_three_unit_su.unit_b))
    split_three_unit_su.add_precondition(contains_su(split_three_unit_su.third_su, split_three_unit_su.unit_c))
    split_three_unit_su.add_precondition(single_unit_su(split_three_unit_su.first_su, split_three_unit_su.unit_a))
    split_three_unit_su.add_precondition(single_unit_su(split_three_unit_su.second_su, split_three_unit_su.unit_b))
    split_three_unit_su.add_precondition(single_unit_su(split_three_unit_su.third_su, split_three_unit_su.unit_c))
    split_three_unit_su.add_precondition(part_of_composition(split_three_unit_su.unit_a, split_three_unit_su.composition))
    split_three_unit_su.add_precondition(part_of_composition(split_three_unit_su.unit_b, split_three_unit_su.composition))
    split_three_unit_su.add_precondition(part_of_composition(split_three_unit_su.unit_c, split_three_unit_su.composition))
    split_three_unit_su.add_precondition(composition_needs_uncoupling(split_three_unit_su.composition))
    split_three_unit_su.add_precondition(unit_before(split_three_unit_su.unit_a, split_three_unit_su.unit_b))
    split_three_unit_su.add_precondition(unit_before(split_three_unit_su.unit_b, split_three_unit_su.unit_c))
    split_three_unit_su.add_precondition(at_su(split_three_unit_su.parent_su, split_three_unit_su.track))
    split_three_unit_su.add_effect(active_su(split_three_unit_su.parent_su), False)
    split_three_unit_su.add_effect(allowed_to_move_su(split_three_unit_su.parent_su), False)
    split_three_unit_su.add_effect(concurrent_movements, concurrent_movements - 1)
    split_three_unit_su.add_effect(active_su(split_three_unit_su.first_su), True)
    split_three_unit_su.add_effect(active_su(split_three_unit_su.second_su), True)
    split_three_unit_su.add_effect(active_su(split_three_unit_su.third_su), True)
    split_three_unit_su.add_effect(su_may_move(split_three_unit_su.first_su), True)
    split_three_unit_su.add_effect(su_may_move(split_three_unit_su.second_su), True)
    split_three_unit_su.add_effect(su_may_move(split_three_unit_su.third_su), True)
    split_three_unit_su.add_effect(at_su(split_three_unit_su.parent_su, split_three_unit_su.track), False)
    split_three_unit_su.add_effect(at_su(split_three_unit_su.first_su, split_three_unit_su.track), True)
    split_three_unit_su.add_effect(at_su(split_three_unit_su.second_su, split_three_unit_su.track), True)
    split_three_unit_su.add_effect(at_su(split_three_unit_su.third_su, split_three_unit_su.track), True)
    pass
    pass
    pass
    split_three_unit_su.add_effect(number_of_trains_on_track(split_three_unit_su.track), number_of_trains_on_track(split_three_unit_su.track) + 2)
    split_three_unit_su.add_effect(available(split_three_unit_su.unit_a), True)
    split_three_unit_su.add_effect(available(split_three_unit_su.unit_b), True)
    split_three_unit_su.add_effect(available(split_three_unit_su.unit_c), True)
    split_three_unit_su.add_effect(part_of_composition(split_three_unit_su.unit_a, split_three_unit_su.composition), False)
    split_three_unit_su.add_effect(part_of_composition(split_three_unit_su.unit_b, split_three_unit_su.composition), False)
    split_three_unit_su.add_effect(part_of_composition(split_three_unit_su.unit_c, split_three_unit_su.composition), False)
    # See note above: use front/back uncoupling instead of size-specific splits.

    slot_coupled = problem.add_fluent(up.Fluent("slot_coupled", up.BoolType(), slot=request_slot_type), default_initial_value=False)
    coupled_to_request = problem.add_fluent(up.Fluent("coupled_to_request", up.BoolType(), unit=train_unit_type, request=departure_request_type), default_initial_value=False)
    physically_coupled = problem.add_fluent(up.Fluent("physically_coupled", up.BoolType(), first=train_unit_type, second=train_unit_type), default_initial_value=False)
    request_assembled = problem.add_fluent(up.Fluent("request_assembled", up.BoolType(), request=departure_request_type), default_initial_value=False)

    start_request_composition = up.InstantaneousAction(
        "start_request_composition",
        source_su=shunting_unit_type,
        request_su=shunting_unit_type,
        unit=train_unit_type,
        slot=request_slot_type,
        request=departure_request_type,
        track=track_part_type,
    )
    start_request_composition.add_precondition(active_su(start_request_composition.source_su))
    start_request_composition.add_precondition(up.Not(parked_su(start_request_composition.source_su)))
    start_request_composition.add_precondition(up.Not(active_su(start_request_composition.request_su)))
    start_request_composition.add_precondition(contains_su(start_request_composition.source_su, start_request_composition.unit))
    start_request_composition.add_precondition(single_unit_su(start_request_composition.source_su, start_request_composition.unit))
    start_request_composition.add_precondition(request_su_for_request(start_request_composition.request_su, start_request_composition.request))
    start_request_composition.add_precondition(at_su(start_request_composition.source_su, start_request_composition.track))
    start_request_composition.add_precondition(coupling_allowed(start_request_composition.track))
    start_request_composition.add_precondition(coupling_track_for_request(start_request_composition.request, start_request_composition.track))
    start_request_composition.add_precondition(matched(start_request_composition.unit, start_request_composition.slot))
    start_request_composition.add_precondition(slot_for_request(start_request_composition.slot, start_request_composition.request))
    start_request_composition.add_effect(active_su(start_request_composition.source_su), False)
    start_request_composition.add_effect(active_su(start_request_composition.request_su), True)
    # start_request_composition.add_effect(su_may_move(start_request_composition.request_su), True)
    start_request_composition.add_effect(at_su(start_request_composition.source_su, start_request_composition.track), False)
    start_request_composition.add_effect(at_su(start_request_composition.request_su, start_request_composition.track), True)
    pass
    start_request_composition.add_effect(su_length(start_request_composition.request_su), su_length(start_request_composition.source_su))
    start_request_composition.add_effect(su_unit_count(start_request_composition.request_su), 1)
    start_request_composition.add_effect(contains_su(start_request_composition.request_su, start_request_composition.unit), True)
    start_request_composition.add_effect(front_of(start_request_composition.unit, start_request_composition.request_su), True)
    start_request_composition.add_effect(back_of(start_request_composition.unit, start_request_composition.request_su), True)
    start_request_composition.add_effect(front_slot(start_request_composition.slot, start_request_composition.request_su), True)
    start_request_composition.add_effect(back_slot(start_request_composition.slot, start_request_composition.request_su), True)
    start_request_composition.add_effect(slot_coupled(start_request_composition.slot), True)
    start_request_composition.add_effect(coupled_to_request(start_request_composition.unit, start_request_composition.request), True)
    _srcn = up.Variable("srcn", shunting_unit_type)
    start_request_composition.add_effect(fluent=frontmost_a_su(start_request_composition.request_su), value=True, condition=frontmost_a_su(start_request_composition.source_su))
    start_request_composition.add_effect(fluent=frontmost_b_su(start_request_composition.request_su), value=True, condition=frontmost_b_su(start_request_composition.source_su))
    start_request_composition.add_effect(frontmost_a_su(start_request_composition.source_su), False)
    start_request_composition.add_effect(frontmost_b_su(start_request_composition.source_su), False)
    start_request_composition.add_effect(fluent=behind_su(_srcn, start_request_composition.request_su), value=True, condition=behind_su(_srcn, start_request_composition.source_su), forall=[_srcn])
    start_request_composition.add_effect(fluent=behind_su(_srcn, start_request_composition.source_su), value=False, condition=behind_su(_srcn, start_request_composition.source_su), forall=[_srcn])
    start_request_composition.add_effect(fluent=behind_su(start_request_composition.request_su, _srcn), value=True, condition=behind_su(start_request_composition.source_su, _srcn), forall=[_srcn])
    start_request_composition.add_effect(fluent=behind_su(start_request_composition.source_su, _srcn), value=False, condition=behind_su(start_request_composition.source_su, _srcn), forall=[_srcn])
    problem.add_action(start_request_composition)

    couple_front_to_request = up.InstantaneousAction(
        "couple_front_to_request",
        source_su=shunting_unit_type,
        request_su=shunting_unit_type,
        unit=train_unit_type,
        old_front_unit=train_unit_type,
        slot=request_slot_type,
        old_front_slot=request_slot_type,
        request=departure_request_type,
        track=track_part_type,
    )
    couple_front_to_request.add_precondition(active_su(couple_front_to_request.source_su))
    couple_front_to_request.add_precondition(active_su(couple_front_to_request.request_su))
    couple_front_to_request.add_precondition(up.Not(parked_su(couple_front_to_request.source_su)))
    couple_front_to_request.add_precondition(up.Not(parked_su(couple_front_to_request.request_su)))
    couple_front_to_request.add_precondition(contains_su(couple_front_to_request.source_su, couple_front_to_request.unit))
    couple_front_to_request.add_precondition(single_unit_su(couple_front_to_request.source_su, couple_front_to_request.unit))
    couple_front_to_request.add_precondition(request_su_for_request(couple_front_to_request.request_su, couple_front_to_request.request))
    couple_front_to_request.add_precondition(at_su(couple_front_to_request.source_su, couple_front_to_request.track))
    couple_front_to_request.add_precondition(at_su(couple_front_to_request.request_su, couple_front_to_request.track))
    couple_front_to_request.add_precondition(coupling_allowed(couple_front_to_request.track))
    couple_front_to_request.add_precondition(coupling_track_for_request(couple_front_to_request.request, couple_front_to_request.track))
    couple_front_to_request.add_precondition(matched(couple_front_to_request.unit, couple_front_to_request.slot))
    couple_front_to_request.add_precondition(slot_for_request(couple_front_to_request.slot, couple_front_to_request.request))
    couple_front_to_request.add_precondition(front_of(couple_front_to_request.old_front_unit, couple_front_to_request.request_su))
    couple_front_to_request.add_precondition(front_slot(couple_front_to_request.old_front_slot, couple_front_to_request.request_su))
    couple_front_to_request.add_precondition(slot_before(couple_front_to_request.slot, couple_front_to_request.old_front_slot))
    couple_front_to_request.add_effect(active_su(couple_front_to_request.source_su), False)
    couple_front_to_request.add_effect(at_su(couple_front_to_request.source_su, couple_front_to_request.track), False)
    couple_front_to_request.add_effect(su_length(couple_front_to_request.request_su), su_length(couple_front_to_request.request_su) + su_length(couple_front_to_request.source_su))
    couple_front_to_request.add_effect(su_unit_count(couple_front_to_request.request_su), su_unit_count(couple_front_to_request.request_su) + 1)
    couple_front_to_request.add_effect(number_of_trains_on_track(couple_front_to_request.track), number_of_trains_on_track(couple_front_to_request.track) - 1)
    couple_front_to_request.add_effect(contains_su(couple_front_to_request.request_su, couple_front_to_request.unit), True)
    couple_front_to_request.add_effect(front_of(couple_front_to_request.old_front_unit, couple_front_to_request.request_su), False)
    couple_front_to_request.add_effect(front_of(couple_front_to_request.unit, couple_front_to_request.request_su), True)
    couple_front_to_request.add_effect(next_in_su(couple_front_to_request.unit, couple_front_to_request.old_front_unit, couple_front_to_request.request_su), True)
    couple_front_to_request.add_effect(front_slot(couple_front_to_request.old_front_slot, couple_front_to_request.request_su), False)
    couple_front_to_request.add_effect(front_slot(couple_front_to_request.slot, couple_front_to_request.request_su), True)
    couple_front_to_request.add_effect(slot_coupled(couple_front_to_request.slot), True)
    couple_front_to_request.add_effect(coupled_to_request(couple_front_to_request.unit, couple_front_to_request.request), True)
    couple_front_to_request.add_effect(physically_coupled(couple_front_to_request.unit, couple_front_to_request.old_front_unit), True)
    _cf = up.Variable("cf_cfr", shunting_unit_type)
    couple_front_to_request.add_precondition(behind_su(couple_front_to_request.request_su, couple_front_to_request.source_su))
    couple_front_to_request.add_effect(behind_su(couple_front_to_request.request_su, couple_front_to_request.source_su), False)
    couple_front_to_request.add_effect(fluent=behind_su(couple_front_to_request.request_su, _cf), value=True, condition=behind_su(couple_front_to_request.source_su, _cf), forall=[_cf])
    couple_front_to_request.add_effect(fluent=behind_su(couple_front_to_request.source_su, _cf), value=False, condition=behind_su(couple_front_to_request.source_su, _cf), forall=[_cf])
    couple_front_to_request.add_effect(fluent=frontmost_a_su(couple_front_to_request.request_su), value=True, condition=frontmost_a_su(couple_front_to_request.source_su))
    couple_front_to_request.add_effect(frontmost_a_su(couple_front_to_request.source_su), False)
    couple_front_to_request.add_effect(frontmost_b_su(couple_front_to_request.source_su), False)
    problem.add_action(couple_front_to_request)

    couple_back_to_request = up.InstantaneousAction(
        "couple_back_to_request",
        request_su=shunting_unit_type,
        source_su=shunting_unit_type,
        old_back_unit=train_unit_type,
        unit=train_unit_type,
        old_back_slot=request_slot_type,
        slot=request_slot_type,
        request=departure_request_type,
        track=track_part_type,
    )
    couple_back_to_request.add_precondition(active_su(couple_back_to_request.request_su))
    couple_back_to_request.add_precondition(active_su(couple_back_to_request.source_su))
    couple_back_to_request.add_precondition(up.Not(parked_su(couple_back_to_request.request_su)))
    couple_back_to_request.add_precondition(up.Not(parked_su(couple_back_to_request.source_su)))
    couple_back_to_request.add_precondition(contains_su(couple_back_to_request.source_su, couple_back_to_request.unit))
    couple_back_to_request.add_precondition(single_unit_su(couple_back_to_request.source_su, couple_back_to_request.unit))
    couple_back_to_request.add_precondition(request_su_for_request(couple_back_to_request.request_su, couple_back_to_request.request))
    couple_back_to_request.add_precondition(at_su(couple_back_to_request.request_su, couple_back_to_request.track))
    couple_back_to_request.add_precondition(at_su(couple_back_to_request.source_su, couple_back_to_request.track))
    couple_back_to_request.add_precondition(coupling_allowed(couple_back_to_request.track))
    couple_back_to_request.add_precondition(coupling_track_for_request(couple_back_to_request.request, couple_back_to_request.track))
    couple_back_to_request.add_precondition(matched(couple_back_to_request.unit, couple_back_to_request.slot))
    couple_back_to_request.add_precondition(slot_for_request(couple_back_to_request.slot, couple_back_to_request.request))
    couple_back_to_request.add_precondition(back_of(couple_back_to_request.old_back_unit, couple_back_to_request.request_su))
    couple_back_to_request.add_precondition(back_slot(couple_back_to_request.old_back_slot, couple_back_to_request.request_su))
    couple_back_to_request.add_precondition(slot_before(couple_back_to_request.old_back_slot, couple_back_to_request.slot))
    couple_back_to_request.add_effect(active_su(couple_back_to_request.source_su), False)
    couple_back_to_request.add_effect(at_su(couple_back_to_request.source_su, couple_back_to_request.track), False)
    couple_back_to_request.add_effect(su_length(couple_back_to_request.request_su), su_length(couple_back_to_request.request_su) + su_length(couple_back_to_request.source_su))
    couple_back_to_request.add_effect(su_unit_count(couple_back_to_request.request_su), su_unit_count(couple_back_to_request.request_su) + 1)
    couple_back_to_request.add_effect(number_of_trains_on_track(couple_back_to_request.track), number_of_trains_on_track(couple_back_to_request.track) - 1)
    couple_back_to_request.add_effect(contains_su(couple_back_to_request.request_su, couple_back_to_request.unit), True)
    couple_back_to_request.add_effect(back_of(couple_back_to_request.old_back_unit, couple_back_to_request.request_su), False)
    couple_back_to_request.add_effect(back_of(couple_back_to_request.unit, couple_back_to_request.request_su), True)
    couple_back_to_request.add_effect(next_in_su(couple_back_to_request.old_back_unit, couple_back_to_request.unit, couple_back_to_request.request_su), True)
    couple_back_to_request.add_effect(back_slot(couple_back_to_request.old_back_slot, couple_back_to_request.request_su), False)
    couple_back_to_request.add_effect(back_slot(couple_back_to_request.slot, couple_back_to_request.request_su), True)
    couple_back_to_request.add_effect(slot_coupled(couple_back_to_request.slot), True)
    couple_back_to_request.add_effect(coupled_to_request(couple_back_to_request.unit, couple_back_to_request.request), True)
    couple_back_to_request.add_effect(physically_coupled(couple_back_to_request.old_back_unit, couple_back_to_request.unit), True)
    _cb = up.Variable("cb_cbr", shunting_unit_type)
    couple_back_to_request.add_precondition(behind_su(couple_back_to_request.source_su, couple_back_to_request.request_su))
    couple_back_to_request.add_effect(behind_su(couple_back_to_request.source_su, couple_back_to_request.request_su), False)
    couple_back_to_request.add_effect(fluent=behind_su(_cb, couple_back_to_request.request_su), value=True, condition=behind_su(_cb, couple_back_to_request.source_su), forall=[_cb])
    couple_back_to_request.add_effect(fluent=behind_su(_cb, couple_back_to_request.source_su), value=False, condition=behind_su(_cb, couple_back_to_request.source_su), forall=[_cb])
    couple_back_to_request.add_effect(fluent=frontmost_b_su(couple_back_to_request.request_su), value=True, condition=frontmost_b_su(couple_back_to_request.source_su))
    couple_back_to_request.add_effect(frontmost_a_su(couple_back_to_request.source_su), False)
    couple_back_to_request.add_effect(frontmost_b_su(couple_back_to_request.source_su), False)
    problem.add_action(couple_back_to_request)

    complete_request_composition = up.InstantaneousAction(
        "complete_request_composition",
        request_su=shunting_unit_type,
        request=departure_request_type,
    )
    complete_request_composition.add_precondition(active_su(complete_request_composition.request_su))
    complete_request_composition.add_precondition(up.Not(request_assembled(complete_request_composition.request)))
    complete_request_composition.add_precondition(request_su_for_request(complete_request_composition.request_su, complete_request_composition.request))
    complete_request_composition.add_precondition(up.Equals(su_unit_count(complete_request_composition.request_su), request_size(complete_request_composition.request)))
    complete_request_composition.add_effect(request_assembled(complete_request_composition.request), True)
    complete_request_composition.add_effect(su_may_move(complete_request_composition.request_su), True)
    complete_request_composition.add_effect(must_depart_su(complete_request_composition.request_su), True)
    problem.add_action(complete_request_composition)

    couple_two_sus = up.InstantaneousAction(
        "couple_two_sus",
        su_a=shunting_unit_type,
        su_b=shunting_unit_type,
        su_result=shunting_unit_type,
        unit_a=train_unit_type,
        unit_b=train_unit_type,
        track=track_part_type,
        slot_a=request_slot_type,
        slot_b=request_slot_type,
        request=departure_request_type,
    )
    couple_two_sus.add_precondition(active_su(couple_two_sus.su_a))
    couple_two_sus.add_precondition(active_su(couple_two_sus.su_b))
    couple_two_sus.add_precondition(up.Not(parked_su(couple_two_sus.su_a)))
    couple_two_sus.add_precondition(up.Not(parked_su(couple_two_sus.su_b)))
    couple_two_sus.add_precondition(up.Not(active_su(couple_two_sus.su_result)))
    couple_two_sus.add_precondition(up.Not(up.Equals(couple_two_sus.su_a, couple_two_sus.su_b)))
    couple_two_sus.add_precondition(contains_su(couple_two_sus.su_a, couple_two_sus.unit_a))
    couple_two_sus.add_precondition(contains_su(couple_two_sus.su_b, couple_two_sus.unit_b))
    couple_two_sus.add_precondition(single_unit_su(couple_two_sus.su_a, couple_two_sus.unit_a))
    couple_two_sus.add_precondition(single_unit_su(couple_two_sus.su_b, couple_two_sus.unit_b))
    couple_two_sus.add_precondition(request_su_for_request(couple_two_sus.su_result, couple_two_sus.request))
    couple_two_sus.add_precondition(at_su(couple_two_sus.su_a, couple_two_sus.track))
    couple_two_sus.add_precondition(at_su(couple_two_sus.su_b, couple_two_sus.track))
    couple_two_sus.add_precondition(coupling_allowed(couple_two_sus.track))
    couple_two_sus.add_precondition(coupling_track_for_request(couple_two_sus.request, couple_two_sus.track))
    couple_two_sus.add_precondition(matched(couple_two_sus.unit_a, couple_two_sus.slot_a))
    couple_two_sus.add_precondition(matched(couple_two_sus.unit_b, couple_two_sus.slot_b))
    couple_two_sus.add_precondition(slot_for_request(couple_two_sus.slot_a, couple_two_sus.request))
    couple_two_sus.add_precondition(slot_for_request(couple_two_sus.slot_b, couple_two_sus.request))
    couple_two_sus.add_precondition(slot_before(couple_two_sus.slot_a, couple_two_sus.slot_b))
    couple_two_sus.add_effect(active_su(couple_two_sus.su_a), False)
    couple_two_sus.add_effect(active_su(couple_two_sus.su_b), False)
    couple_two_sus.add_effect(active_su(couple_two_sus.su_result), True)
    couple_two_sus.add_effect(su_may_move(couple_two_sus.su_result), True)
    couple_two_sus.add_effect(must_depart_su(couple_two_sus.su_result), True)
    couple_two_sus.add_effect(at_su(couple_two_sus.su_a, couple_two_sus.track), False)
    couple_two_sus.add_effect(at_su(couple_two_sus.su_b, couple_two_sus.track), False)
    couple_two_sus.add_effect(at_su(couple_two_sus.su_result, couple_two_sus.track), True)
    pass
    couple_two_sus.add_effect(su_length(couple_two_sus.su_result), su_length(couple_two_sus.su_a) + su_length(couple_two_sus.su_b))
    couple_two_sus.add_effect(number_of_trains_on_track(couple_two_sus.track), number_of_trains_on_track(couple_two_sus.track) - 1)
    couple_two_sus.add_effect(contains_su(couple_two_sus.su_result, couple_two_sus.unit_a), True)
    couple_two_sus.add_effect(contains_su(couple_two_sus.su_result, couple_two_sus.unit_b), True)
    couple_two_sus.add_effect(slot_coupled(couple_two_sus.slot_a), True)
    couple_two_sus.add_effect(slot_coupled(couple_two_sus.slot_b), True)
    couple_two_sus.add_effect(coupled_to_request(couple_two_sus.unit_a, couple_two_sus.request), True)
    couple_two_sus.add_effect(coupled_to_request(couple_two_sus.unit_b, couple_two_sus.request), True)
    couple_two_sus.add_effect(physically_coupled(couple_two_sus.unit_a, couple_two_sus.unit_b), True)
    couple_two_sus.add_effect(request_assembled(couple_two_sus.request), True)
    # Fixed two-unit coupling is kept for comparison but not registered; request
    # compositions are built incrementally with start/couple-front/couple-back.

    service_su = up.InstantaneousAction('service_su', su=shunting_unit_type, l=track_part_type, f=facility_type_type)
    service_su.add_precondition(active_su(service_su.su))
    service_su.add_precondition(at_su(service_su.su, service_su.l))
    service_su.add_precondition(service_allowed(service_su.l))
    service_su.add_precondition(facility_type(service_su.l, service_su.f))
    service_su.add_precondition(requires_facility(service_su.su, service_su.f))
    service_su.add_effect(serviced(service_su.su), True)
    problem.add_action(service_su)

    couple_two_sus.add_precondition(serviced(couple_two_sus.su_a))
    couple_two_sus.add_precondition(serviced(couple_two_sus.su_b))
    start_request_composition.add_precondition(serviced(start_request_composition.source_su))
    couple_front_to_request.add_precondition(serviced(couple_front_to_request.source_su))
    couple_front_to_request.add_precondition(serviced(couple_front_to_request.request_su))
    couple_back_to_request.add_precondition(serviced(couple_back_to_request.source_su))
    couple_back_to_request.add_precondition(serviced(couple_back_to_request.request_su))
    split_two_unit_su.add_precondition(serviced(split_two_unit_su.parent_su))
    split_three_unit_su.add_precondition(serviced(split_three_unit_su.parent_su))
    uncouple_front_su.add_precondition(serviced(uncouple_front_su.parent_su))
    uncouple_back_su.add_precondition(serviced(uncouple_back_su.parent_su))
    uncouple_front_pair_su.add_precondition(serviced(uncouple_front_pair_su.parent_su))
    uncouple_back_pair_su.add_precondition(serviced(uncouple_back_pair_su.parent_su))
    depart_aside_su.add_precondition(serviced(depart_aside_su.su))
    depart_bside_su.add_precondition(serviced(depart_bside_su.su))
    depart_aside_su_for_request.add_precondition(serviced(depart_aside_su_for_request.su))
    depart_bside_su_for_request.add_precondition(serviced(depart_bside_su_for_request.su))
    park_su.add_precondition(serviced(park_su.su))

    match = up.InstantaneousAction("match", unit=train_unit_type, slot=request_slot_type)
    match.add_precondition(available(match.unit))
    match.add_precondition(slot_open(match.slot))
    match.add_precondition(compatible(match.unit, match.slot))
    match.add_effect(matched(match.unit, match.slot), True)
    match.add_effect(slot_filled(match.slot), True)
    match.add_effect(available(match.unit), False)
    match.add_effect(slot_open(match.slot), False)
    if not precompute_matching:
        problem.add_action(match)

    parking_fulfill = up.InstantaneousAction("parking_fulfill", su=shunting_unit_type, unit=train_unit_type, slot=parking_slot_type, l=track_part_type)
    parking_fulfill.add_precondition(active_su(parking_fulfill.su))
    parking_fulfill.add_precondition(parked_su(parking_fulfill.su))
    parking_fulfill.add_precondition(at_su(parking_fulfill.su, parking_fulfill.l))
    parking_fulfill.add_precondition(contains_su(parking_fulfill.su, parking_fulfill.unit))
    parking_fulfill.add_precondition(parking_slot_track(parking_fulfill.slot, parking_fulfill.l))
    parking_fulfill.add_precondition(parking_compatible(parking_fulfill.unit, parking_fulfill.slot))
    parking_fulfill.add_precondition(up.Not(parked_unit_used(parking_fulfill.unit)))
    parking_fulfill.add_effect(parking_slot_fulfilled(parking_fulfill.slot), True)
    parking_fulfill.add_effect(parked_unit_used(parking_fulfill.unit), True)
    problem.add_action(parking_fulfill)


    # --- Initialise yard topology ---
    # Build the track graph, locate departure exits, and compute BFS distances
    # from the yard exit to every track part. These distances serve as the
    # `entry_distance` fluents that guide the planner's cost heuristic.

    adjacency = _build_adjacency(location_object)
    exit_ids_a, exit_ids_b = _departure_exit_ids(scenario_object, location_object)
    exit_ids = exit_ids_a.union(exit_ids_b)
    bfs_dist = _bfs_from(adjacency, exit_ids)

    parking_ids = {tp["id"] for tp in location_object["trackParts"] if tp.get("parkingAllowed")}
    parking_bfs_values = sorted({bfs_dist[pid] for pid in parking_ids if pid in bfs_dist})
    bfs_to_entry_dist = {d: i + 1 for i, d in enumerate(parking_bfs_values)}

    # --- Determine which track parts to model ---
    # Identify switch-like (zero-length connector) nodes to collapse them out,
    # then compute a corridor of relevant tracks so the planner only reasons
    # about the sub-graph that matters for this scenario.

    in_standing_trains = scenario_object.get("inStanding", {}).get("trains", [])
    out_standing_trains = scenario_object.get("outStanding", {}).get("trainRequests", [])
    out_requests = scenario_object.get("out", {}).get("trainRequests", [])
    track_occupancies = {}
    train_initial_aside = {}
    track_train_counts = {}
    coupling_candidate_track_ids = set()

    id_to_track_part = {}
    switch_like_track_ids = {tp["id"] for tp in location_object["trackParts"] if _is_switch_like_track_part(tp)}
    all_non_switch_ids = {tp["id"] for tp in location_object["trackParts"] if tp["id"] not in switch_like_track_ids}
    coupling_candidate_track_ids = {tp["id"] for tp in location_object["trackParts"] if tp.get("parkingAllowed") and tp["id"] not in switch_like_track_ids}

    corridor_nodes = _relevant_corridor_nodes(scenario_object, location_object, all_non_switch_ids, coupling_candidate_track_ids, expand_hops=CORRIDOR_EXPAND_HOPS)

    # Augment the corridor with explicit start/parking/exit/facility track ids
    # so they are never pruned away even if the corridor heuristic misses them.
    corridor_or_required = None
    required_track_ids = all_non_switch_ids
    if corridor_nodes is not None:
        corridor_or_required = {str(n) for n in corridor_nodes}
        required_track_ids = set(corridor_or_required)
        for train in in_standing_trains:
            tid = _train_initial_track_id(train, ["firstParkingTrackPart", "entryTrackPart"])
            if tid is not None:
                required_track_ids.add(str(tid))
                corridor_or_required.add(str(tid))
        for request in out_standing_trains:
            tid = request.get("lastParkingTrackPart")
            if tid is not None:
                required_track_ids.add(str(tid))
                corridor_or_required.add(str(tid))
        for request in out_requests:
            for key in ("leaveTrackPart", "lastParkingTrackPart"):
                tid = request.get(key)
                if tid is not None:
                    required_track_ids.add(str(tid))
                    corridor_or_required.add(str(tid))
        for train in scenario_object.get("in", {}).get("trains", []):
            tid = _train_initial_track_id(train, ["entryTrackPart", "firstParkingTrackPart"])
            if tid is not None:
                required_track_ids.add(str(tid))
                corridor_or_required.add(str(tid))
        for tid in exit_ids:
            required_track_ids.add(str(tid))
            corridor_or_required.add(str(tid))

        needed_facility_types = set()
        for source, _, train in all_trains_with_source(scenario_object):
            for member in train.get("members", []):
                for task in member.get("tasks", []):
                    ftype = task.get("type", {}).get("other")
                    if ftype:
                        needed_facility_types.add(ftype)
        for ftid_str, finfo in service_track_ids.items():
            if finfo["type"] in needed_facility_types and ftid_str not in switch_like_track_ids:
                required_track_ids.add(ftid_str)
                corridor_or_required.add(ftid_str)

    def _in_corridor(*ids):
        return corridor_or_required is None or all(str(i) in corridor_or_required for i in ids)

    # Create PDDL objects for every non-switch track part in the corridor
    # and set its static fluents (exit flags, parking/turning permission, service facilities, length).
    for track_part in location_object["trackParts"]:
        if track_part["id"] in switch_like_track_ids:
            continue
        if str(track_part["id"]) not in required_track_ids:
            continue
        obj = problem.add_object(track_part["name"], track_part_type)
        id_to_track_part[track_part["id"]] = obj
        if track_part["id"] in exit_ids_a:
            problem.set_initial_value(departure_exit_a(obj), True)
        if track_part["id"] in exit_ids_b:
            problem.set_initial_value(departure_exit_b(obj), True)
        if track_part.get("sawMovementAllowed", False):
            problem.set_initial_value(turning_allowed(obj), True)
        if track_part.get("parkingAllowed", False):
            problem.set_initial_value(parking_allowed(obj), True)
            problem.set_initial_value(coupling_allowed(obj), True)
            tp_id = track_part["id"]
            if tp_id in bfs_dist and bfs_dist[tp_id] in bfs_to_entry_dist:
                problem.set_initial_value(entry_distance(obj), up.Int(bfs_to_entry_dist[bfs_dist[tp_id]]))
        problem.set_initial_value(track_length(obj), up.Real(Fraction(str(track_part.get("length", 100.0)))))

        if str(track_part["id"]) in service_track_ids:
            info = service_track_ids[str(track_part["id"])]
            problem.set_initial_value(service_allowed(obj), True)
            problem.set_initial_value(facility_type(obj, id_to_facility_type[info["type"]]), True)

        if not track_part.get("parkingAllowed", False):
            problem.set_initial_value(track_length(obj), up.Real(Fraction(10**9)))

    # Connect non-switch tracks only when a directed path exists through switch-like nodes.
    a_adj = _build_directed_adj(location_object, "aSide")
    b_adj = _build_directed_adj(location_object, "bSide")
    allowed_ids = set(id_to_track_part.keys())

    for src_id in list(id_to_track_part):
        for target_id in _bfs_through_switches(a_adj, src_id, switch_like_track_ids, allowed_ids):
            if target_id != src_id and target_id in id_to_track_part:
                problem.set_initial_value(connected_aside(id_to_track_part[src_id], id_to_track_part[target_id]), True)

        for target_id in _bfs_through_switches(b_adj, src_id, switch_like_track_ids, allowed_ids):
            if target_id != src_id and target_id in id_to_track_part:
                problem.set_initial_value(connected_bside(id_to_track_part[src_id], id_to_track_part[target_id]), True)

    # --- Compute initial occupancies ---
    # Standing trains already occupy their tracks at time zero. Record their
    # position and update the track's occupied length / train count.

    id_to_unit = {}
    unit_type_by_id = {}

    for index, train in enumerate(in_standing_trains):
        initial_track_id = _train_initial_track_id(train, ["firstParkingTrackPart", "entryTrackPart"])
        if initial_track_id is not None:
            train_total_length = _train_total_length(train)
            previous_length_on_track = track_occupancies.get(initial_track_id, Fraction(0))
            train_initial_aside[_train_object_name("inStanding", index, train)] = previous_length_on_track
            track_occupancies[initial_track_id] = track_occupancies.get(initial_track_id, Fraction(0)) + train_total_length
            track_train_counts[initial_track_id] = track_train_counts.get(initial_track_id, 0) + 1

    # All out requests must be fulfilled (one departure per request).
    problem.add_goal(up.Equals(num_of_departed_trains(), up.Int(len(out_requests))))

    # Write back initial stacking distances for tracks that start occupied.
    for track_id, occupied_length_value in track_occupancies.items():
        track_obj = id_to_track_part[track_id]
        pass
        problem.set_initial_value(occupied_length(track_obj), up.Real(occupied_length_value))
        problem.set_initial_value(number_of_trains_on_track(track_obj), up.Int(track_train_counts.get(track_id, 0)))

    # --- Create shunting units for all trains ---
    # Every incoming or standing train becomes a shunting unit (SU) that can be
    # split, moved, coupled, and departed. Multi-unit compositions also get pre-allocated
    # single-unit SUs for the split action to activate.

    in_train_sus = []
    track_initial_su_order = {}
    for source, index, train in all_trains_with_source(scenario_object):
        preferred_track_keys = ["firstParkingTrackPart", "entryTrackPart"] if source == "inStanding" else ["entryTrackPart", "firstParkingTrackPart"]
        initial_track_id = _train_initial_track_id(train, preferred_track_keys)
        train_members = train["members"]

        shunting_unit = problem.add_object("su_" + _train_object_name(source, index, train), shunting_unit_type)
        problem.set_initial_value(active_su(shunting_unit), True)
        if source == "inStanding" and len(train["members"]) == 1:
            problem.set_initial_value(su_may_move(shunting_unit), True)

        needs_service = any(task for member in train.get("members", []) for task in member.get("tasks", []))
        if needs_service:
            problem.set_initial_value(serviced(shunting_unit), False)
            for member in train.get("members", []):
                for task in member.get("tasks", []):
                    task_type_str = task.get("type", {}).get("other")
                    if task_type_str and task_type_str in id_to_facility_type:
                        problem.set_initial_value(requires_facility(shunting_unit, id_to_facility_type[task_type_str]), True)

        train_total_length = _train_total_length(train)
        problem.set_initial_value(su_length(shunting_unit), up.Real(train_total_length))
        problem.set_initial_value(su_unit_count(shunting_unit), up.Int(len(train_members)))
        if source == "in":
            problem.set_initial_value(at_su(shunting_unit, phantom_track), True)
            if initial_track_id in id_to_track_part:
                problem.set_initial_value(su_arrival_track(shunting_unit, id_to_track_part[initial_track_id]), True)
        elif initial_track_id in id_to_track_part:
            problem.set_initial_value(at_su(shunting_unit, id_to_track_part[initial_track_id]), True)
            track_initial_su_order.setdefault(initial_track_id, []).append(shunting_unit)
            su_aside = train_initial_aside.get(_train_object_name(source, index, train), Fraction(0))
            pass
        composition_obj = None
        if len(train_members) > 1:
            composition_obj = problem.add_object("composition" + train["id"], arrival_composition_type)
            problem.set_initial_value(composition_needs_uncoupling(composition_obj), True)
            problem.set_initial_value(su_may_move(shunting_unit), True)
        else:
            problem.set_initial_value(su_may_move(shunting_unit), True)

        if source == "in":
            problem.set_initial_value(su_has_arrived(shunting_unit), False)
            in_train_sus.append((int(train.get("arrival", 0)), shunting_unit))

        member_unit_objs = []
        for trainunit in train_members:
            unit = trainunit["trainUnit"]
            unit_obj = problem.add_object("unit" + unit["id"], train_unit_type)
            id_to_unit[unit["id"]] = unit_obj
            unit_type_by_id[unit["id"]] = train_unit_type_key(unit)
            member_unit_objs.append(unit_obj)
            problem.set_initial_value(contains_su(shunting_unit, unit_obj), True)
            if len(train_members) == 1:
                problem.set_initial_value(single_unit_su(shunting_unit, unit_obj), True)
            else:
                single_unit_su_obj = problem.add_object("su_unit" + unit["id"], shunting_unit_type)
                problem.set_initial_value(contains_su(single_unit_su_obj, unit_obj), True)
                problem.set_initial_value(single_unit_su(single_unit_su_obj, unit_obj), True)
                problem.set_initial_value(su_length(single_unit_su_obj), up.Real(_train_unit_length(unit)))
                problem.set_initial_value(su_unit_count(single_unit_su_obj), up.Int(1))
                problem.set_initial_value(front_of(unit_obj, single_unit_su_obj), True)
                problem.set_initial_value(back_of(unit_obj, single_unit_su_obj), True)
            if composition_obj is None:
                problem.set_initial_value(available(unit_obj), True)
            else:
                problem.set_initial_value(part_of_composition(unit_obj, composition_obj), True)

        if member_unit_objs:
            problem.set_initial_value(front_of(member_unit_objs[0], shunting_unit), True)
            problem.set_initial_value(back_of(member_unit_objs[-1], shunting_unit), True)
            if len(member_unit_objs) == 1:
                problem.set_initial_value(back_of(member_unit_objs[0], shunting_unit), True)
            for first_obj, second_obj in zip(member_unit_objs, member_unit_objs[1:]):
                problem.set_initial_value(next_in_su(first_obj, second_obj, shunting_unit), True)

        for first, second in zip(train_members, train_members[1:]):
            first_obj = id_to_unit[first["trainUnit"]["id"]]
            second_obj = id_to_unit[second["trainUnit"]["id"]]
            problem.set_initial_value(unit_before(first_obj, second_obj), True)

    # Enforce arrival order: sort inbound trains by arrival time and chain the
    # arrival-precedence fluents so each train can only arrive after the previous one.
    for _tid, _sus in track_initial_su_order.items():
        problem.set_initial_value(frontmost_a_su(_sus[0]), True)
        problem.set_initial_value(frontmost_b_su(_sus[-1]), True)
        for _front, _back in zip(_sus, _sus[1:]):
            problem.set_initial_value(behind_su(_back, _front), True)

    if in_train_sus:
        in_train_sus.sort(key=lambda p: p[0])
        problem.set_initial_value(su_previous_arrived(in_train_sus[0][1]), True)
        for (_, su_a), (_, su_b) in zip(in_train_sus, in_train_sus[1:]):
            problem.set_initial_value(su_arrival_immediately_before(su_a, su_b), True)

    # --- Parking goals for outStanding requests ---
    # Each standing-out request defines slots on a specific track; matching units
    # must park there and then be marked as used via parking_fulfill.
    for request in out_standing_trains:
        track_id = request.get("lastParkingTrackPart")
        if track_id not in id_to_track_part:
            continue
        track_obj = id_to_track_part[track_id]
        request_name = "parking_" + request["displayName"]
        request_obj = problem.add_object(request_name, parking_request_type)
        for index, requested_unit in enumerate(request.get("trainUnits", [])):
            slot_name = f"{request_name}_slot{index}"
            slot_obj = problem.add_object(slot_name, parking_slot_type)
            problem.set_initial_value(parking_slot_for_request(slot_obj, request_obj), True)
            problem.set_initial_value(parking_slot_track(slot_obj, track_obj), True)
            requested_key = train_unit_type_key(requested_unit)
            for unit_id, unit_obj in id_to_unit.items():
                if unit_type_by_id[unit_id] == requested_key:
                    problem.set_initial_value(parking_compatible(unit_obj, slot_obj), True)
            problem.add_goal(parking_slot_fulfilled(slot_obj))

    # --- Departure requests for out trains ---
    # Create request objects with their coupling tracks, unit slots, and goals.
    # Single-unit requests require a departure action; two-unit requests require
    # assembly (couple) followed by departure of the assembled SU.
    request_objs = {}
    departure_slot_records = []
    for request in out_requests:
        request_name = "request" + request["displayName"]
        request_obj = problem.add_object(request_name, departure_request_type)
        request_objs[request_name] = request_obj
        problem.set_initial_value(request_open(request_obj), True)
        problem.set_initial_value(request_size(request_obj), up.Int(len(request["trainUnits"])))

        for track_id in _coupling_track_ids_for_request(request, location_object, coupling_candidate_track_ids):
            if track_id in id_to_track_part:
                problem.set_initial_value(coupling_track_for_request(request_obj, id_to_track_part[track_id]), True)

        slot_objects = []
        for index, requested_unit in enumerate(request["trainUnits"]):
            slot_obj = problem.add_object(f"{request_name}_slot{index}", request_slot_type)
            slot_objects.append(slot_obj)
            requested_key = train_unit_type_key(requested_unit)

            problem.set_initial_value(slot_open(slot_obj), True)
            problem.set_initial_value(slot_for_request(slot_obj, request_obj), True)
            departure_slot_records.append(
                (slot_obj, requested_key, index, len(request["trainUnits"]))
            )
            if len(request["trainUnits"]) == 1:
                problem.add_goal(request_departed(request_obj))

            for unit_id, unit_obj in id_to_unit.items():
                if unit_type_by_id[unit_id] == requested_key:
                    problem.set_initial_value(compatible(unit_obj, slot_obj), True)

        for first_slot, second_slot in zip(slot_objects, slot_objects[1:]):
            problem.set_initial_value(slot_before(first_slot, second_slot), True)

        if len(slot_objects) > 1:
            request_su = problem.add_object("su_" + request_name, shunting_unit_type)
            problem.set_initial_value(request_su_for_request(request_su, request_obj), True)

            problem.add_goal(request_assembled(request_obj))
            problem.add_goal(departed_su(request_su))

    if precompute_matching:
        assignment = _select_precomputed_matching(
            unit_type_by_id,
            departure_slot_records,
            matching_variant,
            matching_strategy=matching_strategy,
            unit_positions=_unit_source_positions(scenario_object),
        )
        for unit_id, slot_index in assignment:
            unit_obj = id_to_unit[unit_id]
            slot_obj, _, _, _ = departure_slot_records[slot_index]
            problem.set_initial_value(matched(unit_obj, slot_obj), True)
            problem.set_initial_value(slot_filled(slot_obj), True)
            problem.set_initial_value(available(unit_obj), False)
            problem.set_initial_value(slot_open(slot_obj), False)
            print(f"Precomputed match: {unit_obj.name} -> {slot_obj.name}")

    if output_file is None:
        output_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", f"{scenario_name}.pddl")

    # Serialise the unified-planning Problem to PDDL.
    writer = PDDLWriter(problem)
    writer.write_problem(output_file)

    print(f"Problem file written to: {output_file}")

    if domain_file is not None:
        if os.sep not in domain_file:
            domain_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", domain_file)
        writer.write_domain(domain_file)


if __name__ == "__main__":
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level.upper())

    args.domain_file = "domain.pddl" if args.domain_file is None else args.domain_file

    create_instance_from_scenario(
        domain_file=args.domain_file,
        path_to_folder=args.path_to_folder,
        scenario_file=args.scenario_file,
        location_file=args.location_file,
        output_file=args.output_file,
        precompute_matching=args.precompute_matching,
        matching_variant=args.matching_variant,
        matching_strategy=args.matching_strategy,
    )
