import os
import sys
import json
import logging
import argparse
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

    free = problem.add_fluent(up.Fluent("free", up.BoolType(), trackpart=track_part_type), default_initial_value=True)
    arrival = problem.add_fluent(up.Fluent("arrival", up.IntType(), train=arrival_train_type))
    at = problem.add_fluent(up.Fluent("at", up.BoolType(), unit=arrival_train_type, trackpart=track_part_type), default_initial_value=False)
    
    parking_allowed = problem.add_fluent(up.Fluent("parking_allowed", up.BoolType(), trackpart=track_part_type), default_initial_value=False)
    parked = problem.add_fluent(up.Fluent("parked", up.BoolType(), train=arrival_train_type), default_initial_value=False)

    move = up.InstantaneousAction('move', t=arrival_train_type, l_from=track_part_type, l_to=track_part_type)
    move.add_precondition(at(move.t, move.l_from))
    move.add_effect(at(move.t, move.l_to), True)
    move.add_effect(at(move.t, move.l_from), False)
    problem.add_action(move)

    park = up.InstantaneousAction('park', t=arrival_train_type, l=track_part_type)
    park.add_precondition(at(park.t, park.l))
    park.add_precondition(parking_allowed(park.l))
    park.add_effect(parked(park.t), True)
    problem.add_action(park)

    # Example add objects for track parts
    id_to_track_part = {}
    for track_part in location_object["trackParts"]:
        obj = problem.add_object(track_part["name"], track_part_type)
        id_to_track_part[track_part["id"]] = obj
        if track_part.get("parkingAllowed", False):
            problem.set_initial_value(parking_allowed(obj), True)

    # Example set initial value for numeric fluents
    for train_group in ["in"]:
        if train_group in scenario_object:
            for train in scenario_object[train_group]["trains"]:
                arrival_train = problem.add_object("train" + train["id"], arrival_train_type)
                problem.set_initial_value(arrival(arrival_train), up.Int(int(train["arrival"])))
                problem.set_initial_value(at(arrival_train, id_to_track_part[train["firstParkingTrackPart"]]), True)
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


if __name__ == "__main__":
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level.upper())
    create_instance_from_scenario(domain_file=args.domain_file, path_to_folder=args.path_to_folder, scenario_file=args.scenario_file, location_file=args.location_file, output_file=args.output_file)
