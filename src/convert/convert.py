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

    free           = problem.add_fluent(up.Fluent("free",           up.BoolType(), trackpart=track_part_type),                          default_initial_value=True)
    arrival        = problem.add_fluent(up.Fluent("arrival",        up.IntType(),  train=arrival_train_type))
    at             = problem.add_fluent(up.Fluent("at",             up.BoolType(), unit=arrival_train_type, trackpart=track_part_type),  default_initial_value=False)
    parking_allowed = problem.add_fluent(up.Fluent("parking_allowed", up.BoolType(), trackpart=track_part_type),                         default_initial_value=False)
    parked         = problem.add_fluent(up.Fluent("parked",         up.BoolType(), train=arrival_train_type),                           default_initial_value=False)
    connected      = problem.add_fluent(up.Fluent("connected",      up.BoolType(), from_=track_part_type, to=track_part_type),           default_initial_value=False)
    entry_distance = problem.add_fluent(up.Fluent("entry_distance", up.IntType(),  trackpart=track_part_type),                          default_initial_value=up.Int(0))
    departure_rank = problem.add_fluent(up.Fluent("departure_rank", up.IntType(),  train=arrival_train_type),                           default_initial_value=up.Int(0))


    track_capacity = problem.add_fluent(
        up.Fluent("track_capacity", up.RealType(), trackpart=track_part_type),
        default_initial_value=up.Real(Fraction(0))
    )

    train_length = problem.add_fluent(
        up.Fluent("train_length", up.RealType(), train=arrival_train_type),
        default_initial_value=up.Real(Fraction(0))
    )

    move = up.InstantaneousAction('move', t=arrival_train_type, l_from=track_part_type, l_to=track_part_type)
    move.add_precondition(at(move.t, move.l_from))
    # Ensure that the train will only move to a connected track
    move.add_precondition(connected(move.l_from, move.l_to))
    # The train can only move to a track if it is free
    move.add_precondition(free(move.l_to))

    move.add_precondition(
        track_capacity(move.l_to) >= train_length(move.t)
    )

    move.add_effect(at(move.t, move.l_to), True)
    move.add_effect(at(move.t, move.l_from), False)
    # After the move the from track becomes free and the to track becomes occupied
    # We might want to make all tracks unfree if we know that a train is going to be on it
    move.add_effect(free(move.l_to), False)
    move.add_effect(free(move.l_from), True)

    move.add_effect(
        track_capacity(move.l_to),
        track_capacity(move.l_to) - train_length(move.t)
    )

    move.add_effect(
        track_capacity(move.l_from),
        track_capacity(move.l_from) + train_length(move.t)
    )

    problem.add_action(move)

    park = up.InstantaneousAction('park', t=arrival_train_type, l=track_part_type)
    park.add_precondition(at(park.t, park.l))
    park.add_precondition(parking_allowed(park.l))
    # Add that a train must be parked at a track corresponding to its departure rank. This enforces that trains will always be parked at a closer distance to the exist than trains that leave later.
    # Note: this means that all trains with the same departure time will be parked on tracks with that same entry distance. This can be impossible due to the track not being long enough; this is not being checked.
    park.add_precondition(up.Equals(departure_rank(park.t), entry_distance(park.l)))
    park.add_effect(parked(park.t), True)
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
    train_to_rank = _compute_departure_ranks(inbound_trains)

    # Add track part objects
    id_to_track_part = {}
    for track_part in location_object["trackParts"]:
        obj = problem.add_object(track_part["name"], track_part_type)
        id_to_track_part[track_part["id"]] = obj
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

    # Add inbound trains
    for train in inbound_trains:
        arrival_train = problem.add_object("train" + train["id"], arrival_train_type)
        problem.set_initial_value(arrival(arrival_train), up.Int(int(train["arrival"])))
        # The initial position of the train is at its entry track, and that track becomes occupied (not free)
        problem.set_initial_value(free(id_to_track_part[train["firstParkingTrackPart"]]), False)
        problem.set_initial_value(at(arrival_train, id_to_track_part[train["firstParkingTrackPart"]]), True)
        problem.set_initial_value(departure_rank(arrival_train), up.Int(train_to_rank[train["id"]]))

        total_train_length = Fraction(0)

        for member in train["members"]:
            total_train_length += Fraction(str(member["trainUnit"]["type"]["length"]))

        problem.set_initial_value(
            train_length(arrival_train),
            up.Real(total_train_length)
        )

        # Remove occupied capacity from starting track

        current_track = id_to_track_part[train["firstParkingTrackPart"]]

        track_obj = next(
            tp for tp in location_object["trackParts"]
            if tp["id"] == train["firstParkingTrackPart"]
        )

        original_length = Fraction(str(track_obj.get("length", 100.0)))

        remaining_capacity = original_length - total_train_length

        problem.set_initial_value(
            track_capacity(current_track),
            up.Real(remaining_capacity)
        )

        problem.add_goal(parked(arrival_train))
        for trainunit in train["members"]:
            problem.add_object("unit" + trainunit["trainUnit"]["id"], train_unit_type)

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
