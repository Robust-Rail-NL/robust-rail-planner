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
parser.add_argument("--coupling-mode", choices=["implicit_free_uncoupling", "implicit_explicit_uncoupling", "explicit_coupling"], default="implicit_free_uncoupling", required=False, help="Controls the matching/coupling modelling ladder.")
parser.add_argument("--subproblem", choices=["matching", "parking", "combined"], default="combined", required=False, help="Selects which subproblem goals to emit.")


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


def all_train_requests(scenario_object):
    # Outgoing demand comes from both regular departures and outstanding requests.
    requests = []
    requests.extend(scenario_object.get("out", {}).get("trainRequests", []))
    requests.extend(scenario_object.get("outStanding", {}).get("trainRequests", []))
    return requests


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


def _train_initial_track_id(train, preferred_keys):
    # Pick the first available track field for trains whose JSON shape differs by scenario block.
    for key in preferred_keys:
        if train.get(key) is not None:
            return train.get(key)
    return None


def create_instance_from_scenario(path_to_folder=None, scenario_file=None, location_file=None, output_file=None, domain_file=None, coupling_mode="implicit_free_uncoupling", subproblem="combined"):
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
    include_parking = subproblem in ["parking", "combined"]
    include_matching = subproblem in ["matching", "combined"]

    # In unified planning the domain information is included in the problem class
    problem = up.Problem(scenario_name)
    track_part_type = up.UserType("trackpart")
    train_unit_type = up.UserType("trainunit")
    arrival_train_type = up.UserType("arrivaltrain")
    departure_request_type = up.UserType("departurerequest")
    request_slot_type = up.UserType("requestslot")
    arrival_composition_type = up.UserType("arrivalcomposition")

    # free           = problem.add_fluent(up.Fluent("free",           up.BoolType(), trackpart=track_part_type),                          default_initial_value=True)
    arrival        = problem.add_fluent(up.Fluent("arrival",        up.IntType(),  train=arrival_train_type))
    at             = problem.add_fluent(up.Fluent("at",             up.BoolType(), unit=arrival_train_type, trackpart=track_part_type),  default_initial_value=False)
    parking_allowed = problem.add_fluent(up.Fluent("parking_allowed", up.BoolType(), trackpart=track_part_type),                         default_initial_value=False)
    parked         = problem.add_fluent(up.Fluent("parked",         up.BoolType(), train=arrival_train_type),                           default_initial_value=False)
    departed       = problem.add_fluent(up.Fluent("departed",       up.BoolType(), train=arrival_train_type),                           default_initial_value=False)
    # connected      = problem.add_fluent(up.Fluent("connected",      up.BoolType(), from_=track_part_type, to=track_part_type),           default_initial_value=False)
    connected_aside = problem.add_fluent(up.Fluent("connected_aside", up.BoolType(), from_=track_part_type, to=track_part_type),           default_initial_value=False)
    connected_bside = problem.add_fluent(up.Fluent("connected_bside", up.BoolType(), from_=track_part_type, to=track_part_type),           default_initial_value=False)
    departure_exit_a = problem.add_fluent(up.Fluent("departure_exit_a", up.BoolType(), trackpart=track_part_type),                          default_initial_value=False)
    departure_exit_b = problem.add_fluent(up.Fluent("departure_exit_b", up.BoolType(), trackpart=track_part_type),                          default_initial_value=False)
    entry_distance = problem.add_fluent(up.Fluent("entry_distance", up.IntType(),  trackpart=track_part_type),                          default_initial_value=up.Int(0))
    departure_rank = problem.add_fluent(up.Fluent("departure_rank", up.IntType(),  train=arrival_train_type),                           default_initial_value=up.Int(0))
    track_is_parked_at = problem.add_fluent(up.Fluent("track_is_parked_at", up.BoolType(), trackpart=track_part_type),                  default_initial_value=False)
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
    
    available      = problem.add_fluent(up.Fluent("available",      up.BoolType(), unit=train_unit_type),                               default_initial_value=False)
    request_open   = problem.add_fluent(up.Fluent("request_open",   up.BoolType(), request=departure_request_type),                     default_initial_value=False)
    slot_open      = problem.add_fluent(up.Fluent("slot_open",      up.BoolType(), slot=request_slot_type),                             default_initial_value=False)
    slot_filled    = problem.add_fluent(up.Fluent("slot_filled",    up.BoolType(), slot=request_slot_type),                             default_initial_value=False)
    compatible     = problem.add_fluent(up.Fluent("compatible",     up.BoolType(), unit=train_unit_type, slot=request_slot_type),        default_initial_value=False)
    matched        = problem.add_fluent(up.Fluent("matched",        up.BoolType(), unit=train_unit_type, slot=request_slot_type),        default_initial_value=False)
    slot_for_request = problem.add_fluent(up.Fluent("slot_for_request", up.BoolType(), slot=request_slot_type, request=departure_request_type), default_initial_value=False)
    

    startMove = up.InstantaneousAction('start_move', t=arrival_train_type)
    startMove.add_precondition(up.Not(allowed_to_move(startMove.t)))
    startMove.add_precondition(concurrent_movements < max_concurrent_movements)
    startMove.add_effect(allowed_to_move(startMove.t), True)
    startMove.add_effect(concurrent_movements, concurrent_movements + 1)
    problem.add_action(startMove)

    endMove = up.InstantaneousAction('end_move', t=arrival_train_type, l=track_part_type)
    endMove.add_precondition(allowed_to_move(endMove.t))
    endMove.add_precondition(at(endMove.t, endMove.l))
    endMove.add_precondition(parking_allowed(endMove.l))
    endMove.add_effect(allowed_to_move(endMove.t), False)
    endMove.add_effect(concurrent_movements, concurrent_movements - 1)
    problem.add_action(endMove)

    move_aside_empty = up.InstantaneousAction('move_aside_empty', t=arrival_train_type, l_from=track_part_type, l_to=track_part_type)
    move_aside_empty.add_precondition(allowed_to_move(move_aside_empty.t))
    move_aside_empty.add_precondition(at(move_aside_empty.t, move_aside_empty.l_from))
    move_aside_empty.add_precondition(connected_aside(move_aside_empty.l_from, move_aside_empty.l_to))
    move_aside_empty.add_precondition(up.Not(parked(move_aside_empty.t)))
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

    move_aside_occupied = up.InstantaneousAction('move_aside_occupied', t=arrival_train_type, l_from=track_part_type, l_to=track_part_type)
    move_aside_occupied.add_precondition(allowed_to_move(move_aside_occupied.t))
    move_aside_occupied.add_precondition(at(move_aside_occupied.t, move_aside_occupied.l_from))
    move_aside_occupied.add_precondition(connected_aside(move_aside_occupied.l_from, move_aside_occupied.l_to))
    move_aside_occupied.add_precondition(up.Not(parked(move_aside_occupied.t)))
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

    move_bside_empty = up.InstantaneousAction('move_bside_empty', t=arrival_train_type, l_from=track_part_type, l_to=track_part_type)
    move_bside_empty.add_precondition(allowed_to_move(move_bside_empty.t))
    move_bside_empty.add_precondition(at(move_bside_empty.t, move_bside_empty.l_from))
    move_bside_empty.add_precondition(connected_bside(move_bside_empty.l_from, move_bside_empty.l_to))
    move_bside_empty.add_precondition(up.Not(parked(move_bside_empty.t)))
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

    move_bside_occupied = up.InstantaneousAction('move_bside_occupied', t=arrival_train_type, l_from=track_part_type, l_to=track_part_type)
    move_bside_occupied.add_precondition(allowed_to_move(move_bside_occupied.t))
    move_bside_occupied.add_precondition(at(move_bside_occupied.t, move_bside_occupied.l_from))
    move_bside_occupied.add_precondition(connected_bside(move_bside_occupied.l_from, move_bside_occupied.l_to))
    move_bside_occupied.add_precondition(up.Not(parked(move_bside_occupied.t)))
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

    depart_aside = up.InstantaneousAction('depart_aside', t=arrival_train_type, l=track_part_type)
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

    park = up.InstantaneousAction('park', t=arrival_train_type, l=track_part_type)
    park.add_precondition(at(park.t, park.l))
    park.add_precondition(parking_allowed(park.l))
    park.add_effect(parked(park.t), True)
    park.add_effect(track_is_parked_at(park.l), True)
    problem.add_action(park)

    explicit_uncoupling = coupling_mode in ["implicit_explicit_uncoupling", "explicit_coupling"]
    explicit_coupling = coupling_mode == "explicit_coupling"

    if explicit_uncoupling:
        # Units in a multi-unit arrival must first be released from their composition.
        part_of_composition = problem.add_fluent(up.Fluent("part_of_composition", up.BoolType(), unit=train_unit_type, composition=arrival_composition_type), default_initial_value=False)
        composition_needs_uncoupling = problem.add_fluent(up.Fluent("composition_needs_uncoupling", up.BoolType(), composition=arrival_composition_type), default_initial_value=False)

        uncouple = up.InstantaneousAction("uncouple", unit=train_unit_type, composition=arrival_composition_type)
        # Preconditions: the unit belongs to a composition that still needs splitting.
        uncouple.add_precondition(part_of_composition(uncouple.unit, uncouple.composition))
        uncouple.add_precondition(composition_needs_uncoupling(uncouple.composition))
        # Effects: the unit becomes independently matchable and is removed from that composition.
        uncouple.add_effect(available(uncouple.unit), True)
        uncouple.add_effect(part_of_composition(uncouple.unit, uncouple.composition), False)
        problem.add_action(uncouple)

    if explicit_coupling:
        # Explicit coupling turns a completed match into a coupled departure slot.
        slot_coupled = problem.add_fluent(up.Fluent("slot_coupled", up.BoolType(), slot=request_slot_type), default_initial_value=False)
        coupled_to_request = problem.add_fluent(up.Fluent("coupled_to_request", up.BoolType(), unit=train_unit_type, request=departure_request_type), default_initial_value=False)

        couple_to_request = up.InstantaneousAction("couple_to_request", unit=train_unit_type, slot=request_slot_type, request=departure_request_type)
        # Preconditions: the unit is already matched to a slot that belongs to this request.
        couple_to_request.add_precondition(matched(couple_to_request.unit, couple_to_request.slot))
        couple_to_request.add_precondition(slot_for_request(couple_to_request.slot, couple_to_request.request))
        # Effects: the slot is explicitly marked as coupled into the departure request.
        couple_to_request.add_effect(slot_coupled(couple_to_request.slot), True)
        couple_to_request.add_effect(coupled_to_request(couple_to_request.unit, couple_to_request.request), True)
        problem.add_action(couple_to_request)

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
    track_train_counts = {}
    arrival_train_objs = []

    # Add track part objects
    id_to_track_part = {}
    for track_part in location_object["trackParts"]:
        obj = problem.add_object(track_part["name"], track_part_type)
        id_to_track_part[track_part["id"]] = obj
        if track_part["id"] in exit_ids_a:
            problem.set_initial_value(departure_exit_a(obj), True)
        if track_part["id"] in exit_ids_b:
            problem.set_initial_value(departure_exit_b(obj), True)
        if track_part.get("parkingAllowed", False):
            problem.set_initial_value(parking_allowed(obj), True)
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


    id_to_unit = {}
    unit_type_by_id = {}

    if include_parking:
        # Add inbound trains
        for train in inbound_trains:
            arrival_train = problem.add_object("train" + train["id"], arrival_train_type)
            arrival_train_objs.append(arrival_train)
            problem.set_initial_value(arrival(arrival_train), up.Int(int(train["arrival"])))
            initial_track_id = _train_initial_track_id(train, ["entryTrackPart", "firstParkingTrackPart"])
            problem.set_initial_value(at(arrival_train, id_to_track_part[initial_track_id]), True)
            # problem.set_initial_value(departure_rank(arrival_train), up.Int(train_to_rank[train["id"]]))
            train_total_length = _train_total_length(train)
            problem.set_initial_value(train_length(arrival_train), up.Real(train_total_length))
            previous_length_on_track = track_occupancies.get(initial_track_id, Fraction(0))
            problem.set_initial_value(aside_distance(arrival_train), up.Real(previous_length_on_track))
            track_occupancies[initial_track_id] = track_occupancies.get(initial_track_id, Fraction(0)) + train_total_length
            track_train_counts[initial_track_id] = track_train_counts.get(initial_track_id, 0) + 1
            #problem.add_goal(parked(arrival_train))


        for index, train in enumerate(in_standing_trains):
            standing_train = problem.add_object(f"train_in_standing_{index}", arrival_train_type)
            arrival_train_objs.append(standing_train)
            initial_track_id = _train_initial_track_id(train, ["firstParkingTrackPart", "entryTrackPart"])
            problem.set_initial_value(arrival(standing_train), up.Int(int(train.get("arrival", 0))))
            if initial_track_id is not None:
                problem.set_initial_value(at(standing_train, id_to_track_part[initial_track_id]), True)
                train_total_length = _train_total_length(train)
                problem.set_initial_value(train_length(standing_train), up.Real(train_total_length))
                previous_length_on_track = track_occupancies.get(initial_track_id, Fraction(0))
                problem.set_initial_value(aside_distance(standing_train), up.Real(previous_length_on_track))
                track_occupancies[initial_track_id] = track_occupancies.get(initial_track_id, Fraction(0)) + train_total_length
                track_train_counts[initial_track_id] = track_train_counts.get(initial_track_id, 0) + 1

        if not include_matching:
            for request in out_standing_trains:
                track_id = request.get("lastParkingTrackPart")
                if track_id in id_to_track_part:
                    problem.add_goal(track_is_parked_at(id_to_track_part[track_id]))

        # Add outbound train requests: these trains must be assembled (contain all units) and depart.
        # Add a goal stating that the number of departed trains must be equal to out_requests
        if not include_matching:
            problem.add_goal(up.Equals(num_of_departed_trains(), up.Int(len(out_requests))))

        for track_id, occupied_length_value in track_occupancies.items():
            track_obj = id_to_track_part[track_id]
            # problem.set_initial_value(free(track_obj), False)
            problem.set_initial_value(astack_distance(track_obj), up.Real(Fraction(0)))
            problem.set_initial_value(bstack_distance(track_obj), up.Real(occupied_length_value))
            problem.set_initial_value(number_of_trains_on_track(track_obj), up.Int(track_train_counts.get(track_id, 0)))

    for train in all_trains(scenario_object):
        train_members = train["members"]
        composition_obj = None
        if explicit_uncoupling and len(train_members) > 1:
            composition_obj = problem.add_object("composition" + train["id"], arrival_composition_type)
            problem.set_initial_value(composition_needs_uncoupling(composition_obj), True)

        for trainunit in train_members:
            unit = trainunit["trainUnit"]
            unit_obj = problem.add_object("unit" + unit["id"], train_unit_type)
            id_to_unit[unit["id"]] = unit_obj
            unit_type_by_id[unit["id"]] = train_unit_type_key(unit)
            if composition_obj is None:
                problem.set_initial_value(available(unit_obj), True)
            else:
                problem.set_initial_value(part_of_composition(unit_obj, composition_obj), True)

    if include_matching:
        # Map request names to their problem objects so we don't add duplicates later
        request_objs = {}
        for request in all_train_requests(scenario_object):
            request_name = "request" + request["displayName"]
            request_obj = problem.add_object(request_name, departure_request_type)
            request_objs[request_name] = request_obj
            problem.set_initial_value(request_open(request_obj), True)

            for index, requested_unit in enumerate(request["trainUnits"]):
                slot_obj = problem.add_object(f"{request_name}_slot{index}", request_slot_type)
                requested_key = train_unit_type_key(requested_unit)

                problem.set_initial_value(slot_open(slot_obj), True)
                problem.set_initial_value(slot_for_request(slot_obj, request_obj), True)
                if explicit_coupling:
                    problem.add_goal(slot_coupled(slot_obj))
                else:
                    problem.add_goal(slot_filled(slot_obj))

                for unit_id, unit_obj in id_to_unit.items():
                    if unit_type_by_id[unit_id] == requested_key:
                        problem.set_initial_value(compatible(unit_obj, slot_obj), True)

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



    create_instance_from_scenario(domain_file=args.domain_file, path_to_folder=args.path_to_folder, scenario_file=args.scenario_file, location_file=args.location_file, output_file=args.output_file, coupling_mode=args.coupling_mode, subproblem=args.subproblem)
