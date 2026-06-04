import os
import sys
import json
import logging
import argparse
from collections import deque
from fractions import Fraction
import unified_planning.shortcuts as up
from unified_planning.io import PDDLReader, PDDLWriter

parser = argparse.ArgumentParser()
parser.add_argument("-p", "--path-to-folder", help="Specifies the directory where all data relevant to a given location resides. Defaults to ../../scenario-planning-inputs/Location_KleineBinckhorst/ (relative to this script).", required=False, default=None)
parser.add_argument("-s", "--scenario-file", help="Specifies the name of the scenario file (in solver format). Can be either a filename (written in a /scenarios/ folder below the --path mentioned above) or a full path. Defaults to 'scenario_solver_example1.json'", required=False, default="scenario_solver_example1.json")
parser.add_argument("-l", "--location-file", help="Specifies the name of the location file. Defaults to location_solver.json. Can be either a filename (relative to the --path above) or a full path.", required=False, default="location_solver.json")
parser.add_argument("-o", "--output-file", help="Specifies the name of the output pddl instance file. Defaults to {scenario_file}.pddl. Will be stored in /data/", required=False, default=None)
parser.add_argument("-d", "--domain-file", help="Specifies the name of the output pddl domain file. If none, the domain is not written. Will be stored in /data/", required=False, default=None)


### Add logging to the arguments
parser.add_argument("--log-level", default="ERROR", required=False, help="Configure the logging level (e.g., INFO, WARNING, ERROR) default=ERROR.")


def _build_adjacency(location_object):
    # Undirected graph: each trackpart id maps to the set of ids it shares an aSide/bSide connection with.
    # Both directions are added so BFS and connectivity checks work without caring about direction.
    # In: location object, contains "trackParts" section which has for every object:
    # - key "aSide" and "bSide" which contains ID's of neighboring track parts
    # - key "id" containing the ID of this track part
    # Out: dictionary mapping track part ID -> two neighboring track part ID's

    adjacency = {tp["id"]: set() for tp in location_object["trackParts"]}
    for tp in location_object["trackParts"]:
        for nb_id in tp.get("aSide", []) + tp.get("bSide", []):
            if nb_id in adjacency:
                adjacency[tp["id"]].add(nb_id)
                adjacency[nb_id].add(tp["id"])
    return adjacency


def _bfs_from(adjacency, start_ids):
    # Returns hop-distance from any of the start nodes to every reachable node.
    # Used to measure how far each track part is from the yard's departure point(s).
    # In: adjacency (see _build_adjacency), start_ids (list of ID's of all track parts that are marked as entry tracks)
    # Out: a dictionary mapping ID -> shortest hop distance from any of the start_ids tracks
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
    # In: a scenario object containing:
    # - a section "out", containing the list "trainRequests" where each element has:
    #   - "leaveTrackPart", containing a specification of from which track part a train should leave
    # - a section "in", containing the list "trains" where each element has:
    #   - "entryTrackPart", containing a specification of at which track a train will enter the yard
    # Out: a list of all trackparts that are mentioned as "leaveTrackPart" in any "out" train Request if present.
    # If there are no leaveTrackParts, it returns a list of all trackparts that are mentioned as "entryTrackPart" for any "in" train.
    # Side information comes from the location model, which owns the track topology.
    ids = [req["leaveTrackPart"] for req in scenario_object.get("out", {}).get("trainRequests", []) if "leaveTrackPart" in req]
    if not ids:
        ids = [t["entryTrackPart"] for t in scenario_object.get("in", {}).get("trains", []) if "entryTrackPart" in t]

    ids_aside = set()
    ids_bside = set()

    # filter all exits in ids that do not have any elements in aside use one line of code
    track_parts = location_object.get("trackParts", [])
    ids_aside = {tp["id"] for tp in track_parts if tp["id"] in ids and tp.get("bSide")}
    ids_bside = {tp["id"] for tp in track_parts if tp["id"] in ids and tp.get("aSide")}

    return ids_aside, ids_bside


def _compute_departure_ranks(inbound_trains):
    # Assigns rank 1 to the earliest-departing train, 2 to the next, etc.
    # Trains with equal departure times share a rank (lenient parking: either can use the same entry_distance level).
    # In: inbound_trains, a list of elements each containing:
    # - "arrival", the arrival time of the train
    # - "departure", the departure time of the train
    # Out: ranks, a dictionary mapping train ID -> rank (int), where rank indicates as the rank of when the train must leave the yard compared to other trains
    sorted_trains = sorted(inbound_trains, key=lambda t: int(t.get("departure", t["arrival"])))
    ranks = {}
    rank = 1
    prev_dep = None
    for train in sorted_trains:
        dep = int(train.get("departure", train["arrival"]))
        if dep != prev_dep and prev_dep is not None:
            rank += 1
        prev_dep = dep
        ranks[train["id"]] = rank
    return ranks


def train_unit_type_key(train_unit):
    # Normalized train-unit identity used to match available units to request slots.
    unit_type = train_unit["type"]
    return (
        unit_type.get("displayName"),
        int(unit_type.get("carriages", 0)),
        float(unit_type.get("length", 0.0)),
    )


def all_trains(scenario_object):
    # Matching can use newly arriving trains and trains already standing in the yard.
    trains = []
    trains.extend(scenario_object.get("in", {}).get("trains", []))
    trains.extend(scenario_object.get("inStanding", {}).get("trains", []))
    return trains


def all_trains_with_source(scenario_object):
    # Keep source/index so train units can be linked back to their physical train.
    for index, train in enumerate(scenario_object.get("in", {}).get("trains", [])):
        yield "in", index, train
    for index, train in enumerate(scenario_object.get("inStanding", {}).get("trains", [])):
        yield "inStanding", index, train


def all_train_requests(scenario_object):
    # Outgoing demand comes from both regular departures and outstanding requests.
    requests = []
    requests.extend(scenario_object.get("out", {}).get("trainRequests", []))
    requests.extend(scenario_object.get("outStanding", {}).get("trainRequests", []))
    return requests


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


def _track_part_side_for_neighbor(track_part, neighbor_id):
    if neighbor_id in track_part.get("aSide", []):
        return "a"
    if neighbor_id in track_part.get("bSide", []):
        return "b"
    return None


def _switch_like_components(location_object, switch_like_track_ids):
    adjacency = _build_adjacency(location_object)
    seen = set()
    components = []

    for track_id in switch_like_track_ids:
        if track_id in seen:
            continue
        component = set()
        queue = deque([track_id])
        seen.add(track_id)
        while queue:
            current = queue.popleft()
            component.add(current)
            for neighbor_id in adjacency.get(current, []):
                if neighbor_id in switch_like_track_ids and neighbor_id not in seen:
                    seen.add(neighbor_id)
                    queue.append(neighbor_id)
        components.append(component)

    return components


def _reconnect_switch_like_components(location_object, switch_like_track_ids, id_to_track_part, problem, connected_pairs, connected_aside, connected_bside):
    # Collapse each switch-like component into direct boundary-to-boundary links.
    components = _switch_like_components(location_object, switch_like_track_ids)
    original_lookup = {tp["id"]: tp for tp in location_object["trackParts"]}

    for component in components:
        boundary_ports = []
        for switch_id in component:
            switch_part = original_lookup[switch_id]
            for neighbor_id in _track_part_neighbors(switch_part):
                if neighbor_id in component or neighbor_id not in id_to_track_part:
                    continue
                side = _track_part_side_for_neighbor(switch_part, neighbor_id)
                if side is not None:
                    boundary_ports.append((neighbor_id, side))

        for left_index, (left_id, left_side) in enumerate(boundary_ports):
            for right_id, right_side in boundary_ports[left_index + 1:]:
                if left_id == right_id:
                    continue
                pair = tuple(sorted([left_id, right_id]))
                if pair in connected_pairs:
                    continue

                if left_side == "a":
                    problem.set_initial_value(connected_aside(id_to_track_part[left_id], id_to_track_part[right_id]), True)
                else:
                    problem.set_initial_value(connected_bside(id_to_track_part[left_id], id_to_track_part[right_id]), True)

                if right_side == "a":
                    problem.set_initial_value(connected_aside(id_to_track_part[right_id], id_to_track_part[left_id]), True)
                else:
                    problem.set_initial_value(connected_bside(id_to_track_part[right_id], id_to_track_part[left_id]), True)

                connected_pairs.add(pair)


def _train_object_name(source, index, train):
    # Reuse the routing branch's standing-train naming convention.
    if source == "inStanding":
        return f"train_in_standing_{index}"
    return "train" + train["id"]


def determine_initial_direction(train, location_object, initial_track_id):
    # Determines the initial direction of the train based on whether its initial track has no connection on its aSide (facing aside) or bSide (facing bside).
    # If it is parked on a turnable track sawMovementAllowed = True the direction does not matter so we default to bside.
    print("hello")
    if initial_track_id is None:
        print("Initial track ID is None")
        return None

    track_parts = location_object.get("trackParts", [])
    print(initial_track_id)
    for tp in track_parts:
        if tp["id"] == initial_track_id:
            print("found")
            if tp.get("sawMovementAllowed", False):
                print("Track is turnable, defaulting to bside")
                return False  # Default to bside if on a turnable track
            print(tp.get("aSide"), tp.get("bSide"))
            has_aside_connection = bool(tp.get("aSide"))
            has_bside_connection = bool(tp.get("bSide"))
            print(has_aside_connection, has_bside_connection)
            if has_aside_connection and not has_bside_connection:
                return True  # Facing aside
            elif not has_aside_connection and has_bside_connection:
                return False  # Facing bside
            else:
                return False  # Default to bside if both or neither connections are present


def create_instance_from_scenario(path_to_folder=None, scenario_file=None, location_file=None, output_file=None, domain_file=None):
    # Path defaults to ../../scenario-planning-inputs/Location_KleineBinckhorst/
    if path_to_folder is None:
        path_to_folder = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "scenario-planning-inputs", "Location_KleineBinckhorst")

   # If location not specified use default
    if location_file is None:
        location_file = os.path.join(path_to_folder, "location_solver.json")
    # If not full path is specified, take location file from --path
    elif not os.sep in location_file:
        location_file = os.path.join(path_to_folder, location_file)
    
    # If scenario not specified use default
    if not os.sep in scenario_file:
        scenario_name = scenario_file.replace(".json", "")
        scenario_file = os.path.join(path_to_folder, "scenarios", scenario_file)
    else:
        scenario_name = scenario_file.split(os.sep)[-1].replace(".json", "")

    location_object = json.load(open(location_file))
    scenario_object = json.load(open(scenario_file))
    # In unified planning the domain information is included in the problem class
    problem = up.Problem(scenario_name)
    track_part_type = up.UserType("trackpart")
    train_unit_type = up.UserType("trainunit")
    arrival_train_type = up.UserType("arrivaltrain")
    departure_request_type = up.UserType("departurerequest")
    request_slot_type = up.UserType("requestslot")
    arrival_composition_type = up.UserType("arrivalcomposition")
    shunting_unit_type = up.UserType("shuntingunit")

    # free           = problem.add_fluent(up.Fluent("free",           up.BoolType(), trackpart=track_part_type),                          default_initial_value=True)
    arrival        = problem.add_fluent(up.Fluent("arrival",        up.IntType(),  train=arrival_train_type))
    at             = problem.add_fluent(up.Fluent("at",             up.BoolType(), unit=arrival_train_type, trackpart=track_part_type),  default_initial_value=False)
    parking_allowed = problem.add_fluent(up.Fluent("parking_allowed", up.BoolType(), trackpart=track_part_type),                         default_initial_value=False)
    turning_allowed = problem.add_fluent(up.Fluent("turning_allowed", up.BoolType(), trackpart=track_part_type),                         default_initial_value=False)
    parked         = problem.add_fluent(up.Fluent("parked",         up.BoolType(), train=arrival_train_type),                           default_initial_value=False)
    departed       = problem.add_fluent(up.Fluent("departed",       up.BoolType(), train=arrival_train_type),                           default_initial_value=False)
    locked_train   = problem.add_fluent(up.Fluent("locked_train",   up.BoolType(), train=arrival_train_type),                           default_initial_value=False)
    # connected      = problem.add_fluent(up.Fluent("connected",      up.BoolType(), from_=track_part_type, to=track_part_type),           default_initial_value=False)
    connected_aside = problem.add_fluent(up.Fluent("connected_aside", up.BoolType(), from_=track_part_type, to=track_part_type),           default_initial_value=False)
    connected_bside = problem.add_fluent(up.Fluent("connected_bside", up.BoolType(), from_=track_part_type, to=track_part_type),           default_initial_value=False)
    departure_exit_a = problem.add_fluent(up.Fluent("departure_exit_a", up.BoolType(), trackpart=track_part_type),                          default_initial_value=False)
    departure_exit_b = problem.add_fluent(up.Fluent("departure_exit_b", up.BoolType(), trackpart=track_part_type),                          default_initial_value=False)
    entry_distance = problem.add_fluent(up.Fluent("entry_distance", up.IntType(),  trackpart=track_part_type),                          default_initial_value=up.Int(0))
    departure_rank = problem.add_fluent(up.Fluent("departure_rank", up.IntType(),  train=arrival_train_type),                           default_initial_value=up.Int(0))
    number_of_parked_trains = problem.add_fluent(up.Fluent("number_of_parked_trains", up.IntType(), trackpart=track_part_type),                 default_initial_value=up.Int(0))
    number_of_trains_on_track = problem.add_fluent(up.Fluent("number_of_trains_on_track", up.IntType(), trackpart=track_part_type),                 default_initial_value=up.Int(0))
    num_of_departed_trains = problem.add_fluent(up.Fluent("num_of_departed_trains", up.IntType()),                                      default_initial_value=up.Int(0))
    track_length = problem.add_fluent(up.Fluent("track_length", up.RealType(), trackpart=track_part_type),                          default_initial_value=up.Real(Fraction(0)))
    train_length = problem.add_fluent(up.Fluent("train_length", up.RealType(), train=arrival_train_type),                               default_initial_value=up.Real(Fraction(0)))
    # occupied_length = problem.add_fluent(up.Fluent("occupied_length", up.RealType(), trackpart=track_part_type),                        default_initial_value=up.Real(Fraction(0)))
    aside_distance = problem.add_fluent(up.Fluent("aside_distance", up.RealType(), train=arrival_train_type),                           default_initial_value=up.Real(Fraction(0)))
    astack_distance = problem.add_fluent(up.Fluent("astack_distance", up.RealType(), trackpart=track_part_type),                         default_initial_value=up.Real(Fraction(0)))
    bstack_distance = problem.add_fluent(up.Fluent("bstack_distance", up.RealType(), trackpart=track_part_type),                         default_initial_value=up.Real(Fraction(0)))
    allowed_to_move = problem.add_fluent(up.Fluent("allowed_to_move", up.BoolType(), train=arrival_train_type),                           default_initial_value=False)
    # add a fluent that counts the number of trains that are moving at the same time, to enforce that only one train can move at the same time
    concurrent_movements = problem.add_fluent(up.Fluent("concurrent_movements", up.IntType()), default_initial_value=up.Int(0))
    max_concurrent_movements = 1
    direction_train = problem.add_fluent(up.Fluent("direction_train", up.BoolType(), train=arrival_train_type), default_initial_value=False) # False means towards bside, True means towards aside
    
    available      = problem.add_fluent(up.Fluent("available",      up.BoolType(), unit=train_unit_type),                               default_initial_value=False)
    request_open   = problem.add_fluent(up.Fluent("request_open",   up.BoolType(), request=departure_request_type),                     default_initial_value=False)
    slot_open      = problem.add_fluent(up.Fluent("slot_open",      up.BoolType(), slot=request_slot_type),                             default_initial_value=False)
    slot_filled    = problem.add_fluent(up.Fluent("slot_filled",    up.BoolType(), slot=request_slot_type),                             default_initial_value=False)
    compatible     = problem.add_fluent(up.Fluent("compatible",     up.BoolType(), unit=train_unit_type, slot=request_slot_type),        default_initial_value=False)
    matched        = problem.add_fluent(up.Fluent("matched",        up.BoolType(), unit=train_unit_type, slot=request_slot_type),        default_initial_value=False)
    slot_for_request = problem.add_fluent(up.Fluent("slot_for_request", up.BoolType(), slot=request_slot_type, request=departure_request_type), default_initial_value=False)
    slot_before = problem.add_fluent(up.Fluent("slot_before", up.BoolType(), first=request_slot_type, second=request_slot_type), default_initial_value=False)
    unit_in_train = problem.add_fluent(up.Fluent("unit_in_train", up.BoolType(), unit=train_unit_type, train=arrival_train_type), default_initial_value=False)
    unit_before = problem.add_fluent(up.Fluent("unit_before", up.BoolType(), first=train_unit_type, second=train_unit_type), default_initial_value=False)
    coupling_allowed = problem.add_fluent(up.Fluent("coupling_allowed", up.BoolType(), trackpart=track_part_type), default_initial_value=False)
    coupling_track_for_request = problem.add_fluent(up.Fluent("coupling_track_for_request", up.BoolType(), request=departure_request_type, trackpart=track_part_type), default_initial_value=False)

    # Shunting units represent movable train compositions created by split/couple actions.
    active_su        = problem.add_fluent(up.Fluent("active_su", up.BoolType(), shunting_unit=shunting_unit_type), default_initial_value=False)
    contains_su      = problem.add_fluent(up.Fluent("contains_su", up.BoolType(), shunting_unit=shunting_unit_type, unit=train_unit_type), default_initial_value=False)
    at_su            = problem.add_fluent(up.Fluent("at_su", up.BoolType(), shunting_unit=shunting_unit_type, trackpart=track_part_type), default_initial_value=False)
    departed_su      = problem.add_fluent(up.Fluent("departed_su", up.BoolType(), shunting_unit=shunting_unit_type), default_initial_value=False)
    single_unit_su   = problem.add_fluent(up.Fluent("single_unit_su", up.BoolType(), shunting_unit=shunting_unit_type, unit=train_unit_type), default_initial_value=False)
    request_su_for_request = problem.add_fluent(up.Fluent("request_su_for_request", up.BoolType(), shunting_unit=shunting_unit_type, request=departure_request_type), default_initial_value=False)
    su_length        = problem.add_fluent(up.Fluent("su_length", up.RealType(), shunting_unit=shunting_unit_type), default_initial_value=up.Real(Fraction(0)))
    su_aside_distance = problem.add_fluent(up.Fluent("su_aside_distance", up.RealType(), shunting_unit=shunting_unit_type), default_initial_value=up.Real(Fraction(0)))
    allowed_to_move_su = problem.add_fluent(up.Fluent("allowed_to_move_su", up.BoolType(), shunting_unit=shunting_unit_type), default_initial_value=False)


    startMove = up.InstantaneousAction('start_move', t=arrival_train_type)
    startMove.add_precondition(up.Not(allowed_to_move(startMove.t)))
    startMove.add_precondition(up.Not(locked_train(startMove.t)))
    startMove.add_precondition(up.Not(departed(startMove.t)))
    startMove.add_precondition(up.Not(parked(startMove.t)))
    startMove.add_precondition(concurrent_movements < max_concurrent_movements)
    startMove.add_effect(allowed_to_move(startMove.t), True)
    startMove.add_effect(concurrent_movements, concurrent_movements + 1)
    problem.add_action(startMove)

    startMoveSu = up.InstantaneousAction('start_move_su', su=shunting_unit_type)
    startMoveSu.add_precondition(active_su(startMoveSu.su))
    startMoveSu.add_precondition(up.Not(allowed_to_move_su(startMoveSu.su)))
    startMoveSu.add_precondition(concurrent_movements < max_concurrent_movements)
    startMoveSu.add_effect(allowed_to_move_su(startMoveSu.su), True)
    startMoveSu.add_effect(concurrent_movements, concurrent_movements + 1)
    problem.add_action(startMoveSu)

    endMove = up.InstantaneousAction('end_move', t=arrival_train_type, l=track_part_type)
    endMove.add_precondition(allowed_to_move(endMove.t))
    endMove.add_precondition(at(endMove.t, endMove.l))
    endMove.add_precondition(parking_allowed(endMove.l))
    endMove.add_effect(allowed_to_move(endMove.t), False)
    endMove.add_effect(concurrent_movements, concurrent_movements - 1)
    problem.add_action(endMove)

    endMoveSu = up.InstantaneousAction('end_move_su', su=shunting_unit_type, l=track_part_type)
    endMoveSu.add_precondition(active_su(endMoveSu.su))
    endMoveSu.add_precondition(allowed_to_move_su(endMoveSu.su))
    endMoveSu.add_precondition(at_su(endMoveSu.su, endMoveSu.l))
    endMoveSu.add_precondition(parking_allowed(endMoveSu.l))
    endMoveSu.add_effect(allowed_to_move_su(endMoveSu.su), False)
    endMoveSu.add_effect(concurrent_movements, concurrent_movements - 1)
    problem.add_action(endMoveSu)

    move_aside_empty = up.InstantaneousAction('move_aside_empty', t=arrival_train_type, l_from=track_part_type, l_to=track_part_type)
    move_aside_empty.add_precondition(direction_train(move_aside_empty.t)) # only allow move aside if train is facing aside
    move_aside_empty.add_precondition(allowed_to_move(move_aside_empty.t))
    move_aside_empty.add_precondition(up.Not(locked_train(move_aside_empty.t)))
    move_aside_empty.add_precondition(at(move_aside_empty.t, move_aside_empty.l_from))
    move_aside_empty.add_precondition(connected_aside(move_aside_empty.l_from, move_aside_empty.l_to))
    move_aside_empty.add_precondition(aside_distance(move_aside_empty.t) <= astack_distance(move_aside_empty.l_from))
    move_aside_empty.add_precondition(up.Equals(number_of_trains_on_track(move_aside_empty.l_to), 0))
    move_aside_empty.add_precondition(train_length(move_aside_empty.t) <= track_length(move_aside_empty.l_to))

    move_aside_empty.add_effect(number_of_trains_on_track(move_aside_empty.l_from), number_of_trains_on_track(move_aside_empty.l_from) - 1)
    move_aside_empty.add_effect(number_of_trains_on_track(move_aside_empty.l_to), 1)
    move_aside_empty.add_effect(aside_distance(move_aside_empty.t), 0)
    move_aside_empty.add_effect(astack_distance(move_aside_empty.l_from), astack_distance(move_aside_empty.l_from) + train_length(move_aside_empty.t))
    move_aside_empty.add_effect(astack_distance(move_aside_empty.l_to), 0)
    move_aside_empty.add_effect(bstack_distance(move_aside_empty.l_to), train_length(move_aside_empty.t))
    move_aside_empty.add_effect(at(move_aside_empty.t, move_aside_empty.l_to), True)
    move_aside_empty.add_effect(at(move_aside_empty.t, move_aside_empty.l_from), False)
    problem.add_action(move_aside_empty)

    move_aside_empty_su = up.InstantaneousAction('move_aside_empty_su', su=shunting_unit_type, l_from=track_part_type, l_to=track_part_type)
    # Same transition as move_aside_empty, but for the current movable shunting composition.
    move_aside_empty_su.add_precondition(active_su(move_aside_empty_su.su))
    move_aside_empty_su.add_precondition(allowed_to_move_su(move_aside_empty_su.su))
    move_aside_empty_su.add_precondition(at_su(move_aside_empty_su.su, move_aside_empty_su.l_from))
    move_aside_empty_su.add_precondition(connected_aside(move_aside_empty_su.l_from, move_aside_empty_su.l_to))
    move_aside_empty_su.add_precondition(su_aside_distance(move_aside_empty_su.su) <= astack_distance(move_aside_empty_su.l_from))
    move_aside_empty_su.add_precondition(up.Equals(number_of_trains_on_track(move_aside_empty_su.l_to), 0))
    move_aside_empty_su.add_precondition(su_length(move_aside_empty_su.su) <= track_length(move_aside_empty_su.l_to))
    move_aside_empty_su.add_effect(number_of_trains_on_track(move_aside_empty_su.l_from), number_of_trains_on_track(move_aside_empty_su.l_from) - 1)
    move_aside_empty_su.add_effect(number_of_trains_on_track(move_aside_empty_su.l_to), 1)
    move_aside_empty_su.add_effect(su_aside_distance(move_aside_empty_su.su), 0)
    move_aside_empty_su.add_effect(astack_distance(move_aside_empty_su.l_from), astack_distance(move_aside_empty_su.l_from) + su_length(move_aside_empty_su.su))
    move_aside_empty_su.add_effect(astack_distance(move_aside_empty_su.l_to), 0)
    move_aside_empty_su.add_effect(bstack_distance(move_aside_empty_su.l_to), su_length(move_aside_empty_su.su))
    move_aside_empty_su.add_effect(at_su(move_aside_empty_su.su, move_aside_empty_su.l_to), True)
    move_aside_empty_su.add_effect(at_su(move_aside_empty_su.su, move_aside_empty_su.l_from), False)
    problem.add_action(move_aside_empty_su)

    move_aside_occupied = up.InstantaneousAction('move_aside_occupied', t=arrival_train_type, l_from=track_part_type, l_to=track_part_type)
    move_aside_occupied.add_precondition(direction_train(move_aside_occupied.t)) # only allow move aside if train is facing aside
    move_aside_occupied.add_precondition(allowed_to_move(move_aside_occupied.t))
    move_aside_occupied.add_precondition(up.Not(locked_train(move_aside_occupied.t)))
    move_aside_occupied.add_precondition(at(move_aside_occupied.t, move_aside_occupied.l_from))
    move_aside_occupied.add_precondition(connected_aside(move_aside_occupied.l_from, move_aside_occupied.l_to))
    move_aside_occupied.add_precondition(aside_distance(move_aside_occupied.t) <= astack_distance(move_aside_occupied.l_from))
    move_aside_occupied.add_precondition(number_of_trains_on_track(move_aside_occupied.l_to) > 0)
    move_aside_occupied.add_precondition(train_length(move_aside_occupied.t) <= track_length(move_aside_occupied.l_to) - bstack_distance(move_aside_occupied.l_to))

    move_aside_occupied.add_effect(number_of_trains_on_track(move_aside_occupied.l_from), number_of_trains_on_track(move_aside_occupied.l_from) - 1)
    move_aside_occupied.add_effect(number_of_trains_on_track(move_aside_occupied.l_to), number_of_trains_on_track(move_aside_occupied.l_to) + 1)
    move_aside_occupied.add_effect(aside_distance(move_aside_occupied.t), bstack_distance(move_aside_occupied.l_to))
    move_aside_occupied.add_effect(astack_distance(move_aside_occupied.l_from), astack_distance(move_aside_occupied.l_from) + train_length(move_aside_occupied.t))
    move_aside_occupied.add_effect(bstack_distance(move_aside_occupied.l_to), bstack_distance(move_aside_occupied.l_to) + train_length(move_aside_occupied.t))
    move_aside_occupied.add_effect(at(move_aside_occupied.t, move_aside_occupied.l_to), True)
    move_aside_occupied.add_effect(at(move_aside_occupied.t, move_aside_occupied.l_from), False)
    problem.add_action(move_aside_occupied)

    move_aside_occupied_su = up.InstantaneousAction('move_aside_occupied_su', su=shunting_unit_type, l_from=track_part_type, l_to=track_part_type)
    # Aside-side movement into an occupied track stacks the shunting unit behind existing content.
    move_aside_occupied_su.add_precondition(active_su(move_aside_occupied_su.su))
    move_aside_occupied_su.add_precondition(allowed_to_move_su(move_aside_occupied_su.su))
    move_aside_occupied_su.add_precondition(at_su(move_aside_occupied_su.su, move_aside_occupied_su.l_from))
    move_aside_occupied_su.add_precondition(connected_aside(move_aside_occupied_su.l_from, move_aside_occupied_su.l_to))
    move_aside_occupied_su.add_precondition(su_aside_distance(move_aside_occupied_su.su) <= astack_distance(move_aside_occupied_su.l_from))
    move_aside_occupied_su.add_precondition(number_of_trains_on_track(move_aside_occupied_su.l_to) > 0)
    move_aside_occupied_su.add_precondition(su_length(move_aside_occupied_su.su) <= track_length(move_aside_occupied_su.l_to) - bstack_distance(move_aside_occupied_su.l_to))
    move_aside_occupied_su.add_effect(number_of_trains_on_track(move_aside_occupied_su.l_from), number_of_trains_on_track(move_aside_occupied_su.l_from) - 1)
    move_aside_occupied_su.add_effect(number_of_trains_on_track(move_aside_occupied_su.l_to), number_of_trains_on_track(move_aside_occupied_su.l_to) + 1)
    move_aside_occupied_su.add_effect(su_aside_distance(move_aside_occupied_su.su), bstack_distance(move_aside_occupied_su.l_to))
    move_aside_occupied_su.add_effect(astack_distance(move_aside_occupied_su.l_from), astack_distance(move_aside_occupied_su.l_from) + su_length(move_aside_occupied_su.su))
    move_aside_occupied_su.add_effect(bstack_distance(move_aside_occupied_su.l_to), bstack_distance(move_aside_occupied_su.l_to) + su_length(move_aside_occupied_su.su))
    move_aside_occupied_su.add_effect(at_su(move_aside_occupied_su.su, move_aside_occupied_su.l_to), True)
    move_aside_occupied_su.add_effect(at_su(move_aside_occupied_su.su, move_aside_occupied_su.l_from), False)
    problem.add_action(move_aside_occupied_su)

    move_bside_empty = up.InstantaneousAction('move_bside_empty', t=arrival_train_type, l_from=track_part_type, l_to=track_part_type)
    move_bside_empty.add_precondition(up.Not(direction_train(move_bside_empty.t))) # only allow move bside if train is facing bside
    move_bside_empty.add_precondition(allowed_to_move(move_bside_empty.t))
    move_bside_empty.add_precondition(up.Not(locked_train(move_bside_empty.t)))
    move_bside_empty.add_precondition(at(move_bside_empty.t, move_bside_empty.l_from))
    move_bside_empty.add_precondition(connected_bside(move_bside_empty.l_from, move_bside_empty.l_to))
    move_bside_empty.add_precondition(aside_distance(move_bside_empty.t) >= bstack_distance(move_bside_empty.l_from) - train_length(move_bside_empty.t))
    move_bside_empty.add_precondition(up.Equals(number_of_trains_on_track(move_bside_empty.l_to), 0))
    move_bside_empty.add_precondition(train_length(move_bside_empty.t) <= track_length(move_bside_empty.l_to))
    
    move_bside_empty.add_effect(number_of_trains_on_track(move_bside_empty.l_from), number_of_trains_on_track(move_bside_empty.l_from) - 1)
    move_bside_empty.add_effect(number_of_trains_on_track(move_bside_empty.l_to), 1)
    move_bside_empty.add_effect(aside_distance(move_bside_empty.t), 0)
    move_bside_empty.add_effect(bstack_distance(move_bside_empty.l_from), bstack_distance(move_bside_empty.l_from) - train_length(move_bside_empty.t))
    move_bside_empty.add_effect(astack_distance(move_bside_empty.l_to), 0)
    move_bside_empty.add_effect(bstack_distance(move_bside_empty.l_to), train_length(move_bside_empty.t))
    move_bside_empty.add_effect(at(move_bside_empty.t, move_bside_empty.l_to), True)
    move_bside_empty.add_effect(at(move_bside_empty.t, move_bside_empty.l_from), False)
    problem.add_action(move_bside_empty)

    move_bside_empty_su = up.InstantaneousAction('move_bside_empty_su', su=shunting_unit_type, l_from=track_part_type, l_to=track_part_type)
    move_bside_empty_su.add_precondition(active_su(move_bside_empty_su.su))
    move_bside_empty_su.add_precondition(allowed_to_move_su(move_bside_empty_su.su))
    move_bside_empty_su.add_precondition(at_su(move_bside_empty_su.su, move_bside_empty_su.l_from))
    move_bside_empty_su.add_precondition(connected_bside(move_bside_empty_su.l_from, move_bside_empty_su.l_to))
    move_bside_empty_su.add_precondition(su_aside_distance(move_bside_empty_su.su) >= bstack_distance(move_bside_empty_su.l_from) - su_length(move_bside_empty_su.su))
    move_bside_empty_su.add_precondition(up.Equals(number_of_trains_on_track(move_bside_empty_su.l_to), 0))
    move_bside_empty_su.add_precondition(su_length(move_bside_empty_su.su) <= track_length(move_bside_empty_su.l_to))
    move_bside_empty_su.add_effect(number_of_trains_on_track(move_bside_empty_su.l_from), number_of_trains_on_track(move_bside_empty_su.l_from) - 1)
    move_bside_empty_su.add_effect(number_of_trains_on_track(move_bside_empty_su.l_to), 1)
    move_bside_empty_su.add_effect(su_aside_distance(move_bside_empty_su.su), 0)
    move_bside_empty_su.add_effect(bstack_distance(move_bside_empty_su.l_from), bstack_distance(move_bside_empty_su.l_from) - su_length(move_bside_empty_su.su))
    move_bside_empty_su.add_effect(astack_distance(move_bside_empty_su.l_to), 0)
    move_bside_empty_su.add_effect(bstack_distance(move_bside_empty_su.l_to), su_length(move_bside_empty_su.su))
    move_bside_empty_su.add_effect(at_su(move_bside_empty_su.su, move_bside_empty_su.l_to), True)
    move_bside_empty_su.add_effect(at_su(move_bside_empty_su.su, move_bside_empty_su.l_from), False)
    problem.add_action(move_bside_empty_su)

    move_bside_occupied = up.InstantaneousAction('move_bside_occupied', t=arrival_train_type, l_from=track_part_type, l_to=track_part_type)
    move_bside_occupied.add_precondition(up.Not(direction_train(move_bside_occupied.t))) # only allow move bside if train is facing bside
    move_bside_occupied.add_precondition(allowed_to_move(move_bside_occupied.t))
    move_bside_occupied.add_precondition(up.Not(locked_train(move_bside_occupied.t)))
    move_bside_occupied.add_precondition(at(move_bside_occupied.t, move_bside_occupied.l_from))
    move_bside_occupied.add_precondition(connected_bside(move_bside_occupied.l_from, move_bside_occupied.l_to))
    move_bside_occupied.add_precondition(aside_distance(move_bside_occupied.t) >= bstack_distance(move_bside_occupied.l_from) - train_length(move_bside_occupied.t))
    move_bside_occupied.add_precondition(number_of_trains_on_track(move_bside_occupied.l_to) > 0)
    move_bside_occupied.add_precondition(train_length(move_bside_occupied.t) <= astack_distance(move_bside_occupied.l_to))
    
    move_bside_occupied.add_effect(number_of_trains_on_track(move_bside_occupied.l_from), number_of_trains_on_track(move_bside_occupied.l_from) - 1)
    move_bside_occupied.add_effect(number_of_trains_on_track(move_bside_occupied.l_to), number_of_trains_on_track(move_bside_occupied.l_to) + 1)
    move_bside_occupied.add_effect(aside_distance(move_bside_occupied.t), (astack_distance(move_bside_occupied.l_to) - train_length(move_bside_occupied.t)))
    move_bside_occupied.add_effect(bstack_distance(move_bside_occupied.l_from), bstack_distance(move_bside_occupied.l_from) - train_length(move_bside_occupied.t))
    move_bside_occupied.add_effect(astack_distance(move_bside_occupied.l_to), astack_distance(move_bside_occupied.l_to) - train_length(move_bside_occupied.t))
    move_bside_occupied.add_effect(at(move_bside_occupied.t, move_bside_occupied.l_to), True)
    move_bside_occupied.add_effect(at(move_bside_occupied.t, move_bside_occupied.l_from), False)
    problem.add_action(move_bside_occupied)

    move_bside_occupied_su = up.InstantaneousAction('move_bside_occupied_su', su=shunting_unit_type, l_from=track_part_type, l_to=track_part_type)
    # B-side movement into an occupied track places the shunting unit at the open aside-side gap.
    move_bside_occupied_su.add_precondition(active_su(move_bside_occupied_su.su))
    move_bside_occupied_su.add_precondition(allowed_to_move_su(move_bside_occupied_su.su))
    move_bside_occupied_su.add_precondition(at_su(move_bside_occupied_su.su, move_bside_occupied_su.l_from))
    move_bside_occupied_su.add_precondition(connected_bside(move_bside_occupied_su.l_from, move_bside_occupied_su.l_to))
    move_bside_occupied_su.add_precondition(su_aside_distance(move_bside_occupied_su.su) >= bstack_distance(move_bside_occupied_su.l_from) - su_length(move_bside_occupied_su.su))
    move_bside_occupied_su.add_precondition(number_of_trains_on_track(move_bside_occupied_su.l_to) > 0)
    move_bside_occupied_su.add_precondition(su_length(move_bside_occupied_su.su) <= astack_distance(move_bside_occupied_su.l_to))
    move_bside_occupied_su.add_effect(number_of_trains_on_track(move_bside_occupied_su.l_from), number_of_trains_on_track(move_bside_occupied_su.l_from) - 1)
    move_bside_occupied_su.add_effect(number_of_trains_on_track(move_bside_occupied_su.l_to), number_of_trains_on_track(move_bside_occupied_su.l_to) + 1)
    move_bside_occupied_su.add_effect(su_aside_distance(move_bside_occupied_su.su), astack_distance(move_bside_occupied_su.l_to) - su_length(move_bside_occupied_su.su))
    move_bside_occupied_su.add_effect(bstack_distance(move_bside_occupied_su.l_from), bstack_distance(move_bside_occupied_su.l_from) - su_length(move_bside_occupied_su.su))
    move_bside_occupied_su.add_effect(astack_distance(move_bside_occupied_su.l_to), astack_distance(move_bside_occupied_su.l_to) - su_length(move_bside_occupied_su.su))
    move_bside_occupied_su.add_effect(at_su(move_bside_occupied_su.su, move_bside_occupied_su.l_to), True)
    move_bside_occupied_su.add_effect(at_su(move_bside_occupied_su.su, move_bside_occupied_su.l_from), False)
    problem.add_action(move_bside_occupied_su)

    depart_aside = up.InstantaneousAction('depart_aside', t=arrival_train_type, l=track_part_type)
    depart_aside.add_precondition(up.Not(locked_train(depart_aside.t)))
    depart_aside.add_precondition(direction_train(depart_aside.t)) # only allow depart aside if train is facing aside
    depart_aside.add_precondition(allowed_to_move(depart_aside.t))
    depart_aside.add_precondition(at(depart_aside.t, depart_aside.l))
    depart_aside.add_precondition(departure_exit_a(depart_aside.l))
    depart_aside.add_precondition(aside_distance(depart_aside.t) <= astack_distance(depart_aside.l))
    depart_aside.add_effect(at(depart_aside.t, depart_aside.l), False)
    depart_aside.add_effect(departed(depart_aside.t), True)
    depart_aside.add_effect(num_of_departed_trains(), num_of_departed_trains() + 1)
    depart_aside.add_effect(number_of_trains_on_track(depart_aside.l), number_of_trains_on_track(depart_aside.l) - 1)
    depart_aside.add_effect(aside_distance(depart_aside.t), 0)
    depart_aside.add_effect(astack_distance(depart_aside.l), astack_distance(depart_aside.l) + train_length(depart_aside.t))
    depart_aside.add_effect(concurrent_movements, concurrent_movements - 1)
    depart_aside.add_effect(allowed_to_move(depart_aside.t), False)
    problem.add_action(depart_aside)

    depart_bside = up.InstantaneousAction('depart_bside', t=arrival_train_type, l=track_part_type)
    depart_bside.add_precondition(up.Not(locked_train(depart_bside.t)))
    depart_bside.add_precondition(up.Not(direction_train(depart_bside.t))) # only allow depart bside if train is facing bside
    depart_bside.add_precondition(allowed_to_move(depart_bside.t))
    depart_bside.add_precondition(at(depart_bside.t, depart_bside.l))
    depart_bside.add_precondition(departure_exit_b(depart_bside.l))
    depart_bside.add_precondition(aside_distance(depart_bside.t) >= bstack_distance(depart_bside.l) - train_length(depart_bside.t))
    depart_bside.add_effect(at(depart_bside.t, depart_bside.l), False)
    depart_bside.add_effect(departed(depart_bside.t), True)
    depart_bside.add_effect(num_of_departed_trains(), num_of_departed_trains() + 1)
    depart_bside.add_effect(number_of_trains_on_track(depart_bside.l), number_of_trains_on_track(depart_bside.l) - 1)
    depart_bside.add_effect(aside_distance(depart_bside.t), 0)
    depart_bside.add_effect(bstack_distance(depart_bside.l), bstack_distance(depart_bside.l) - train_length(depart_bside.t))
    depart_bside.add_effect(concurrent_movements, concurrent_movements - 1)
    depart_bside.add_effect(allowed_to_move(depart_bside.t), False)
    problem.add_action(depart_bside)

    depart_aside_su = up.InstantaneousAction('depart_aside_su', su=shunting_unit_type, l=track_part_type)
    # Departure for an assembled shunting unit proves it can move as a physical composition.
    depart_aside_su.add_precondition(active_su(depart_aside_su.su))
    depart_aside_su.add_precondition(allowed_to_move_su(depart_aside_su.su))
    depart_aside_su.add_precondition(at_su(depart_aside_su.su, depart_aside_su.l))
    depart_aside_su.add_precondition(departure_exit_a(depart_aside_su.l))
    depart_aside_su.add_precondition(su_aside_distance(depart_aside_su.su) <= astack_distance(depart_aside_su.l))
    depart_aside_su.add_effect(active_su(depart_aside_su.su), False)
    depart_aside_su.add_effect(at_su(depart_aside_su.su, depart_aside_su.l), False)
    depart_aside_su.add_effect(departed_su(depart_aside_su.su), True)
    depart_aside_su.add_effect(number_of_trains_on_track(depart_aside_su.l), number_of_trains_on_track(depart_aside_su.l) - 1)
    depart_aside_su.add_effect(su_aside_distance(depart_aside_su.su), 0)
    depart_aside_su.add_effect(astack_distance(depart_aside_su.l), astack_distance(depart_aside_su.l) + su_length(depart_aside_su.su))
    depart_aside_su.add_effect(concurrent_movements, concurrent_movements - 1)
    depart_aside_su.add_effect(allowed_to_move_su(depart_aside_su.su), False)
    problem.add_action(depart_aside_su)

    depart_bside_su = up.InstantaneousAction('depart_bside_su', su=shunting_unit_type, l=track_part_type)
    # B-side departure removes the assembled shunting unit from the back of the stack.
    depart_bside_su.add_precondition(active_su(depart_bside_su.su))
    depart_bside_su.add_precondition(allowed_to_move_su(depart_bside_su.su))
    depart_bside_su.add_precondition(at_su(depart_bside_su.su, depart_bside_su.l))
    depart_bside_su.add_precondition(departure_exit_b(depart_bside_su.l))
    depart_bside_su.add_precondition(su_aside_distance(depart_bside_su.su) >= bstack_distance(depart_bside_su.l) - su_length(depart_bside_su.su))
    depart_bside_su.add_effect(active_su(depart_bside_su.su), False)
    depart_bside_su.add_effect(at_su(depart_bside_su.su, depart_bside_su.l), False)
    depart_bside_su.add_effect(departed_su(depart_bside_su.su), True)
    depart_bside_su.add_effect(number_of_trains_on_track(depart_bside_su.l), number_of_trains_on_track(depart_bside_su.l) - 1)
    depart_bside_su.add_effect(su_aside_distance(depart_bside_su.su), 0)
    depart_bside_su.add_effect(bstack_distance(depart_bside_su.l), bstack_distance(depart_bside_su.l) - su_length(depart_bside_su.su))
    depart_bside_su.add_effect(concurrent_movements, concurrent_movements - 1)
    depart_bside_su.add_effect(allowed_to_move_su(depart_bside_su.su), False)
    problem.add_action(depart_bside_su)

    park = up.InstantaneousAction('park', t=arrival_train_type, l=track_part_type)
    park.add_precondition(up.Not(locked_train(park.t)))
    park.add_precondition(allowed_to_move(park.t))
    park.add_precondition(at(park.t, park.l))
    park.add_precondition(parking_allowed(park.l))
    park.add_effect(parked(park.t), True)
    park.add_effect(number_of_parked_trains(park.l), number_of_parked_trains(park.l) + 1)
    park.add_effect(allowed_to_move(park.t), False)
    park.add_effect(concurrent_movements, concurrent_movements - 1)
    problem.add_action(park)

    turn_a_side = up.InstantaneousAction('turn_a_side', t=arrival_train_type, l=track_part_type)
    turn_a_side.add_precondition(allowed_to_move(turn_a_side.t))
    turn_a_side.add_precondition(up.Not(direction_train(turn_a_side.t))) # only allow turn if train is currently facing bside, so it will end up facing aside
    turn_a_side.add_precondition(at(turn_a_side.t, turn_a_side.l))
    turn_a_side.add_precondition(turning_allowed(turn_a_side.l))
    turn_a_side.add_effect(direction_train(turn_a_side.t), True)
    problem.add_action(turn_a_side)

    turn_b_side = up.InstantaneousAction('turn_b_side', t=arrival_train_type, l=track_part_type)
    turn_b_side.add_precondition(allowed_to_move(turn_b_side.t))
    turn_b_side.add_precondition(direction_train(turn_b_side.t)) # only allow turn if train is currently facing aside, so it will end up facing bside
    turn_b_side.add_precondition(at(turn_b_side.t, turn_b_side.l))
    turn_b_side.add_precondition(turning_allowed(turn_b_side.l))
    turn_b_side.add_effect(direction_train(turn_b_side.t), False)
    problem.add_action(turn_b_side)

    part_of_composition = problem.add_fluent(up.Fluent("part_of_composition", up.BoolType(), unit=train_unit_type, composition=arrival_composition_type), default_initial_value=False)
    composition_needs_uncoupling = problem.add_fluent(up.Fluent("composition_needs_uncoupling", up.BoolType(), composition=arrival_composition_type), default_initial_value=False)
    # Units in a multi-unit arrival must first be released from their composition.
    uncouple = up.InstantaneousAction("uncouple", unit=train_unit_type, composition=arrival_composition_type)
    # Preconditions: the unit belongs to a composition that still needs splitting.
    uncouple.add_precondition(part_of_composition(uncouple.unit, uncouple.composition))
    uncouple.add_precondition(composition_needs_uncoupling(uncouple.composition))
    # Effects: the unit becomes independently matchable and is removed from that composition.
    uncouple.add_effect(available(uncouple.unit), True)
    uncouple.add_effect(part_of_composition(uncouple.unit, uncouple.composition), False)
    problem.add_action(uncouple)

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
    # Splitting turns one active two-unit composition into two active single-unit shunting units.
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
    split_two_unit_su.add_effect(at_su(split_two_unit_su.parent_su, split_two_unit_su.track), False)
    split_two_unit_su.add_effect(at_su(split_two_unit_su.left_su, split_two_unit_su.track), True)
    split_two_unit_su.add_effect(at_su(split_two_unit_su.right_su, split_two_unit_su.track), True)
    # Preserve the physical stack after splitting: the left unit keeps the parent's aside position,
    # while the right unit starts immediately behind it and can move from the b-side first.
    split_two_unit_su.add_effect(su_aside_distance(split_two_unit_su.left_su), su_aside_distance(split_two_unit_su.parent_su))
    split_two_unit_su.add_effect(su_aside_distance(split_two_unit_su.right_su), su_aside_distance(split_two_unit_su.parent_su) + su_length(split_two_unit_su.left_su))
    split_two_unit_su.add_effect(number_of_trains_on_track(split_two_unit_su.track), number_of_trains_on_track(split_two_unit_su.track) + 1)
    split_two_unit_su.add_effect(available(split_two_unit_su.unit_a), True)
    split_two_unit_su.add_effect(available(split_two_unit_su.unit_b), True)
    split_two_unit_su.add_effect(part_of_composition(split_two_unit_su.unit_a, split_two_unit_su.composition), False)
    split_two_unit_su.add_effect(part_of_composition(split_two_unit_su.unit_b, split_two_unit_su.composition), False)
    problem.add_action(split_two_unit_su)

    # Explicit coupling goals track both slot completion and physical assembly.
    slot_coupled = problem.add_fluent(up.Fluent("slot_coupled", up.BoolType(), slot=request_slot_type), default_initial_value=False)
    coupled_to_request = problem.add_fluent(up.Fluent("coupled_to_request", up.BoolType(), unit=train_unit_type, request=departure_request_type), default_initial_value=False)
    physically_coupled = problem.add_fluent(up.Fluent("physically_coupled", up.BoolType(), first=train_unit_type, second=train_unit_type), default_initial_value=False)
    request_assembled = problem.add_fluent(up.Fluent("request_assembled", up.BoolType(), request=departure_request_type), default_initial_value=False)

    couple_two_units = up.InstantaneousAction(
        "couple_two_units",
        unit_a=train_unit_type,
        unit_b=train_unit_type,
        train_a=arrival_train_type,
        train_b=arrival_train_type,
        track=track_part_type,
        slot_a=request_slot_type,
        slot_b=request_slot_type,
        request=departure_request_type,
    )
    # The matched units must be the two ordered slots of the same outgoing request.
    couple_two_units.add_precondition(matched(couple_two_units.unit_a, couple_two_units.slot_a))
    couple_two_units.add_precondition(matched(couple_two_units.unit_b, couple_two_units.slot_b))
    couple_two_units.add_precondition(slot_for_request(couple_two_units.slot_a, couple_two_units.request))
    couple_two_units.add_precondition(slot_for_request(couple_two_units.slot_b, couple_two_units.request))
    couple_two_units.add_precondition(slot_before(couple_two_units.slot_a, couple_two_units.slot_b))
    couple_two_units.add_precondition(up.Not(up.Equals(couple_two_units.unit_a, couple_two_units.unit_b)))
    # Physical checks: each unit belongs to its train, both trains share a valid track, and order matches the slots.
    couple_two_units.add_precondition(unit_in_train(couple_two_units.unit_a, couple_two_units.train_a))
    couple_two_units.add_precondition(unit_in_train(couple_two_units.unit_b, couple_two_units.train_b))
    couple_two_units.add_precondition(at(couple_two_units.train_a, couple_two_units.track))
    couple_two_units.add_precondition(at(couple_two_units.train_b, couple_two_units.track))
    couple_two_units.add_precondition(coupling_allowed(couple_two_units.track))
    couple_two_units.add_precondition(coupling_track_for_request(couple_two_units.request, couple_two_units.track))
    couple_two_units.add_precondition(aside_distance(couple_two_units.train_a) < aside_distance(couple_two_units.train_b))
    # Completing this action proves the ordered request is physically assembled.
    couple_two_units.add_effect(slot_coupled(couple_two_units.slot_a), True)
    couple_two_units.add_effect(slot_coupled(couple_two_units.slot_b), True)
    couple_two_units.add_effect(coupled_to_request(couple_two_units.unit_a, couple_two_units.request), True)
    couple_two_units.add_effect(coupled_to_request(couple_two_units.unit_b, couple_two_units.request), True)
    couple_two_units.add_effect(physically_coupled(couple_two_units.unit_a, couple_two_units.unit_b), True)
    couple_two_units.add_effect(request_assembled(couple_two_units.request), True)
    problem.add_action(couple_two_units)

    couple_two_units_same_train = up.InstantaneousAction(
        "couple_two_units_same_train",
        unit_a=train_unit_type,
        unit_b=train_unit_type,
        train=arrival_train_type,
        track=track_part_type,
        slot_a=request_slot_type,
        slot_b=request_slot_type,
        request=departure_request_type,
    )
    couple_two_units_same_train.add_precondition(matched(couple_two_units_same_train.unit_a, couple_two_units_same_train.slot_a))
    couple_two_units_same_train.add_precondition(matched(couple_two_units_same_train.unit_b, couple_two_units_same_train.slot_b))
    couple_two_units_same_train.add_precondition(slot_for_request(couple_two_units_same_train.slot_a, couple_two_units_same_train.request))
    couple_two_units_same_train.add_precondition(slot_for_request(couple_two_units_same_train.slot_b, couple_two_units_same_train.request))
    couple_two_units_same_train.add_precondition(slot_before(couple_two_units_same_train.slot_a, couple_two_units_same_train.slot_b))
    couple_two_units_same_train.add_precondition(up.Not(up.Equals(couple_two_units_same_train.unit_a, couple_two_units_same_train.unit_b)))
    couple_two_units_same_train.add_precondition(unit_in_train(couple_two_units_same_train.unit_a, couple_two_units_same_train.train))
    couple_two_units_same_train.add_precondition(unit_in_train(couple_two_units_same_train.unit_b, couple_two_units_same_train.train))
    couple_two_units_same_train.add_precondition(at(couple_two_units_same_train.train, couple_two_units_same_train.track))
    couple_two_units_same_train.add_precondition(coupling_allowed(couple_two_units_same_train.track))
    couple_two_units_same_train.add_precondition(coupling_track_for_request(couple_two_units_same_train.request, couple_two_units_same_train.track))
    couple_two_units_same_train.add_precondition(unit_before(couple_two_units_same_train.unit_a, couple_two_units_same_train.unit_b))
    # Same-train coupling has the same completion effects as separate-train coupling.
    couple_two_units_same_train.add_effect(slot_coupled(couple_two_units_same_train.slot_a), True)
    couple_two_units_same_train.add_effect(slot_coupled(couple_two_units_same_train.slot_b), True)
    couple_two_units_same_train.add_effect(coupled_to_request(couple_two_units_same_train.unit_a, couple_two_units_same_train.request), True)
    couple_two_units_same_train.add_effect(coupled_to_request(couple_two_units_same_train.unit_b, couple_two_units_same_train.request), True)
    couple_two_units_same_train.add_effect(physically_coupled(couple_two_units_same_train.unit_a, couple_two_units_same_train.unit_b), True)
    couple_two_units_same_train.add_effect(request_assembled(couple_two_units_same_train.request), True)
    problem.add_action(couple_two_units_same_train)

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
    # Coupling consumes two active shunting units and activates the assembled request unit.
    couple_two_sus.add_precondition(active_su(couple_two_sus.su_a))
    couple_two_sus.add_precondition(active_su(couple_two_sus.su_b))
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
    # The two shunting units must be adjacent in the same order as the departure request slots.
    couple_two_sus.add_precondition(up.Equals(su_aside_distance(couple_two_sus.su_b), su_aside_distance(couple_two_sus.su_a) + su_length(couple_two_sus.su_a)))
    couple_two_sus.add_precondition(matched(couple_two_sus.unit_a, couple_two_sus.slot_a))
    couple_two_sus.add_precondition(matched(couple_two_sus.unit_b, couple_two_sus.slot_b))
    couple_two_sus.add_precondition(slot_for_request(couple_two_sus.slot_a, couple_two_sus.request))
    couple_two_sus.add_precondition(slot_for_request(couple_two_sus.slot_b, couple_two_sus.request))
    couple_two_sus.add_precondition(slot_before(couple_two_sus.slot_a, couple_two_sus.slot_b))
    couple_two_sus.add_effect(active_su(couple_two_sus.su_a), False)
    couple_two_sus.add_effect(active_su(couple_two_sus.su_b), False)
    couple_two_sus.add_effect(active_su(couple_two_sus.su_result), True)
    couple_two_sus.add_effect(at_su(couple_two_sus.su_a, couple_two_sus.track), False)
    couple_two_sus.add_effect(at_su(couple_two_sus.su_b, couple_two_sus.track), False)
    couple_two_sus.add_effect(at_su(couple_two_sus.su_result, couple_two_sus.track), True)
    # The assembled shunting unit inherits the front unit's position and combined length.
    couple_two_sus.add_effect(su_aside_distance(couple_two_sus.su_result), su_aside_distance(couple_two_sus.su_a))
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
    problem.add_action(couple_two_sus)

    # Matching assigns exactly one compatible available unit to an open request slot.
    match = up.InstantaneousAction("match", unit=train_unit_type, slot=request_slot_type)
    # Preconditions: the unit is unused, the slot is empty, and the unit type fits the slot.
    match.add_precondition(available(match.unit))
    match.add_precondition(slot_open(match.slot))
    match.add_precondition(compatible(match.unit, match.slot))
    # Effects: record the assignment, close the slot, and prevent reusing the unit.
    match.add_effect(matched(match.unit, match.slot), True)
    match.add_effect(slot_filled(match.slot), True)
    match.add_effect(available(match.unit), False)
    match.add_effect(slot_open(match.slot), False)
    problem.add_action(match)
    

    # Pre-compute connectivity and entry distances from JSON (no UP objects yet)
    adjacency = _build_adjacency(location_object)
    exit_ids_a, exit_ids_b = _departure_exit_ids(scenario_object, location_object)
    exit_ids = exit_ids_a.union(exit_ids_b)
    bfs_dist = _bfs_from(adjacency, exit_ids)

    # Find ids of all tracks where parking is allowed
    parking_ids = {tp["id"] for tp in location_object["trackParts"] if tp.get("parkingAllowed")}
    # Create sorted dictionary from all different bfs distances to the id's of parking tracks with those distances
    parking_bfs_values = sorted({bfs_dist[pid] for pid in parking_ids if pid in bfs_dist})
    # Normalize the distances; all closest parking tracks get 1, those after get 2 etc.
    bfs_to_entry_dist = {d: i + 1 for i, d in enumerate(parking_bfs_values)}

    inbound_trains = scenario_object.get("in", {}).get("trains", [])
    in_standing_trains = scenario_object.get("inStanding", {}).get("trains", [])
    out_standing_trains = scenario_object.get("outStanding", {}).get("trainRequests", [])
    out_requests = scenario_object.get("out", {}).get("trainRequests", [])
    # train_to_rank = _compute_departure_ranks(inbound_trains)
    track_occupancies = {}
    train_initial_aside = {}
    track_train_counts = {}
    arrival_train_objs = []
    train_obj_by_key = {}
    coupling_candidate_track_ids = set()

    # Add track part objects
    id_to_track_part = {}
    switch_like_track_ids = {tp["id"] for tp in location_object["trackParts"] if _is_switch_like_track_part(tp)}
    for track_part in location_object["trackParts"]:
        if track_part["id"] in switch_like_track_ids:
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
            coupling_candidate_track_ids.add(track_part["id"])
            tp_id = track_part["id"]
            if tp_id in bfs_dist and bfs_dist[tp_id] in bfs_to_entry_dist:
                problem.set_initial_value(entry_distance(obj), up.Int(bfs_to_entry_dist[bfs_dist[tp_id]]))
        problem.set_initial_value(track_length(obj), up.Real(Fraction(str(track_part.get("length", 100.0)))))

        # Use a very large number to indicate effectively infinite capacity for non-parking tracks, so that they can be used for temporary movements
        if not track_part.get("parkingAllowed", False):
            problem.set_initial_value(track_length(obj), up.Real(Fraction(10**9)))

    # Set connectivity (each undirected edge -> two directed facts)
    connected_pairs = set()
    for track_part in location_object["trackParts"]:
        src_id = track_part["id"]
        if src_id not in id_to_track_part:
            continue
        for nb_id in track_part.get("aSide", []):
            if nb_id in id_to_track_part:
                pair = tuple(sorted([src_id, nb_id]))
                # problem.set_initial_value(connected(id_to_track_part[src_id], id_to_track_part[nb_id]), True)
                # problem.set_initial_value(connected(id_to_track_part[nb_id], id_to_track_part[src_id]), True)
                problem.set_initial_value(connected_aside(id_to_track_part[src_id], id_to_track_part[nb_id]), True)
                if pair not in connected_pairs:
                    connected_pairs.add(pair)

        for nb_id in track_part.get("bSide", []):
            if nb_id in id_to_track_part:
                pair = tuple(sorted([src_id, nb_id]))
                # problem.set_initial_value(connected(id_to_track_part[src_id], id_to_track_part[nb_id]), True)
                # problem.set_initial_value(connected(id_to_track_part[nb_id], id_to_track_part[src_id]), True)
                problem.set_initial_value(connected_bside(id_to_track_part[src_id], id_to_track_part[nb_id]), True)
                if pair not in connected_pairs:
                    connected_pairs.add(pair)

    _reconnect_switch_like_components(location_object, switch_like_track_ids, id_to_track_part, problem, connected_pairs, connected_aside, connected_bside)


    id_to_unit = {}
    unit_type_by_id = {}

    # Add inbound trains
    for index, train in enumerate(inbound_trains):
        arrival_train = problem.add_object(_train_object_name("in", index, train), arrival_train_type)
        train_obj_by_key[("in", index)] = arrival_train
        arrival_train_objs.append(arrival_train)
        problem.set_initial_value(arrival(arrival_train), up.Int(int(train["arrival"])))
        initial_track_id = _train_initial_track_id(train, ["entryTrackPart", "firstParkingTrackPart"])
        problem.set_initial_value(at(arrival_train, id_to_track_part[initial_track_id]), True)
        # problem.set_initial_value(departure_rank(arrival_train), up.Int(train_to_rank[train["id"]]))
        train_total_length = _train_total_length(train)
        problem.set_initial_value(train_length(arrival_train), up.Real(train_total_length))
        previous_length_on_track = track_occupancies.get(initial_track_id, Fraction(0))
        problem.set_initial_value(aside_distance(arrival_train), up.Real(previous_length_on_track))
        train_initial_aside[_train_object_name("in", index, train)] = previous_length_on_track
        track_occupancies[initial_track_id] = track_occupancies.get(initial_track_id, Fraction(0)) + train_total_length
        track_train_counts[initial_track_id] = track_train_counts.get(initial_track_id, 0) + 1
        problem.set_initial_value(direction_train(arrival_train), determine_initial_direction(train, location_object, initial_track_id))

    for index, train in enumerate(in_standing_trains):
        standing_train = problem.add_object(_train_object_name("inStanding", index, train), arrival_train_type)
        train_obj_by_key[("inStanding", index)] = standing_train
        arrival_train_objs.append(standing_train)
        initial_track_id = _train_initial_track_id(train, ["firstParkingTrackPart", "entryTrackPart"])
        problem.set_initial_value(arrival(standing_train), up.Int(int(train.get("arrival", 0))))
        if initial_track_id is not None:
            problem.set_initial_value(at(standing_train, id_to_track_part[initial_track_id]), True)
            train_total_length = _train_total_length(train)
            problem.set_initial_value(train_length(standing_train), up.Real(train_total_length))
            previous_length_on_track = track_occupancies.get(initial_track_id, Fraction(0))
            problem.set_initial_value(aside_distance(standing_train), up.Real(previous_length_on_track))
            train_initial_aside[_train_object_name("inStanding", index, train)] = previous_length_on_track
            track_occupancies[initial_track_id] = track_occupancies.get(initial_track_id, Fraction(0)) + train_total_length
            track_train_counts[initial_track_id] = track_train_counts.get(initial_track_id, 0) + 1

    # Each outstanding request contributes one required parked train on its target track.
    required_parked_per_track = {}
    for request in out_standing_trains:
        track_id = request.get("lastParkingTrackPart")
        if track_id in id_to_track_part:
            required_parked_per_track[track_id] = required_parked_per_track.get(track_id, 0) + 1

    for track_id, required_count in required_parked_per_track.items():
        problem.add_goal(up.Equals(number_of_parked_trains(id_to_track_part[track_id]), up.Int(required_count)))

    # Add outbound train requests: these trains must be assembled (contain all units) and depart.
    # Add a goal stating that the number of departed trains must be equal to out_requests
    problem.add_goal(up.Equals(num_of_departed_trains(), up.Int(len(out_requests))))

    for track_id, occupied_length_value in track_occupancies.items():
        track_obj = id_to_track_part[track_id]
        # problem.set_initial_value(free(track_obj), False)
        problem.set_initial_value(astack_distance(track_obj), up.Real(Fraction(0)))
        problem.set_initial_value(bstack_distance(track_obj), up.Real(occupied_length_value))
        problem.set_initial_value(number_of_trains_on_track(track_obj), up.Int(track_train_counts.get(track_id, 0)))

    for source, index, train in all_trains_with_source(scenario_object):
        # Matching-only runs still need physical train objects for coupling checks.
        preferred_track_keys = ["firstParkingTrackPart", "entryTrackPart"] if source == "inStanding" else ["entryTrackPart", "firstParkingTrackPart"]
        initial_track_id = _train_initial_track_id(train, preferred_track_keys)
        physical_train = train_obj_by_key.get((source, index))
        if physical_train is None:
            # Parking mode creates trains earlier; matching-only mode creates them here.
            physical_train = problem.add_object(_train_object_name(source, index, train), arrival_train_type)
            train_obj_by_key[(source, index)] = physical_train
            problem.set_initial_value(arrival(physical_train), up.Int(int(train.get("arrival", 0))))
            if initial_track_id in id_to_track_part:
                train_total_length = _train_total_length(train)
                previous_length_on_track = track_occupancies.get(initial_track_id, Fraction(0))
                problem.set_initial_value(at(physical_train, id_to_track_part[initial_track_id]), True)
                problem.set_initial_value(train_length(physical_train), up.Real(train_total_length))
                problem.set_initial_value(aside_distance(physical_train), up.Real(previous_length_on_track))
                train_initial_aside[_train_object_name(source, index, train)] = previous_length_on_track
                track_occupancies[initial_track_id] = previous_length_on_track + train_total_length

        train_members = train["members"]

        # Only lock multi-unit trains: single-unit trains don't need coupling and must be able to move.
        if len(train_members) > 1:
            problem.set_initial_value(locked_train(physical_train), True)

        # Initial shunting units mirror each arriving train before any split/couple action.
        shunting_unit = problem.add_object("su_" + _train_object_name(source, index, train), shunting_unit_type)
        problem.set_initial_value(active_su(shunting_unit), True)
        train_total_length = _train_total_length(train)
        problem.set_initial_value(su_length(shunting_unit), up.Real(train_total_length))
        if initial_track_id in id_to_track_part:
            problem.set_initial_value(at_su(shunting_unit, id_to_track_part[initial_track_id]), True)
            su_aside = train_initial_aside.get(_train_object_name(source, index, train), Fraction(0))
            problem.set_initial_value(su_aside_distance(shunting_unit), up.Real(su_aside))
        composition_obj = None
        if len(train_members) > 1:
            composition_obj = problem.add_object("composition" + train["id"], arrival_composition_type)
            problem.set_initial_value(composition_needs_uncoupling(composition_obj), True)

        for trainunit in train_members:
            unit = trainunit["trainUnit"]
            unit_obj = problem.add_object("unit" + unit["id"], train_unit_type)
            id_to_unit[unit["id"]] = unit_obj
            unit_type_by_id[unit["id"]] = train_unit_type_key(unit)
            problem.set_initial_value(unit_in_train(unit_obj, physical_train), True)
            problem.set_initial_value(contains_su(shunting_unit, unit_obj), True)
            if len(train_members) == 1:
                problem.set_initial_value(single_unit_su(shunting_unit, unit_obj), True)
            # Pre-create an inactive single-unit shunting unit for later split actions.
            single_unit_su_obj = problem.add_object("su_unit" + unit["id"], shunting_unit_type)
            problem.set_initial_value(contains_su(single_unit_su_obj, unit_obj), True)
            problem.set_initial_value(single_unit_su(single_unit_su_obj, unit_obj), True)
            problem.set_initial_value(su_length(single_unit_su_obj), up.Real(_train_unit_length(unit)))
            if composition_obj is None:
                problem.set_initial_value(available(unit_obj), True)
            else:
                problem.set_initial_value(part_of_composition(unit_obj, composition_obj), True)

        for first, second in zip(train_members, train_members[1:]):
            # Preserve adjacent unit order inside an arriving composition.
            first_obj = id_to_unit[first["trainUnit"]["id"]]
            second_obj = id_to_unit[second["trainUnit"]["id"]]
            problem.set_initial_value(unit_before(first_obj, second_obj), True)

    # Map request names to their problem objects so we don't add duplicates later
    request_objs = {}
    for request in all_train_requests(scenario_object):
        # This explicit-coupling variant is intentionally scoped to two-unit requests.
        if len(request["trainUnits"]) > 2:
            raise ValueError(
                "explicit_coupling currently supports only one-unit requests "
                f"or physical coupling of exactly two units; request {request['displayName']} "
                f"has {len(request['trainUnits'])} units"
            )

        request_name = "request" + request["displayName"]
        request_obj = problem.add_object(request_name, departure_request_type)
        request_objs[request_name] = request_obj
        problem.set_initial_value(request_open(request_obj), True)

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
            if len(request["trainUnits"]) == 1:
                problem.add_goal(slot_filled(slot_obj))

            # Compatibility keeps matching type-safe before any coupling action runs.
            for unit_id, unit_obj in id_to_unit.items():
                if unit_type_by_id[unit_id] == requested_key:
                    problem.set_initial_value(compatible(unit_obj, slot_obj), True)

        if len(slot_objects) == 2:
            # Slot order is only used by explicit two-unit coupling.
            problem.set_initial_value(slot_before(slot_objects[0], slot_objects[1]), True)
            # Request shunting units are inactive until a coupling action assembles them.
            request_su = problem.add_object("su_" + request_name, shunting_unit_type)
            problem.set_initial_value(request_su_for_request(request_su, request_obj), True)

            problem.add_goal(request_assembled(request_obj))
            problem.add_goal(departed_su(request_su))

    ### Write to files
    if output_file is None:
        output_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", f"{scenario_name}.pddl")
    

    print(problem.actions)
    writer = PDDLWriter(problem)
    writer.write_problem(output_file)

    # Print the output file of the domain
    print(f"Problem file written to: {output_file}")

    if domain_file is not None:
        if os.sep not in domain_file:
            domain_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", domain_file)
        writer.write_domain(domain_file)


if __name__ == "__main__":
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level.upper())

    # Set the domain file name
    args.domain_file = "domain.pddl" if args.domain_file is None else args.domain_file



    create_instance_from_scenario(domain_file=args.domain_file, path_to_folder=args.path_to_folder, scenario_file=args.scenario_file, location_file=args.location_file, output_file=args.output_file)
