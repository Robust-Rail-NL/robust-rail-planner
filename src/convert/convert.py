import os
from shutil import move
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


def _departure_exit_ids(scenario_object):
    # The departure track is where outbound trains leave the yard — the BFS root for entry_distance.
    # Falls back to inbound entry tracks if no outbound requests are present in the scenario.
    # In: a scenario object containing:
    # - a section "out", containing the list "trainRequests" where each element has:
    #   - "leaveTrackPart", containing a specification of from which track part a train should leave
    # - a section "in", containing the list "trains" where each element has:
    #   - "entryTrackPart", containing a specification of at which track a train will enter the yard
    # Out: a list of all trackparts that are mentioned as "leaveTrackPart" in any "out" train Request if present.
    # If there are no leaveTrackParts, it returns a list of all trackparts that are mentioned as "entryTrackPart" for any "in" train
    ids = [req["leaveTrackPart"] for req in scenario_object.get("out", {}).get("trainRequests", []) if "leaveTrackPart" in req]
    if not ids:
        ids = [t["entryTrackPart"] for t in scenario_object.get("in", {}).get("trains", []) if "entryTrackPart" in t]
    return ids


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


def _generic_train_name(prefix, index):
    return f"{prefix}_{index}"


def _train_total_length(train):
    total_length = Fraction(0)
    # Support both 'members' (incoming format) and 'trainUnits' (outgoing requests)
    if "members" in train:
        for member in train.get("members", []):
            total_length += Fraction(str(member["trainUnit"]["type"]["length"]))
    elif "trainUnits" in train:
        for tu in train.get("trainUnits", []):
            total_length += Fraction(str(tu.get("type", {}).get("length", 0)))
    return total_length


def _train_initial_track_id(train, preferred_keys):
    for key in preferred_keys:
        if train.get(key) is not None:
            return train.get(key)
    return None


def _add_train_units(problem, train, train_unit_type, name_prefix):
    # Support both inbound 'members' and outbound 'trainUnits'
    if "members" in train:
        for index, member in enumerate(train.get("members", [])):
            unit_id = member["trainUnit"].get("id") or f"{name_prefix}_{index}"
            problem.add_object("unit" + unit_id, train_unit_type)
    elif "trainUnits" in train:
        for index, tu in enumerate(train.get("trainUnits", [])):
            unit_id = tu.get("id") or f"{name_prefix}_out_{index}"
            problem.add_object("unit" + unit_id, train_unit_type)


def _register_train(problem, train, id_to_track_part, arrival_train_type, train_unit_type, occupancy_by_track, arrival_fluent, at_fluent, parked_fluent, train_length_fluent, object_name, initial_track_id=None, parked_initial=False, arrival_time=None):
    train_obj = problem.add_object(object_name, arrival_train_type)

    if arrival_time is not None:
        problem.set_initial_value(arrival_fluent(train_obj), up.Int(int(arrival_time)))

    total_length = _train_total_length(train)
    problem.set_initial_value(train_length_fluent(train_obj), up.Real(total_length))

    if initial_track_id is not None:
        track_obj = id_to_track_part[initial_track_id]
        problem.set_initial_value(at_fluent(train_obj, track_obj), True)
        occupancy_by_track[initial_track_id] = occupancy_by_track.get(initial_track_id, Fraction(0)) + total_length

    if parked_initial:
        problem.set_initial_value(parked_fluent(train_obj), True)

    _add_train_units(problem, train, train_unit_type, object_name)
    return train_obj


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

    # --------------------------- start model construction --------------------------

    # In unified planning the domain information is included in the problem class
    problem = up.Problem(scenario_name)
    track_part_type = up.UserType("trackpart")
    train_unit_type = up.UserType("trainunit")
    arrival_train_type = up.UserType("arrivaltrain")

    free           = problem.add_fluent(up.Fluent("free",           up.BoolType(), trackpart=track_part_type),                          default_initial_value=True)
    arrival        = problem.add_fluent(up.Fluent("arrival",        up.IntType(),  train=arrival_train_type))
    at             = problem.add_fluent(up.Fluent("at",             up.BoolType(), unit=arrival_train_type, trackpart=track_part_type),  default_initial_value=False)
    parking_allowed = problem.add_fluent(up.Fluent("parking_allowed", up.BoolType(), trackpart=track_part_type),                         default_initial_value=False)
    parked         = problem.add_fluent(up.Fluent("parked",         up.BoolType(), train=arrival_train_type),                           default_initial_value=False)
    departed       = problem.add_fluent(up.Fluent("departed",       up.BoolType(), train=arrival_train_type),                           default_initial_value=False)
    connected      = problem.add_fluent(up.Fluent("connected",      up.BoolType(), from_=track_part_type, to=track_part_type),           default_initial_value=False)
    departure_exit = problem.add_fluent(up.Fluent("departure_exit", up.BoolType(), trackpart=track_part_type),                        default_initial_value=False)
    entry_distance = problem.add_fluent(up.Fluent("entry_distance", up.IntType(),  trackpart=track_part_type),                          default_initial_value=up.Int(0))
    departure_rank = problem.add_fluent(up.Fluent("departure_rank", up.IntType(),  train=arrival_train_type),                           default_initial_value=up.Int(0))
    track_is_parked_at = problem.add_fluent(up.Fluent("track_is_parked_at", up.BoolType(), trackpart=track_part_type), default_initial_value=False)
    num_of_departed_trains = problem.add_fluent(up.Fluent("num_of_departed_trains", up.IntType()), default_initial_value=up.Int(0))


    track_capacity = problem.add_fluent(
        up.Fluent("track_capacity", up.RealType(), trackpart=track_part_type),
        default_initial_value=up.Real(Fraction(0))
    )

    train_length = problem.add_fluent(
        up.Fluent("train_length", up.RealType(), train=arrival_train_type),
        default_initial_value=up.Real(Fraction(0))
    )

    occupied_length = problem.add_fluent(
        up.Fluent("occupied_length", up.RealType(), trackpart=track_part_type),
        default_initial_value=up.Real(Fraction(0))
    )

    move = up.InstantaneousAction('move', t=arrival_train_type, l_from=track_part_type, l_to=track_part_type)
    move.add_precondition(at(move.t, move.l_from))
    # Ensure that the train will only move to a connected track
    move.add_precondition(connected(move.l_from, move.l_to))
    # The train can only move to a track if it is free
    move.add_precondition(free(move.l_to))
    # The train can only move if it is not parked (enforces that parking and moving are mutually exclusive)
    move.add_precondition(up.Not(parked(move.t)))

    move.add_effect(at(move.t, move.l_to), True)
    move.add_effect(at(move.t, move.l_from), False)
    # After the move the from track becomes free and the to track becomes occupied
    # We might want to make all tracks unfree if we know that a train is going to be on it
    move.add_effect(free(move.l_to), False)
    move.add_effect(free(move.l_from), True)

    # Make sure the train is no longer parked
    # move.add_effect(parked(move.t), False)
    # move.add_effect(track_is_parked_at(move.l_from), False)

    problem.add_action(move)

    depart = up.InstantaneousAction('depart', t=arrival_train_type, l=track_part_type)
    depart.add_precondition(at(depart.t, depart.l))
    # depart.add_precondition(parked(depart.t))
    depart.add_precondition(departure_exit(depart.l))
    depart.add_effect(at(depart.t, depart.l), False)
    depart.add_effect(free(depart.l), True)
    depart.add_effect(occupied_length(depart.l), occupied_length(depart.l) - train_length(depart.t))
    depart.add_effect(parked(depart.t), False)
    depart.add_effect(departed(depart.t), True)
    depart.add_effect(num_of_departed_trains(), num_of_departed_trains() + 1)

    problem.add_action(depart)

    park = up.InstantaneousAction('park', t=arrival_train_type, l=track_part_type)
    park.add_precondition(at(park.t, park.l))
    park.add_precondition(parking_allowed(park.l))
    park.add_precondition(
        occupied_length(park.l) + train_length(park.t)
        <= track_capacity(park.l)
    )
    park.add_effect(parked(park.t), True)
    park.add_effect(track_is_parked_at(park.l), True)
    # When a train parks, it occupies the track it is on. 
    park.add_effect(
        occupied_length(park.l),
        occupied_length(park.l) + train_length(park.t)
    )
    problem.add_action(park)

    # Pre-compute connectivity and entry distances from JSON (no UP objects yet)
    adjacency = _build_adjacency(location_object)
    exit_ids = _departure_exit_ids(scenario_object)
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

    track_occupancies = {}

    # Add track part objects
    id_to_track_part = {}
    for track_part in location_object["trackParts"]:
        obj = problem.add_object(track_part["name"], track_part_type)
        id_to_track_part[track_part["id"]] = obj
        if track_part["id"] in exit_ids:
            problem.set_initial_value(departure_exit(obj), True)
        if track_part.get("parkingAllowed", False):
            problem.set_initial_value(parking_allowed(obj), True)
            tp_id = track_part["id"]
            if tp_id in bfs_dist and bfs_dist[tp_id] in bfs_to_entry_dist:
                problem.set_initial_value(entry_distance(obj), up.Int(bfs_to_entry_dist[bfs_dist[tp_id]]))

        track_length_value = Fraction(str(track_part.get("length", 100.0)))

        problem.set_initial_value(
            track_capacity(obj),
            up.Real(track_length_value)
        )

    # Set connectivity (each undirected edge -> two directed facts)
    connected_pairs = set()
    for track_part in location_object["trackParts"]:
        src_id = track_part["id"]
        for nb_id in track_part.get("aSide", []) + track_part.get("bSide", []):
            if nb_id in id_to_track_part:
                pair = tuple(sorted([src_id, nb_id]))
                if pair not in connected_pairs:
                    connected_pairs.add(pair)
                    problem.set_initial_value(connected(id_to_track_part[src_id], id_to_track_part[nb_id]), True)
                    problem.set_initial_value(connected(id_to_track_part[nb_id], id_to_track_part[src_id]), True)

    # Add inbound trains: they must move to their first parking track and park there.
    for index, train in enumerate(inbound_trains):
        train_obj = _register_train(
            problem,
            train,
            id_to_track_part,
            arrival_train_type,
            train_unit_type,
            track_occupancies,
            arrival,
            at,
            parked,
            train_length,
            _generic_train_name("train_in", index),
            initial_track_id=train["entryTrackPart"],
            parked_initial=False,
            arrival_time=train.get("arrival"),
        )
        # problem.add_goal(parked(train_obj))

    # Add trains that are already parked in the yard
    for index, train in enumerate(in_standing_trains):
        _register_train(
            problem,
            train,
            id_to_track_part,
            arrival_train_type,
            train_unit_type,
            track_occupancies,
            arrival,
            at,
            parked,
            train_length,
            _generic_train_name("train_in_standing", index),
            initial_track_id=_train_initial_track_id(train, ["firstParkingTrackPart", "entryTrackPart"]),
            parked_initial=False,
            arrival_time=train.get("arrival", 0),
        )

    # Add out standing trains. These are request/goals for certain train units to be at a certain point at the end of the plan
    for index, train in enumerate(out_standing_trains):
        # Get the location where there should be a train unit at the end of the plan
        track_id = train.get("lastParkingTrackPart")
        # Now add a goal that there should be a train unit at that location at the end of the plan
        if track_id in id_to_track_part:
            track_obj = id_to_track_part[track_id]
            problem.add_goal(track_is_parked_at(track_obj))

    # Add outbound train requests: these trains must be assembled (contain all units) and depart.
    # Add a goal stating that the number of departed trains must be equal to out_requests
    problem.add_goal(up.Equals(num_of_departed_trains(), up.Int(len(out_requests))))
    

    for track_id, occupied_length_value in track_occupancies.items():
        track_obj = id_to_track_part[track_id]
        problem.set_initial_value(free(track_obj), False)
        problem.set_initial_value(occupied_length(track_obj), up.Real(occupied_length_value))


    # -------------------------- end model construction, start writing to file --------------------------

    ### Write to files
    if output_file is None:
        output_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", f"{scenario_name}.pddl")
    
    writer = PDDLWriter(problem)
    writer.write_problem(output_file)

    if domain_file is not None:
        if os.sep not in domain_file:
            domain_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", domain_file)
        writer.write_domain(domain_file)


    ### Debug the files that were written to
    print(f"Written problem to {output_file}")
    print(f"Domain file written to {domain_file}" if domain_file else "No domain file written.")


if __name__ == "__main__":
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level.upper())

    # If the domain file is not specified default it to {scenario_name}_domain.pddl in the same output directory
    if args.domain_file is None:
        args.domain_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", f"{args.scenario_file.replace('.json', '')}_domain.pddl")


    create_instance_from_scenario(domain_file=args.domain_file, path_to_folder=args.path_to_folder, scenario_file=args.scenario_file, location_file=args.location_file, output_file=args.output_file)
