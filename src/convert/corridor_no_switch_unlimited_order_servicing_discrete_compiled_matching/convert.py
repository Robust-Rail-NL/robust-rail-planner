import os
import json
import logging
import argparse
from collections import Counter, defaultdict, deque
from fractions import Fraction
import unified_planning.shortcuts as up
from unified_planning.io import PDDLWriter

try:
    import numpy as np
    from scipy.optimize import Bounds, LinearConstraint, milp
    from scipy.sparse import lil_matrix
except ImportError:
    np = None


parser = argparse.ArgumentParser()
parser.add_argument("-p", "--path-to-folder", required=False, default=None)
parser.add_argument("-s", "--scenario-file", required=False, default="scenario_example1.json")
parser.add_argument("-l", "--location-file", required=False, default="location.json")
parser.add_argument("-o", "--output-file", required=False, default=None)
parser.add_argument("-d", "--domain-file", required=False, default=None)
parser.add_argument("--log-level", default="ERROR", required=False)
parser.add_argument("--matching-variant", type=int, default=0)


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
    ids = [req["leaveTrackPart"] for req in scenario_object.get("out", []) if "leaveTrackPart" in req]
    if not ids:
        ids = [t["entryTrackPart"] for t in scenario_object.get("in", []) if "entryTrackPart" in t]

    track_parts = location_object.get("trackParts", [])
    ids_aside = {tp["id"] for tp in track_parts if tp["id"] in ids and tp.get("bSide")}
    ids_bside = {tp["id"] for tp in track_parts if tp["id"] in ids and tp.get("aSide")}

    return ids_aside, ids_bside


def train_unit_type_key(train_unit):
    # Normalized train-unit identity used to match available units to request slots.
    return (train_unit.get("typePrefix"), train_unit.get("carriages"))


def all_trains_with_source(scenario_object):
    # Keep source/index so train units can be linked back to their physical train.
    for index, train in enumerate(scenario_object.get("in", [])):
        yield "in", index, train
    for index, train in enumerate(scenario_object.get("inStanding", [])):
        yield "inStanding", index, train


def _coupling_track_ids_for_request(request, location_object,
                                    candidate_track_ids, train_unit_types):
    # Prefer request-specific parking/departure information, otherwise use nearby coupling tracks.
    candidate_track_ids = {str(track_id) for track_id in candidate_track_ids}
    required_length = float(_train_total_length(train_unit_types, request))
    track_length_by_id = {
        str(track_part["id"]): float(track_part.get("length", 0.0))
        for track_part in location_object.get("trackParts", [])
    }
    candidate_track_ids = {
        track_id
        for track_id in candidate_track_ids
        if track_length_by_id.get(track_id, 0.0) >= required_length
    }
    if not candidate_track_ids:
        raise ValueError(
            f"No coupling track can hold request {request.get('displayName')} "
            f"with length {required_length}"
        )
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
    return {train_unit_type_key(member) for member in train.get("members", [])}


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
            positions[member["id"]] = (index, len(members))
    return positions


def _has_order_sensitive_matching(scenario_object):
    source_types = {
        train_unit_type_key(member)
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


def _select_order_preserving_matching(
    unit_type_by_id, slot_records, unit_positions, allowed_slot_indices=None
):
    assignments = []
    unit_order = list(unit_type_by_id)
    # Limit matching to slots not already filled by a preserved composition.
    if allowed_slot_indices is None:
        allowed_slot_indices = list(range(len(slot_records)))
    requested_types = list(
        dict.fromkeys(slot_records[index][1] for index in allowed_slot_indices)
    )

    # Compatibility is exact by train-unit type, so each type can be optimized independently.
    for requested_type in requested_types:
        slots = [
            index
            for index in allowed_slot_indices
            if slot_records[index][1] == requested_type
        ]
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


def _optimize_composition_preserving_matching(
    unit_type_by_id, slot_records, source_groups
):
    if np is None:
        return None

    source_index_by_unit = {
        unit_id: source_index
        for source_index, (unit_ids, _) in enumerate(source_groups)
        for unit_id in unit_ids
    }
    request_index_by_slot = {}
    request_index = -1
    for slot_index, (_, _, position, _) in enumerate(slot_records):
        if position == 0:
            request_index += 1
        request_index_by_slot[slot_index] = request_index

    # Create assignment variables only for type-compatible unit and slot pairs.
    compatible_pairs = [
        (unit_id, slot_index)
        for unit_id, unit_type in unit_type_by_id.items()
        for slot_index, slot_record in enumerate(slot_records)
        if unit_type == slot_record[1]
    ]
    edge_pairs = [
        (source_index, request_id)
        for source_index in range(len(source_groups))
        for request_id in range(request_index + 1)
    ]
    x_index = {pair: index for index, pair in enumerate(compatible_pairs)}
    y_offset = len(compatible_pairs)
    y_index = {pair: y_offset + index for index, pair in enumerate(edge_pairs)}
    variable_count = y_offset + len(edge_pairs)

    rows = []
    lower = []
    upper = []
    # A unit may be used once, while every outgoing slot must be filled once.
    for unit_id in unit_type_by_id:
        rows.append([x_index[pair] for pair in compatible_pairs if pair[0] == unit_id])
        lower.append(0.0)
        upper.append(1.0)
    for slot_index in range(len(slot_records)):
        rows.append([x_index[pair] for pair in compatible_pairs if pair[1] == slot_index])
        lower.append(1.0)
        upper.append(1.0)

    # Link each selected assignment to its source-composition/request pair.
    matrix = lil_matrix((len(rows) + len(compatible_pairs), variable_count))
    for row_index, columns in enumerate(rows):
        matrix[row_index, columns] = 1.0
    constraint_row = len(rows)
    for pair, column in x_index.items():
        unit_id, slot_index = pair
        edge = (source_index_by_unit[unit_id], request_index_by_slot[slot_index])
        matrix[constraint_row, column] = 1.0
        matrix[constraint_row, y_index[edge]] = -1.0
        lower.append(-np.inf)
        upper.append(0.0)
        constraint_row += 1

    # Minimize composition fragmentation, then prefer chronological assignments.
    objective = np.zeros(variable_count)
    chronological_position = {
        unit_id: position
        for position, unit_id in enumerate(
            unit_id for unit_ids, _ in source_groups for unit_id in unit_ids
        )
    }
    for pair, column in x_index.items():
        unit_id, slot_index = pair
        objective[column] = 1e-5 * abs(
            chronological_position.get(unit_id, slot_index) - slot_index
        )
    for column in y_index.values():
        objective[column] = 1.0

    result = milp(
        c=objective,
        integrality=np.ones(variable_count),
        bounds=Bounds(np.zeros(variable_count), np.ones(variable_count)),
        constraints=LinearConstraint(matrix.tocsr(), np.array(lower), np.array(upper)),
        options={"time_limit": 30.0},
    )
    if not result.success:
        return None
    return sorted(
        [pair for pair, column in x_index.items() if result.x[column] > 0.5],
        key=lambda item: item[1],
    )


def _select_composition_preserving_matching(
    scenario_object, unit_type_by_id, slot_records, unit_positions
):
    # Keep each source composition's membership and unit order together.
    source_rows = list(all_trains_with_source(scenario_object))
    source_rows.sort(
        key=lambda row: (
            0 if row[0] == "inStanding" else 1,
            int(row[2].get("arrival", 0)),
            row[1],
        )
    )
    source_groups = []
    for _, _, train in source_rows:
        unit_ids = [
            member["id"]
            for member in train.get("members", [])
            if member["id"] in unit_type_by_id
        ]
        if unit_ids:
            source_groups.append(
                (unit_ids, [unit_type_by_id[unit_id] for unit_id in unit_ids])
            )

    optimized_assignment = _optimize_composition_preserving_matching(
        unit_type_by_id, slot_records, source_groups
    )
    if optimized_assignment is not None:
        return optimized_assignment

    # Reuse complete incoming compositions where possible before assigning the
    # remaining units with the existing order-preserving matcher.
    assignments = []
    assigned_units = set()
    assigned_slots = set()
    used_groups = set()
    slot_offset = 0
    for request in scenario_object.get("out", []):
        requested_types = [train_unit_type_key(unit) for unit in request.get("trainUnits", [])]
        request_slots = list(range(slot_offset, slot_offset + len(requested_types)))
        slot_offset += len(requested_types)
        for group_index, (unit_ids, source_types) in enumerate(source_groups):
            if group_index in used_groups or source_types != requested_types:
                continue
            assignments.extend(zip(unit_ids, request_slots))
            assigned_units.update(unit_ids)
            assigned_slots.update(request_slots)
            used_groups.add(group_index)
            break

    remaining_units = {
        unit_id: unit_type
        for unit_id, unit_type in unit_type_by_id.items()
        if unit_id not in assigned_units
    }
    remaining_slots = [
        index for index in range(len(slot_records)) if index not in assigned_slots
    ]
    # Fill slots not covered by exact composition matches while preserving order.
    assignments.extend(
        _select_order_preserving_matching(
            remaining_units,
            slot_records,
            unit_positions,
            allowed_slot_indices=remaining_slots,
        )
    )
    grouped_assignment = sorted(assignments, key=lambda item: item[1])

    # A chronological unit stream minimizes composition fragmentation when exact
    # source/request sizes differ, such as pairs that must form triples.
    stream_units = [unit_id for unit_ids, _ in source_groups for unit_id in unit_ids]
    stream_assignment = list(zip(stream_units, range(len(slot_records))))
    stream_compatible = len(stream_units) == len(slot_records) and all(
        unit_type_by_id[unit_id] == slot_records[slot_index][1]
        for unit_id, slot_index in stream_assignment
    )
    if not stream_compatible:
        return grouped_assignment

    source_index_by_unit = {
        unit_id: source_index
        for source_index, (unit_ids, _) in enumerate(source_groups)
        for unit_id in unit_ids
    }
    request_index_by_slot = {}
    request_index = -1
    for slot_index, (_, _, position, _) in enumerate(slot_records):
        if position == 0:
            request_index += 1
        request_index_by_slot[slot_index] = request_index

    def fragmentation_score(candidate):
        # Count distinct source-composition to departure-request assignments.
        edges = {
            (source_index_by_unit[unit_id], request_index_by_slot[slot_index])
            for unit_id, slot_index in candidate
        }
        return len(edges)

    return min(grouped_assignment, stream_assignment, key=fragmentation_score)


def _select_precomputed_matching(
    unit_type_by_id,
    slot_records,
    matching_variant,
    matching_strategy="stable",
    unit_positions=None,
    scenario_object=None,
):
    if matching_variant < 0:
        raise ValueError("matching_variant must be non-negative")

    if matching_strategy == "order_preserving" and matching_variant == 0:
        return _select_order_preserving_matching(
            unit_type_by_id, slot_records, unit_positions or {}
        )
    if matching_strategy == "composition_preserving" and matching_variant == 0:
        return _select_composition_preserving_matching(
            scenario_object or {},
            unit_type_by_id,
            slot_records,
            unit_positions or {},
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


def _departure_matching_candidates(scenario_object, unit_type_by_id):
    # Count demand by type so departure matching cannot consume parking units.
    outgoing_counts = Counter(
        train_unit_type_key(unit)
        for request in scenario_object.get("out", [])
        for unit in request.get("trainUnits", [])
    )
    parking_counts = Counter(
        train_unit_type_key(unit)
        for request in scenario_object.get("outStanding", [])
        for unit in request.get("trainUnits", [])
    )
    candidates_by_type = defaultdict(list)
    for source, _, train in all_trains_with_source(scenario_object):
        for member in train.get("members", []):
            candidates_by_type[train_unit_type_key(member)].append(
                (0 if source == "inStanding" else 1, member["id"])
            )

    reserved = set()
    for unit_type, parking_count in parking_counts.items():
        candidates = sorted(candidates_by_type[unit_type])
        required = outgoing_counts[unit_type] + parking_count
        if len(candidates) < required:
            raise ValueError(
                f"Not enough units of type {unit_type} for both departure and parking requests"
            )
        # Standing units are ordered first and reserved for parking where possible.
        reserved.update(unit_id for _, unit_id in candidates[:parking_count])

    return {
        unit_id: unit_type
        for unit_id, unit_type in unit_type_by_id.items()
        if unit_id not in reserved
    }


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


def _relevant_corridor_nodes(scenario_object, location_object,
                             known_track_ids, coupling_candidate_track_ids,
                             train_unit_types, expand_hops=CORRIDOR_EXPAND_HOPS):
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

    for request in scenario_object.get("out", []):
        request_keys = _request_type_keys(request)
        coupling_ids = [str(c) for c in
                        _coupling_track_ids_for_request(request, location_object, coupling_candidate_track_ids, train_unit_types)]
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

    for request in scenario_object.get("outStanding", []):
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


def _train_total_length(train_unit_types, train) -> Fraction:
    # Sum the physical length of every unit in an arriving composition or outgoing request.
    total_length = Fraction(0)
    if "members" in train:
        for member in train.get("members", []):
            total_length += _train_unit_length(train_unit_types, member)
    elif "trainUnits" in train:
        for tu in train.get("trainUnits", []):
            total_length += _train_unit_length(train_unit_types, tu)
    return total_length


def _train_unit_length(train_unit_types, train_unit):
    # Physical length of one atomic train unit, used for single-unit shunting units.
    return Fraction(str(
        train_unit_types[train_unit_type_key(train_unit)]["length"]
    ))


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


def create_instance_from_scenario(
    path_to_folder=None,
    scenario_file=None,
    location_file=None,
    output_file=None,
    domain_file=None,
    matching_variant=0,
):
    precompute_matching = True
    matching_strategy = "composition_preserving"
    compile_precomputed_actions = True
    if path_to_folder is None:
        path_to_folder = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "scenario-planning-inputs", "Location_KleineBinckhorst")

    if location_file is None:
        location_file = os.path.join(path_to_folder, "location.json")
    elif not os.sep in location_file:
        location_file = os.path.join(path_to_folder, location_file)

    if scenario_file is None:
        scenario_file = os.path.join(path_to_folder, "scenarios", "scenario_example1.json")
        scenario_name = "scenario_example1"
    elif os.sep not in scenario_file:
        scenario_name = scenario_file.replace(".json", "")
        scenario_file = os.path.join(path_to_folder, "scenarios", scenario_file)
    else:
        scenario_name = scenario_file.split(os.sep)[-1].replace(".json", "")

    location_object = json.load(open(location_file))
    scenario_object = json.load(open(scenario_file))
    train_unit_types = {
        train_unit_type_key(tut): tut
        for tut in scenario_object["trainUnitTypes"]
    }

    problem = up.Problem(scenario_name)
    track_part_type = up.UserType("trackpart")
    train_unit_type = up.UserType("trainunit")
    departure_request_type = up.UserType("departurerequest")
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

    coupling_allowed = problem.add_fluent(up.Fluent("coupling_allowed", up.BoolType(), trackpart=track_part_type), default_initial_value=False)

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
    compiled_arrival_ready = problem.add_fluent(up.Fluent("compiled_arrival_ready", up.BoolType(), su=shunting_unit_type), default_initial_value=False)
    compiled_departure_unlocks = problem.add_fluent(up.Fluent("compiled_departure_unlocks", up.BoolType(), departing_su=shunting_unit_type, next_su=shunting_unit_type), default_initial_value=False)

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
    arrive_su.add_effect(number_of_trains_on_track(arrive_su.l), number_of_trains_on_track(arrive_su.l) + 1)
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
    move_aside_empty_su.add_precondition(up.Equals(number_of_trains_on_track(move_aside_empty_su.l_to), 0))
    move_aside_empty_su.add_precondition(su_length(move_aside_empty_su.su) <= track_length(move_aside_empty_su.l_to))
    move_aside_empty_su.add_effect(number_of_trains_on_track(move_aside_empty_su.l_from), number_of_trains_on_track(move_aside_empty_su.l_from) - 1)
    move_aside_empty_su.add_effect(number_of_trains_on_track(move_aside_empty_su.l_to), 1)
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
    move_aside_occupied_su.add_precondition(number_of_trains_on_track(move_aside_occupied_su.l_to) > 0)
    move_aside_occupied_su.add_effect(number_of_trains_on_track(move_aside_occupied_su.l_from), number_of_trains_on_track(move_aside_occupied_su.l_from) - 1)
    move_aside_occupied_su.add_effect(number_of_trains_on_track(move_aside_occupied_su.l_to), number_of_trains_on_track(move_aside_occupied_su.l_to) + 1)
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
    move_bside_empty_su.add_precondition(up.Equals(number_of_trains_on_track(move_bside_empty_su.l_to), 0))
    move_bside_empty_su.add_precondition(su_length(move_bside_empty_su.su) <= track_length(move_bside_empty_su.l_to))
    move_bside_empty_su.add_effect(number_of_trains_on_track(move_bside_empty_su.l_from), number_of_trains_on_track(move_bside_empty_su.l_from) - 1)
    move_bside_empty_su.add_effect(number_of_trains_on_track(move_bside_empty_su.l_to), 1)
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
    move_bside_occupied_su.add_precondition(number_of_trains_on_track(move_bside_occupied_su.l_to) > 0)
    move_bside_occupied_su.add_effect(number_of_trains_on_track(move_bside_occupied_su.l_from), number_of_trains_on_track(move_bside_occupied_su.l_from) - 1)
    move_bside_occupied_su.add_effect(number_of_trains_on_track(move_bside_occupied_su.l_to), number_of_trains_on_track(move_bside_occupied_su.l_to) + 1)
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
    depart_aside_su.add_precondition(must_depart_su(depart_aside_su.su))
    depart_aside_su.add_precondition(at_su(depart_aside_su.su, depart_aside_su.l))
    depart_aside_su.add_precondition(departure_exit_a(depart_aside_su.l))
    depart_aside_su.add_effect(active_su(depart_aside_su.su), False)
    depart_aside_su.add_effect(at_su(depart_aside_su.su, depart_aside_su.l), False)
    depart_aside_su.add_effect(at_su(depart_aside_su.su, phantom_track), True)
    depart_aside_su.add_effect(departed_su(depart_aside_su.su), True)
    depart_aside_su.add_effect(occupied_length(depart_aside_su.l), occupied_length(depart_aside_su.l) - su_length(depart_aside_su.su))
    depart_aside_su.add_effect(number_of_trains_on_track(depart_aside_su.l), number_of_trains_on_track(depart_aside_su.l) - 1)
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
    depart_bside_su.add_precondition(must_depart_su(depart_bside_su.su))
    depart_bside_su.add_precondition(at_su(depart_bside_su.su, depart_bside_su.l))
    depart_bside_su.add_precondition(departure_exit_b(depart_bside_su.l))
    depart_bside_su.add_effect(active_su(depart_bside_su.su), False)
    depart_bside_su.add_effect(at_su(depart_bside_su.su, depart_bside_su.l), False)
    depart_bside_su.add_effect(at_su(depart_bside_su.su, phantom_track), True)
    depart_bside_su.add_effect(departed_su(depart_bside_su.su), True)
    depart_bside_su.add_effect(occupied_length(depart_bside_su.l), occupied_length(depart_bside_su.l) - su_length(depart_bside_su.su))
    depart_bside_su.add_effect(number_of_trains_on_track(depart_bside_su.l), number_of_trains_on_track(depart_bside_su.l) - 1)
    depart_bside_su.add_effect(concurrent_movements, concurrent_movements - 1)
    depart_bside_su.add_effect(allowed_to_move_su(depart_bside_su.su), False)
    depart_bside_su.add_effect(num_of_departed_trains(), num_of_departed_trains() + 1)
    _v_depart_bside_su = up.Variable("v_depart_bside_su", shunting_unit_type)
    depart_bside_su.add_precondition(frontmost_b_su(depart_bside_su.su))
    depart_bside_su.add_effect(fluent=frontmost_b_su(_v_depart_bside_su), value=True, condition=behind_su(depart_bside_su.su, _v_depart_bside_su), forall=[_v_depart_bside_su])
    depart_bside_su.add_effect(fluent=behind_su(depart_bside_su.su, _v_depart_bside_su), value=False, condition=behind_su(depart_bside_su.su, _v_depart_bside_su), forall=[_v_depart_bside_su])
    depart_bside_su.add_effect(frontmost_b_su(depart_bside_su.su), False)
    problem.add_action(depart_bside_su)

    part_of_composition = problem.add_fluent(up.Fluent("part_of_composition", up.BoolType(), unit=train_unit_type, composition=arrival_composition_type), default_initial_value=False)
    composition_needs_uncoupling = problem.add_fluent(up.Fluent("composition_needs_uncoupling", up.BoolType(), composition=arrival_composition_type), default_initial_value=False)

    physically_coupled = problem.add_fluent(up.Fluent("physically_coupled", up.BoolType(), first=train_unit_type, second=train_unit_type), default_initial_value=False)
    request_assembled = problem.add_fluent(up.Fluent("request_assembled", up.BoolType(), request=departure_request_type), default_initial_value=False)

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

    service_su = up.InstantaneousAction('service_su', su=shunting_unit_type, l=track_part_type, f=facility_type_type)
    service_su.add_precondition(active_su(service_su.su))
    service_su.add_precondition(at_su(service_su.su, service_su.l))
    service_su.add_precondition(up.Not(serviced(service_su.su)))
    service_su.add_precondition(service_allowed(service_su.l))
    service_su.add_precondition(facility_type(service_su.l, service_su.f))
    service_su.add_precondition(requires_facility(service_su.su, service_su.f))
    service_su.add_effect(serviced(service_su.su), True)
    problem.add_action(service_su)

    depart_aside_su.add_precondition(serviced(depart_aside_su.su))
    depart_bside_su.add_precondition(serviced(depart_bside_su.su))
    park_su.add_precondition(serviced(park_su.su))

    compiled_target_request_su = problem.add_fluent(
        up.Fluent("compiled_target_request_su", up.BoolType(), unit=train_unit_type, request_su=shunting_unit_type),
        default_initial_value=False,
    )
    compiled_whole_target = problem.add_fluent(
        up.Fluent("compiled_whole_target", up.BoolType(), source_su=shunting_unit_type, request_su=shunting_unit_type),
        default_initial_value=False,
    )
    compiled_direct_departure = problem.add_fluent(
        up.Fluent("compiled_direct_departure", up.BoolType(), su=shunting_unit_type),
        default_initial_value=False,
    )
    compiled_departure_material = problem.add_fluent(
        up.Fluent("compiled_departure_material", up.BoolType(), su=shunting_unit_type),
        default_initial_value=False,
    )
    compiled_route_edge = problem.add_fluent(
        up.Fluent("compiled_route_edge", up.BoolType(), source=track_part_type, target=track_part_type),
        default_initial_value=False,
    )
    compiled_arrival_composition_su = problem.add_fluent(
        up.Fluent("compiled_arrival_composition_su", up.BoolType(), su=shunting_unit_type),
        default_initial_value=False,
    )
    compiled_uncouple_track = problem.add_fluent(
        up.Fluent("compiled_uncouple_track", up.BoolType(), su=shunting_unit_type, track=track_part_type),
        default_initial_value=False,
    )
    compiled_single_request = problem.add_fluent(
        up.Fluent("compiled_single_request", up.BoolType(), unit=train_unit_type, request=departure_request_type),
        default_initial_value=False,
    )
    compiled_coupling_track = problem.add_fluent(
        up.Fluent("compiled_coupling_track", up.BoolType(), request_su=shunting_unit_type, track=track_part_type),
        default_initial_value=False,
    )
    compiled_target_rank = problem.add_fluent(
        up.Fluent("compiled_target_rank", up.IntType(), unit=train_unit_type),
        default_initial_value=0,
    )
    compiled_front_rank = problem.add_fluent(
        up.Fluent("compiled_front_rank", up.IntType(), request_su=shunting_unit_type),
        default_initial_value=0,
    )
    compiled_back_rank = problem.add_fluent(
        up.Fluent("compiled_back_rank", up.IntType(), request_su=shunting_unit_type),
        default_initial_value=0,
    )

    if compile_precomputed_actions:
        park_su.add_precondition(up.Not(compiled_departure_material(park_su.su)))
        for movement_action in (
            move_aside_empty_su,
            move_aside_occupied_su,
            move_bside_empty_su,
            move_bside_occupied_su,
        ):
            movement_action.add_precondition(up.Not(compiled_direct_departure(movement_action.su)))
            movement_action.add_precondition(
                compiled_route_edge(movement_action.l_from, movement_action.l_to)
            )

        adopt_composition = up.InstantaneousAction(
            "compiled_adopt_composition",
            source_su=shunting_unit_type,
            request_su=shunting_unit_type,
            track=track_part_type,
        )
        adopt_composition.add_precondition(active_su(adopt_composition.source_su))
        adopt_composition.add_precondition(up.Not(active_su(adopt_composition.request_su)))
        adopt_composition.add_precondition(compiled_whole_target(adopt_composition.source_su, adopt_composition.request_su))
        adopt_composition.add_precondition(at_su(adopt_composition.source_su, adopt_composition.track))
        adopt_composition.add_precondition(serviced(adopt_composition.source_su))
        adopt_composition.add_effect(active_su(adopt_composition.source_su), False)
        adopt_composition.add_effect(active_su(adopt_composition.request_su), True)
        adopt_composition.add_effect(at_su(adopt_composition.source_su, adopt_composition.track), False)
        adopt_composition.add_effect(at_su(adopt_composition.request_su, adopt_composition.track), True)
        adopt_composition.add_effect(su_length(adopt_composition.request_su), su_length(adopt_composition.source_su))
        adopt_composition.add_effect(su_unit_count(adopt_composition.request_su), su_unit_count(adopt_composition.source_su))
        adopt_composition.add_effect(su_may_move(adopt_composition.request_su), True)
        adopt_composition.add_effect(must_depart_su(adopt_composition.request_su), True)
        adopt_composition.add_effect(
            fluent=allowed_to_move_su(adopt_composition.request_su),
            value=True,
            condition=allowed_to_move_su(adopt_composition.source_su),
        )
        adopt_composition.add_effect(
            fluent=allowed_to_move_su(adopt_composition.source_su),
            value=False,
            condition=allowed_to_move_su(adopt_composition.source_su),
        )
        # Transfer unit membership and internal order to the adopted request SU.
        adopted_unit = up.Variable("adopted_unit", train_unit_type)
        adopted_next_unit = up.Variable("adopted_next_unit", train_unit_type)
        adopted_request = up.Variable("adopted_request", departure_request_type)
        adopted_track_neighbor = up.Variable("adopted_track_neighbor", shunting_unit_type)
        adopt_composition.add_effect(fluent=contains_su(adopt_composition.request_su, adopted_unit), value=True, condition=contains_su(adopt_composition.source_su, adopted_unit), forall=[adopted_unit])
        adopt_composition.add_effect(fluent=contains_su(adopt_composition.source_su, adopted_unit), value=False, condition=contains_su(adopt_composition.source_su, adopted_unit), forall=[adopted_unit])
        adopt_composition.add_effect(fluent=front_of(adopted_unit, adopt_composition.request_su), value=True, condition=front_of(adopted_unit, adopt_composition.source_su), forall=[adopted_unit])
        adopt_composition.add_effect(fluent=front_of(adopted_unit, adopt_composition.source_su), value=False, condition=front_of(adopted_unit, adopt_composition.source_su), forall=[adopted_unit])
        adopt_composition.add_effect(fluent=back_of(adopted_unit, adopt_composition.request_su), value=True, condition=back_of(adopted_unit, adopt_composition.source_su), forall=[adopted_unit])
        adopt_composition.add_effect(fluent=back_of(adopted_unit, adopt_composition.source_su), value=False, condition=back_of(adopted_unit, adopt_composition.source_su), forall=[adopted_unit])
        adopt_composition.add_effect(fluent=next_in_su(adopted_unit, adopted_next_unit, adopt_composition.request_su), value=True, condition=next_in_su(adopted_unit, adopted_next_unit, adopt_composition.source_su), forall=[adopted_unit, adopted_next_unit])
        adopt_composition.add_effect(fluent=next_in_su(adopted_unit, adopted_next_unit, adopt_composition.source_su), value=False, condition=next_in_su(adopted_unit, adopted_next_unit, adopt_composition.source_su), forall=[adopted_unit, adopted_next_unit])
        adopt_composition.add_effect(fluent=request_assembled(adopted_request), value=True, condition=request_su_for_request(adopt_composition.request_su, adopted_request), forall=[adopted_request])
        adopt_composition.add_effect(fluent=frontmost_a_su(adopt_composition.request_su), value=True, condition=frontmost_a_su(adopt_composition.source_su))
        adopt_composition.add_effect(fluent=frontmost_b_su(adopt_composition.request_su), value=True, condition=frontmost_b_su(adopt_composition.source_su))
        adopt_composition.add_effect(frontmost_a_su(adopt_composition.source_su), False)
        adopt_composition.add_effect(frontmost_b_su(adopt_composition.source_su), False)
        # Replace the source SU with the request SU in the surrounding track order.
        adopt_composition.add_effect(fluent=behind_su(adopted_track_neighbor, adopt_composition.request_su), value=True, condition=behind_su(adopted_track_neighbor, adopt_composition.source_su), forall=[adopted_track_neighbor])
        adopt_composition.add_effect(fluent=behind_su(adopted_track_neighbor, adopt_composition.source_su), value=False, condition=behind_su(adopted_track_neighbor, adopt_composition.source_su), forall=[adopted_track_neighbor])
        adopt_composition.add_effect(fluent=behind_su(adopt_composition.request_su, adopted_track_neighbor), value=True, condition=behind_su(adopt_composition.source_su, adopted_track_neighbor), forall=[adopted_track_neighbor])
        adopt_composition.add_effect(fluent=behind_su(adopt_composition.source_su, adopted_track_neighbor), value=False, condition=behind_su(adopt_composition.source_su, adopted_track_neighbor), forall=[adopted_track_neighbor])
        problem.add_action(adopt_composition)

        def add_compiled_uncouple(name, front):
            action = up.InstantaneousAction(
                name,
                parent_su=shunting_unit_type,
                child_su=shunting_unit_type,
                unit=train_unit_type,
                track=track_part_type,
            )
            action.add_precondition(active_su(action.parent_su))
            action.add_precondition(compiled_arrival_composition_su(action.parent_su))
            action.add_precondition(up.Not(compiled_direct_departure(action.parent_su)))
            action.add_precondition(compiled_uncouple_track(action.parent_su, action.track))
            action.add_precondition(allowed_to_move_su(action.parent_su))
            action.add_precondition(up.Not(active_su(action.child_su)))
            action.add_precondition(contains_su(action.parent_su, action.unit))
            action.add_precondition(contains_su(action.child_su, action.unit))
            action.add_precondition(single_unit_su(action.child_su, action.unit))
            action.add_precondition(front_of(action.unit, action.parent_su) if front else back_of(action.unit, action.parent_su))
            action.add_precondition(at_su(action.parent_su, action.track))
            action.add_precondition(serviced(action.parent_su))
            action.add_precondition(up.GE(su_unit_count(action.parent_su), 2))
            action.add_effect(active_su(action.child_su), True)
            action.add_effect(su_may_move(action.parent_su), True)
            action.add_effect(su_may_move(action.child_su), True)
            action.add_effect(allowed_to_move_su(action.parent_su), False)
            action.add_effect(concurrent_movements, concurrent_movements - 1)
            action.add_effect(at_su(action.child_su, action.track), True)
            action.add_effect(su_length(action.parent_su), su_length(action.parent_su) - su_length(action.child_su))
            action.add_effect(su_unit_count(action.parent_su), su_unit_count(action.parent_su) - 1)
            action.add_effect(su_unit_count(action.child_su), 1)
            action.add_effect(number_of_trains_on_track(action.track), number_of_trains_on_track(action.track) + 1)
            action.add_effect(contains_su(action.parent_su, action.unit), False)
            action.add_effect(front_of(action.unit, action.parent_su) if front else back_of(action.unit, action.parent_su), False)
            action.add_effect(front_of(action.unit, action.child_su), True)
            action.add_effect(back_of(action.unit, action.child_su), True)
            # Promote the remaining end unit and release composition membership.
            uncoupled_neighbor_unit = up.Variable(f"{name}_neighbor_unit", train_unit_type)
            uncoupled_composition = up.Variable(f"{name}_composition", arrival_composition_type)
            if front:
                neighbor_condition = next_in_su(action.unit, uncoupled_neighbor_unit, action.parent_su)
                action.add_effect(fluent=front_of(uncoupled_neighbor_unit, action.parent_su), value=True, condition=neighbor_condition, forall=[uncoupled_neighbor_unit])
                action.add_effect(fluent=next_in_su(action.unit, uncoupled_neighbor_unit, action.parent_su), value=False, condition=neighbor_condition, forall=[uncoupled_neighbor_unit])
            else:
                neighbor_condition = next_in_su(uncoupled_neighbor_unit, action.unit, action.parent_su)
                action.add_effect(fluent=back_of(uncoupled_neighbor_unit, action.parent_su), value=True, condition=neighbor_condition, forall=[uncoupled_neighbor_unit])
                action.add_effect(fluent=next_in_su(uncoupled_neighbor_unit, action.unit, action.parent_su), value=False, condition=neighbor_condition, forall=[uncoupled_neighbor_unit])
            pair_condition = up.And(neighbor_condition, up.Equals(su_unit_count(action.parent_su), 2))
            action.add_effect(fluent=single_unit_su(action.parent_su, uncoupled_neighbor_unit), value=True, condition=pair_condition, forall=[uncoupled_neighbor_unit])
            action.add_effect(
                fluent=part_of_composition(action.unit, uncoupled_composition),
                value=False,
                condition=part_of_composition(action.unit, uncoupled_composition),
                forall=[uncoupled_composition],
            )
            action.add_effect(
                fluent=part_of_composition(uncoupled_neighbor_unit, uncoupled_composition),
                value=False,
                condition=up.And(pair_condition, part_of_composition(uncoupled_neighbor_unit, uncoupled_composition)),
                forall=[uncoupled_neighbor_unit, uncoupled_composition],
            )
            # Insert the detached child at the correct end of the track order.
            uncoupled_track_neighbor = up.Variable(f"{name}_track_neighbor", shunting_unit_type)
            if front:
                action.add_effect(fluent=frontmost_a_su(action.child_su), value=True, condition=frontmost_a_su(action.parent_su))
                action.add_effect(frontmost_a_su(action.parent_su), False)
                action.add_effect(behind_su(action.parent_su, action.child_su), True)
                action.add_effect(fluent=behind_su(action.child_su, uncoupled_track_neighbor), value=True, condition=behind_su(action.parent_su, uncoupled_track_neighbor), forall=[uncoupled_track_neighbor])
                action.add_effect(fluent=behind_su(action.parent_su, uncoupled_track_neighbor), value=False, condition=behind_su(action.parent_su, uncoupled_track_neighbor), forall=[uncoupled_track_neighbor])
            else:
                action.add_effect(fluent=frontmost_b_su(action.child_su), value=True, condition=frontmost_b_su(action.parent_su))
                action.add_effect(frontmost_b_su(action.parent_su), False)
                action.add_effect(behind_su(action.child_su, action.parent_su), True)
                action.add_effect(fluent=behind_su(uncoupled_track_neighbor, action.child_su), value=True, condition=behind_su(uncoupled_track_neighbor, action.parent_su), forall=[uncoupled_track_neighbor])
                action.add_effect(fluent=behind_su(uncoupled_track_neighbor, action.parent_su), value=False, condition=behind_su(uncoupled_track_neighbor, action.parent_su), forall=[uncoupled_track_neighbor])
            problem.add_action(action)

        add_compiled_uncouple("compiled_uncouple_front", True)
        add_compiled_uncouple("compiled_uncouple_back", False)

        compiled_start = up.InstantaneousAction(
            "compiled_start_request",
            source_su=shunting_unit_type,
            unit=train_unit_type,
            request_su=shunting_unit_type,
            track=track_part_type,
        )
        compiled_start.add_precondition(active_su(compiled_start.source_su))
        compiled_start.add_precondition(up.Not(parked_su(compiled_start.source_su)))
        compiled_start.add_precondition(up.Not(active_su(compiled_start.request_su)))
        compiled_start.add_precondition(contains_su(compiled_start.source_su, compiled_start.unit))
        compiled_start.add_precondition(single_unit_su(compiled_start.source_su, compiled_start.unit))
        compiled_start.add_precondition(compiled_target_request_su(compiled_start.unit, compiled_start.request_su))
        compiled_start.add_precondition(at_su(compiled_start.source_su, compiled_start.track))
        compiled_start.add_precondition(coupling_allowed(compiled_start.track))
        compiled_start.add_precondition(compiled_coupling_track(compiled_start.request_su, compiled_start.track))
        compiled_start.add_precondition(serviced(compiled_start.source_su))
        compiled_start.add_effect(active_su(compiled_start.source_su), False)
        compiled_start.add_effect(active_su(compiled_start.request_su), True)
        compiled_start.add_effect(at_su(compiled_start.source_su, compiled_start.track), False)
        compiled_start.add_effect(at_su(compiled_start.request_su, compiled_start.track), True)
        compiled_start.add_effect(su_length(compiled_start.request_su), su_length(compiled_start.source_su))
        compiled_start.add_effect(su_unit_count(compiled_start.request_su), 1)
        compiled_start.add_effect(contains_su(compiled_start.request_su, compiled_start.unit), True)
        compiled_start.add_effect(front_of(compiled_start.unit, compiled_start.request_su), True)
        compiled_start.add_effect(back_of(compiled_start.unit, compiled_start.request_su), True)
        compiled_start.add_effect(compiled_front_rank(compiled_start.request_su), compiled_target_rank(compiled_start.unit))
        compiled_start.add_effect(compiled_back_rank(compiled_start.request_su), compiled_target_rank(compiled_start.unit))
        # Replace the source SU with the new request SU in the track order.
        start_request_track_neighbor = up.Variable("start_request_track_neighbor", shunting_unit_type)
        compiled_start.add_effect(fluent=frontmost_a_su(compiled_start.request_su), value=True, condition=frontmost_a_su(compiled_start.source_su))
        compiled_start.add_effect(fluent=frontmost_b_su(compiled_start.request_su), value=True, condition=frontmost_b_su(compiled_start.source_su))
        compiled_start.add_effect(frontmost_a_su(compiled_start.source_su), False)
        compiled_start.add_effect(frontmost_b_su(compiled_start.source_su), False)
        compiled_start.add_effect(fluent=behind_su(start_request_track_neighbor, compiled_start.request_su), value=True, condition=behind_su(start_request_track_neighbor, compiled_start.source_su), forall=[start_request_track_neighbor])
        compiled_start.add_effect(fluent=behind_su(start_request_track_neighbor, compiled_start.source_su), value=False, condition=behind_su(start_request_track_neighbor, compiled_start.source_su), forall=[start_request_track_neighbor])
        compiled_start.add_effect(fluent=behind_su(compiled_start.request_su, start_request_track_neighbor), value=True, condition=behind_su(compiled_start.source_su, start_request_track_neighbor), forall=[start_request_track_neighbor])
        compiled_start.add_effect(fluent=behind_su(compiled_start.source_su, start_request_track_neighbor), value=False, condition=behind_su(compiled_start.source_su, start_request_track_neighbor), forall=[start_request_track_neighbor])
        problem.add_action(compiled_start)

        def add_compiled_end_coupling(name, front):
            action = up.InstantaneousAction(
                name,
                source_su=shunting_unit_type,
                unit=train_unit_type,
                request_su=shunting_unit_type,
                track=track_part_type,
            )
            action.add_precondition(active_su(action.source_su))
            action.add_precondition(active_su(action.request_su))
            action.add_precondition(up.Not(parked_su(action.source_su)))
            action.add_precondition(up.Not(parked_su(action.request_su)))
            action.add_precondition(contains_su(action.source_su, action.unit))
            action.add_precondition(single_unit_su(action.source_su, action.unit))
            action.add_precondition(compiled_target_request_su(action.unit, action.request_su))
            action.add_precondition(at_su(action.source_su, action.track))
            action.add_precondition(at_su(action.request_su, action.track))
            action.add_precondition(coupling_allowed(action.track))
            action.add_precondition(compiled_coupling_track(action.request_su, action.track))
            action.add_precondition(serviced(action.source_su))
            action.add_precondition(serviced(action.request_su))
            if front:
                action.add_precondition(up.Equals(compiled_target_rank(action.unit) + 1, compiled_front_rank(action.request_su)))
                action.add_precondition(behind_su(action.request_su, action.source_su))
            else:
                action.add_precondition(up.Equals(compiled_target_rank(action.unit), compiled_back_rank(action.request_su) + 1))
                action.add_precondition(behind_su(action.source_su, action.request_su))
            action.add_effect(active_su(action.source_su), False)
            action.add_effect(at_su(action.source_su, action.track), False)
            action.add_effect(su_length(action.request_su), su_length(action.request_su) + su_length(action.source_su))
            action.add_effect(su_unit_count(action.request_su), su_unit_count(action.request_su) + 1)
            action.add_effect(number_of_trains_on_track(action.track), number_of_trains_on_track(action.track) - 1)
            action.add_effect(contains_su(action.request_su, action.unit), True)
            # Update the request end unit and reconnect its surrounding track order.
            previous_request_end_unit = up.Variable(f"{name}_old_unit", train_unit_type)
            coupled_track_neighbor = up.Variable(f"{name}_neighbor", shunting_unit_type)
            if front:
                action.add_effect(fluent=front_of(previous_request_end_unit, action.request_su), value=False, condition=front_of(previous_request_end_unit, action.request_su), forall=[previous_request_end_unit])
                action.add_effect(fluent=next_in_su(action.unit, previous_request_end_unit, action.request_su), value=True, condition=front_of(previous_request_end_unit, action.request_su), forall=[previous_request_end_unit])
                action.add_effect(fluent=physically_coupled(action.unit, previous_request_end_unit), value=True, condition=front_of(previous_request_end_unit, action.request_su), forall=[previous_request_end_unit])
                action.add_effect(front_of(action.unit, action.request_su), True)
                action.add_effect(compiled_front_rank(action.request_su), compiled_target_rank(action.unit))
                action.add_effect(behind_su(action.request_su, action.source_su), False)
                action.add_effect(fluent=behind_su(action.request_su, coupled_track_neighbor), value=True, condition=behind_su(action.source_su, coupled_track_neighbor), forall=[coupled_track_neighbor])
                action.add_effect(fluent=behind_su(action.source_su, coupled_track_neighbor), value=False, condition=behind_su(action.source_su, coupled_track_neighbor), forall=[coupled_track_neighbor])
                action.add_effect(fluent=frontmost_a_su(action.request_su), value=True, condition=frontmost_a_su(action.source_su))
            else:
                action.add_effect(fluent=back_of(previous_request_end_unit, action.request_su), value=False, condition=back_of(previous_request_end_unit, action.request_su), forall=[previous_request_end_unit])
                action.add_effect(fluent=next_in_su(previous_request_end_unit, action.unit, action.request_su), value=True, condition=back_of(previous_request_end_unit, action.request_su), forall=[previous_request_end_unit])
                action.add_effect(fluent=physically_coupled(previous_request_end_unit, action.unit), value=True, condition=back_of(previous_request_end_unit, action.request_su), forall=[previous_request_end_unit])
                action.add_effect(back_of(action.unit, action.request_su), True)
                action.add_effect(compiled_back_rank(action.request_su), compiled_target_rank(action.unit))
                action.add_effect(behind_su(action.source_su, action.request_su), False)
                action.add_effect(fluent=behind_su(coupled_track_neighbor, action.request_su), value=True, condition=behind_su(coupled_track_neighbor, action.source_su), forall=[coupled_track_neighbor])
                action.add_effect(fluent=behind_su(coupled_track_neighbor, action.source_su), value=False, condition=behind_su(coupled_track_neighbor, action.source_su), forall=[coupled_track_neighbor])
                action.add_effect(fluent=frontmost_b_su(action.request_su), value=True, condition=frontmost_b_su(action.source_su))
            action.add_effect(frontmost_a_su(action.source_su), False)
            action.add_effect(frontmost_b_su(action.source_su), False)
            problem.add_action(action)

        add_compiled_end_coupling("compiled_couple_front", True)
        add_compiled_end_coupling("compiled_couple_back", False)

        def add_compiled_single_departure(name, aside):
            action = up.InstantaneousAction(name, su=shunting_unit_type, unit=train_unit_type, request=departure_request_type, l=track_part_type)
            action.add_precondition(active_su(action.su))
            action.add_precondition(allowed_to_move_su(action.su))
            action.add_precondition(contains_su(action.su, action.unit))
            action.add_precondition(single_unit_su(action.su, action.unit))
            action.add_precondition(compiled_single_request(action.unit, action.request))
            action.add_precondition(at_su(action.su, action.l))
            action.add_precondition(departure_exit_a(action.l) if aside else departure_exit_b(action.l))
            action.add_precondition(frontmost_a_su(action.su) if aside else frontmost_b_su(action.su))
            action.add_precondition(serviced(action.su))
            action.add_effect(active_su(action.su), False)
            action.add_effect(at_su(action.su, action.l), False)
            action.add_effect(at_su(action.su, phantom_track), True)
            action.add_effect(departed_su(action.su), True)
            action.add_effect(occupied_length(action.l), occupied_length(action.l) - su_length(action.su))
            action.add_effect(request_departed(action.request), True)
            action.add_effect(num_of_departed_trains(), num_of_departed_trains() + 1)
            action.add_effect(number_of_trains_on_track(action.l), number_of_trains_on_track(action.l) - 1)
            action.add_effect(concurrent_movements, concurrent_movements - 1)
            action.add_effect(allowed_to_move_su(action.su), False)
            # Promote the adjacent SU to the exposed track end after departure.
            departure_track_neighbor = up.Variable(f"{name}_neighbor", shunting_unit_type)
            if aside:
                action.add_effect(fluent=frontmost_a_su(departure_track_neighbor), value=True, condition=behind_su(departure_track_neighbor, action.su), forall=[departure_track_neighbor])
                action.add_effect(fluent=behind_su(departure_track_neighbor, action.su), value=False, condition=behind_su(departure_track_neighbor, action.su), forall=[departure_track_neighbor])
                action.add_effect(frontmost_a_su(action.su), False)
            else:
                action.add_effect(fluent=frontmost_b_su(departure_track_neighbor), value=True, condition=behind_su(action.su, departure_track_neighbor), forall=[departure_track_neighbor])
                action.add_effect(fluent=behind_su(action.su, departure_track_neighbor), value=False, condition=behind_su(action.su, departure_track_neighbor), forall=[departure_track_neighbor])
                action.add_effect(frontmost_b_su(action.su), False)
            problem.add_action(action)
            return action

        compiled_depart_aside = add_compiled_single_departure("compiled_depart_aside_for_request", True)
        compiled_depart_bside = add_compiled_single_departure("compiled_depart_bside_for_request", False)

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

    in_standing_trains = scenario_object.get("inStanding", [])
    out_standing_trains = scenario_object.get("outStanding", [])
    out_requests = scenario_object.get("out", [])
    track_occupancies = {}
    track_train_counts = {}
    coupling_candidate_track_ids = set()

    id_to_track_part = {}
    switch_like_track_ids = {tp["id"] for tp in location_object["trackParts"] if _is_switch_like_track_part(tp)}
    all_non_switch_ids = {tp["id"] for tp in location_object["trackParts"] if tp["id"] not in switch_like_track_ids}
    coupling_candidate_track_ids = {tp["id"] for tp in location_object["trackParts"] if tp.get("parkingAllowed") and tp["id"] not in switch_like_track_ids}

    corridor_nodes = _relevant_corridor_nodes(scenario_object,
                                              location_object,
                                              all_non_switch_ids,
                                              coupling_candidate_track_ids,
                                              train_unit_types,
                                              expand_hops=CORRIDOR_EXPAND_HOPS)

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
        for train in scenario_object.get("in", []):
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
            train_total_length = _train_total_length(train_unit_types, train)
            track_occupancies[initial_track_id] = track_occupancies.get(initial_track_id, Fraction(0)) + train_total_length
            track_train_counts[initial_track_id] = track_train_counts.get(initial_track_id, 0) + 1

    # All out requests must be fulfilled (one departure per request).
    problem.add_goal(up.Equals(num_of_departed_trains(), up.Int(len(out_requests))))

    # Write back initial stacking distances for tracks that start occupied.
    for track_id, occupied_length_value in track_occupancies.items():
        track_obj = id_to_track_part[track_id]
        problem.set_initial_value(occupied_length(track_obj), up.Real(occupied_length_value))
        problem.set_initial_value(number_of_trains_on_track(track_obj), up.Int(track_train_counts.get(track_id, 0)))

    # --- Create shunting units for all trains ---
    # Every incoming or standing train becomes a shunting unit (SU) that can be
    # split, moved, coupled, and departed. Multi-unit compositions also get pre-allocated
    # single-unit SUs for the split action to activate.

    in_train_sus = []
    track_initial_su_order = {}
    source_composition_records = []
    single_unit_su_by_unit_name = {}
    direct_departure_sources = set()
    for source, index, train in all_trains_with_source(scenario_object):
        preferred_track_keys = ["firstParkingTrackPart", "entryTrackPart"] if source == "inStanding" else ["entryTrackPart", "firstParkingTrackPart"]
        initial_track_id = _train_initial_track_id(train, preferred_track_keys)
        train_members = train["members"]

        shunting_unit = problem.add_object("su_" + _train_object_name(source, index, train), shunting_unit_type)
        problem.set_initial_value(active_su(shunting_unit), True)
        if source == "inStanding" and len(train["members"]) == 1:
            problem.set_initial_value(su_may_move(shunting_unit), True)

        needs_service = any(task for member in train.get("members", []) for task in member.get("tasks", []))
        if source == "in" and not needs_service and initial_track_id in (exit_ids_a | exit_ids_b):
            direct_departure_sources.add(shunting_unit)
        if needs_service:
            problem.set_initial_value(serviced(shunting_unit), False)
            for member in train.get("members", []):
                for task in member.get("tasks", []):
                    task_type_str = task.get("type", {}).get("other")
                    if task_type_str and task_type_str in id_to_facility_type:
                        problem.set_initial_value(requires_facility(shunting_unit, id_to_facility_type[task_type_str]), True)

        train_total_length = _train_total_length(train_unit_types, train)
        problem.set_initial_value(su_length(shunting_unit), up.Real(train_total_length))
        problem.set_initial_value(su_unit_count(shunting_unit), up.Int(len(train_members)))
        if source == "in":
            problem.set_initial_value(at_su(shunting_unit, phantom_track), True)
            if initial_track_id in id_to_track_part:
                problem.set_initial_value(su_arrival_track(shunting_unit, id_to_track_part[initial_track_id]), True)
        elif initial_track_id in id_to_track_part:
            problem.set_initial_value(at_su(shunting_unit, id_to_track_part[initial_track_id]), True)
            track_initial_su_order.setdefault(initial_track_id, []).append(shunting_unit)
        composition_obj = None
        if len(train_members) > 1:
            composition_obj = problem.add_object("composition" + train["id"], arrival_composition_type)
            problem.set_initial_value(composition_needs_uncoupling(composition_obj), True)
            problem.set_initial_value(su_may_move(shunting_unit), True)
            if compile_precomputed_actions:
                problem.set_initial_value(compiled_arrival_composition_su(shunting_unit), True)
                if initial_track_id in id_to_track_part:
                    problem.set_initial_value(
                        compiled_uncouple_track(shunting_unit, id_to_track_part[initial_track_id]),
                        True,
                    )
        else:
            problem.set_initial_value(su_may_move(shunting_unit), True)

        if source == "in":
            problem.set_initial_value(su_has_arrived(shunting_unit), False)
            in_train_sus.append((int(train.get("arrival", 0)), shunting_unit))

        member_unit_objs = []
        for unit in train_members:
            unit_obj = problem.add_object("unit" + str(unit["id"]), train_unit_type)
            id_to_unit[unit["id"]] = unit_obj
            unit_type_by_id[unit["id"]] = train_unit_type_key(unit)
            member_unit_objs.append(unit_obj)
            problem.set_initial_value(contains_su(shunting_unit, unit_obj), True)
            if len(train_members) == 1:
                problem.set_initial_value(single_unit_su(shunting_unit, unit_obj), True)
            else:
                single_unit_su_obj = problem.add_object("su_unit" + str(unit["id"]), shunting_unit_type)
                single_unit_su_by_unit_name[unit_obj.name] = single_unit_su_obj
                problem.set_initial_value(contains_su(single_unit_su_obj, unit_obj), True)
                problem.set_initial_value(single_unit_su(single_unit_su_obj, unit_obj), True)
                problem.set_initial_value(su_length(single_unit_su_obj), up.Real(_train_unit_length(train_unit_types, unit)))
                problem.set_initial_value(su_unit_count(single_unit_su_obj), up.Int(1))
                problem.set_initial_value(front_of(unit_obj, single_unit_su_obj), True)
                problem.set_initial_value(back_of(unit_obj, single_unit_su_obj), True)
            if composition_obj is not None:
                problem.set_initial_value(part_of_composition(unit_obj, composition_obj), True)

        if member_unit_objs:
            problem.set_initial_value(front_of(member_unit_objs[0], shunting_unit), True)
            problem.set_initial_value(back_of(member_unit_objs[-1], shunting_unit), True)
            if len(member_unit_objs) == 1:
                problem.set_initial_value(back_of(member_unit_objs[0], shunting_unit), True)
            for first_obj, second_obj in zip(member_unit_objs, member_unit_objs[1:]):
                problem.set_initial_value(next_in_su(first_obj, second_obj, shunting_unit), True)
            source_composition_records.append((shunting_unit, member_unit_objs))

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
    departure_slot_records = []
    request_action_records = []
    for request in out_requests:
        request_name = "request" + request["displayName"]
        request_obj = problem.add_object(request_name, departure_request_type)
        problem.set_initial_value(request_size(request_obj), up.Int(len(request["trainUnits"])))

        coupling_track_objects = []
        for track_id in _coupling_track_ids_for_request(request, location_object, coupling_candidate_track_ids, train_unit_types):
            if track_id in id_to_track_part:
                coupling_track_object = id_to_track_part[track_id]
                coupling_track_objects.append(coupling_track_object)

        slot_objects = []
        for index, requested_unit in enumerate(request["trainUnits"]):
            slot_name = f"{request_name}_slot{index}"
            slot_objects.append(slot_name)
            requested_key = train_unit_type_key(requested_unit)

            departure_slot_records.append(
                (slot_name, requested_key, index, len(request["trainUnits"]))
            )
            if len(request["trainUnits"]) == 1:
                problem.add_goal(request_departed(request_obj))

        request_su = None
        if len(slot_objects) > 1:
            request_su = problem.add_object("su_" + request_name, shunting_unit_type)
            problem.set_initial_value(request_su_for_request(request_su, request_obj), True)

            problem.add_goal(request_assembled(request_obj))
            problem.add_goal(departed_su(request_su))
        request_action_records.append(
            (request, request_obj, request_su, slot_objects, coupling_track_objects)
        )

    assignment = []
    if precompute_matching:
        # Match departures only after reserving units needed by parking requests.
        departure_candidates = _departure_matching_candidates(
            scenario_object, unit_type_by_id
        )
        assignment = _select_precomputed_matching(
            departure_candidates,
            departure_slot_records,
            matching_variant,
            matching_strategy=matching_strategy,
            unit_positions=_unit_source_positions(scenario_object),
            scenario_object=scenario_object,
        )
        for unit_id, slot_index in assignment:
            unit_obj = id_to_unit[unit_id]
            slot_name, _, _, _ = departure_slot_records[slot_index]
            print(f"Precomputed match: {unit_obj.name} -> {slot_name}")

    if compile_precomputed_actions:
        assigned_unit_by_slot = {
            departure_slot_records[slot_index][0]: id_to_unit[unit_id]
            for unit_id, slot_index in assignment
        }

        departure_su_by_source = {}
        source_su_by_unit_sequence = {
            tuple(unit.name for unit in source_units): source_su
            for source_su, source_units in source_composition_records
        }
        source_su_by_unit_name = {
            unit.name: source_su
            for source_su, source_units in source_composition_records
            for unit in source_units
        }
        assigned_unit_names = {unit.name for unit in assigned_unit_by_slot.values()}
        for unit_name in assigned_unit_names:
            problem.set_initial_value(
                compiled_departure_material(source_su_by_unit_name[unit_name]), True
            )
            single_unit_su_obj = single_unit_su_by_unit_name.get(unit_name)
            if single_unit_su_obj is not None:
                problem.set_initial_value(
                    compiled_departure_material(single_unit_su_obj), True
                )
        compiled_request_sources = []

        for _, request_obj, request_su, slot_objects, coupling_tracks in request_action_records:
            slot_units = [assigned_unit_by_slot[slot_name] for slot_name in slot_objects]
            compiled_request_sources.append(
                (
                    request_obj,
                    request_su,
                    {source_su_by_unit_name[unit.name] for unit in slot_units},
                )
            )
            if len(slot_objects) == 1:
                problem.set_initial_value(compiled_single_request(slot_units[0], request_obj), True)
                source_su = source_su_by_unit_sequence.get((slot_units[0].name,))
                if source_su is not None:
                    departure_su_by_source[source_su] = source_su
                    if source_su in direct_departure_sources:
                        problem.set_initial_value(compiled_direct_departure(source_su), True)
                continue
            for rank, unit in enumerate(slot_units):
                problem.set_initial_value(compiled_target_request_su(unit, request_su), True)
                problem.set_initial_value(compiled_target_rank(unit), up.Int(rank))
            for source_su, source_units in source_composition_records:
                if source_units == slot_units:
                    problem.set_initial_value(compiled_whole_target(source_su, request_su), True)
                    departure_su_by_source[source_su] = request_su
                    if source_su in direct_departure_sources:
                        problem.set_initial_value(compiled_direct_departure(source_su), True)
                        problem.set_initial_value(compiled_direct_departure(request_su), True)
                    break
            for track in coupling_tracks:
                problem.set_initial_value(compiled_coupling_track(request_su, track), True)

        has_service_tasks = any(
            member.get("tasks")
            for _, _, train in all_trains_with_source(scenario_object)
            for member in train.get("members", [])
        )
        restrict_routes = not has_service_tasks and not scenario_object.get("outStanding")
        _, adjacency = _build_side_aware_track_graph(
            location_object, allowed_track_ids=id_to_track_part.keys()
        )
        allowed_route_edges = set()
        if restrict_routes:
            coupling_ids = {
                track.name.removeprefix("o_")
                for _, _, _, _, tracks in request_action_records
                for track in tracks
            }
            for exit_id in exit_ids_a | exit_ids_b:
                for coupling_id in coupling_ids:
                    route = _shortest_path(adjacency, exit_id, coupling_id)
                    for first, second in zip(route, route[1:]):
                        allowed_route_edges.add((first, second))
                        allowed_route_edges.add((second, first))
        if not allowed_route_edges:
            allowed_route_edges = {
                (source, target)
                for source, targets in adjacency.items()
                for target in targets
            }
        for source_id, target_id in allowed_route_edges:
            if source_id in id_to_track_part and target_id in id_to_track_part:
                problem.set_initial_value(
                    compiled_route_edge(id_to_track_part[source_id], id_to_track_part[target_id]),
                    True,
                )

        # When every arriving composition already exactly matches one departure request,
        # admit the next arrival only after the previous composition has departed.
        ordered_arrival_sus = [su for _, su in in_train_sus]
        if ordered_arrival_sus and all(su in departure_su_by_source for su in ordered_arrival_sus):
            arrive_su.add_precondition(compiled_arrival_ready(arrive_su.su))
            problem.set_initial_value(compiled_arrival_ready(ordered_arrival_sus[0]), True)
            for current_su, next_arrival_su in zip(ordered_arrival_sus, ordered_arrival_sus[1:]):
                departing_su = departure_su_by_source[current_su]
                problem.set_initial_value(compiled_departure_unlocks(departing_su, next_arrival_su), True)

            next_arrival = up.Variable("compiled_next_arrival", shunting_unit_type)
            for departure_action in (
                depart_aside_su,
                depart_bside_su,
                compiled_depart_aside,
                compiled_depart_bside,
            ):
                departure_action.add_effect(
                    fluent=compiled_arrival_ready(next_arrival),
                    value=True,
                    condition=compiled_departure_unlocks(departure_action.su, next_arrival),
                    forall=[next_arrival],
                )
        elif ordered_arrival_sus:
            # Requests connected through shared source compositions form independent
            # assembly components. Process one component at a time to avoid admitting
            # unrelated trains that can only congest the yard.
            arrive_su.add_precondition(compiled_arrival_ready(arrive_su.su))
            node_neighbors = {}
            request_completion = {}
            source_object_by_name = {
                source_su.name: source_su for source_su, _ in source_composition_records
            }
            for request_obj, request_su, source_sus in compiled_request_sources:
                request_node = ("request", request_obj.name)
                node_neighbors.setdefault(request_node, set())
                request_completion[request_obj.name] = (request_obj, request_su)
                for source_su in source_sus:
                    source_node = ("source", source_su.name)
                    node_neighbors.setdefault(source_node, set()).add(request_node)
                    node_neighbors[request_node].add(source_node)

            components = []
            unseen = set(node_neighbors)
            while unseen:
                start = min(unseen)
                component = set()
                queue = deque([start])
                unseen.remove(start)
                while queue:
                    node = queue.popleft()
                    component.add(node)
                    for neighbor in node_neighbors[node]:
                        if neighbor in unseen:
                            unseen.remove(neighbor)
                            queue.append(neighbor)
                components.append(component)

            arrival_rank = {su.name: rank for rank, su in enumerate(ordered_arrival_sus)}
            components.sort(
                key=lambda component: min(
                    (arrival_rank.get(name, -1) for kind, name in component if kind == "source"),
                    default=-1,
                )
            )
            incoming_names = set(arrival_rank)
            for su in ordered_arrival_sus:
                problem.set_initial_value(su_previous_arrived(su), True)

            request_sources_by_name = {
                request_obj.name: {source_su.name for source_su in source_sus}
                for request_obj, _, source_sus in compiled_request_sources
            }
            scheduled_source_names = set().union(
                *request_sources_by_name.values()
            ) if request_sources_by_name else set()
            for source_name in incoming_names - scheduled_source_names:
                problem.set_initial_value(
                    compiled_arrival_ready(source_object_by_name[source_name]), True
                )
            request_schedule = []
            for component in components:
                remaining_requests = {
                    name for kind, name in component if kind == "request"
                }
                current_sources = set()
                while remaining_requests:
                    sharing = [
                        name
                        for name in remaining_requests
                        if request_sources_by_name[name] & current_sources
                    ]
                    candidates = sharing or list(remaining_requests)
                    selected = min(
                        candidates,
                        key=lambda name: min(
                            (
                                arrival_rank.get(source_name, -1)
                                for source_name in request_sources_by_name[name]
                            ),
                            default=-1,
                        ),
                    )
                    request_schedule.append(selected)
                    current_sources = request_sources_by_name[selected]
                    remaining_requests.remove(selected)

            enabled_sources = set()
            for request_index, request_name in enumerate(request_schedule):
                needed_sources = {
                    source_name
                    for source_name in request_sources_by_name[request_name]
                    if source_name in incoming_names and source_name not in enabled_sources
                }
                if request_index == 0:
                    for source_name in needed_sources:
                        problem.set_initial_value(
                            compiled_arrival_ready(source_object_by_name[source_name]), True
                        )
                elif needed_sources:
                    previous_name = request_schedule[request_index - 1]
                    previous_request, previous_su = request_completion[previous_name]
                    advance = up.InstantaneousAction(
                        f"compiled_advance_request_{request_index}"
                    )
                    if previous_su is None:
                        advance.add_precondition(request_departed(previous_request))
                    else:
                        advance.add_precondition(departed_su(previous_su))
                    for source_name in needed_sources:
                        advance.add_effect(
                            compiled_arrival_ready(source_object_by_name[source_name]), True
                        )
                    problem.add_action(advance)
                enabled_sources.update(needed_sources)


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
        matching_variant=args.matching_variant,
    )
