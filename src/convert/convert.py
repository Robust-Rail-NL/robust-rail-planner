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
parser.add_argument("--coupling-mode", choices=["implicit_free_uncoupling", "implicit_explicit_uncoupling", "explicit_coupling"], default="implicit_free_uncoupling", required=False, help="Controls the matching/coupling modelling ladder. Default keeps the current baseline: implicit coupling and free uncoupling.")


### Add logging to the arguments
parser.add_argument("--log-level", default="ERROR", required=False, help="Configure the logging level (e.g., INFO, WARNING, ERROR) default=ERROR.")


def train_unit_type_key(train_unit):
    unit_type = train_unit["type"]
    return (
        unit_type.get("displayName"),
        int(unit_type.get("carriages", 0)),
        float(unit_type.get("length", 0.0)),
    )


def all_trains(scenario_object):
    trains = []
    trains.extend(scenario_object.get("in", {}).get("trains", []))
    trains.extend(scenario_object.get("inStanding", {}).get("trains", []))
    return trains


def all_train_requests(scenario_object):
    requests = []
    requests.extend(scenario_object.get("out", {}).get("trainRequests", []))
    requests.extend(scenario_object.get("outStanding", {}).get("trainRequests", []))
    return requests


def create_instance_from_scenario(path_to_folder=None, scenario_file=None, location_file=None, output_file=None, domain_file=None, coupling_mode="implicit_free_uncoupling"):
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

    free = problem.add_fluent(up.Fluent("free", up.BoolType(), trackpart=track_part_type), default_initial_value=True)
    arrival = problem.add_fluent(up.Fluent("arrival", up.IntType(), train=arrival_train_type))
    at = problem.add_fluent(up.Fluent("at", up.BoolType(), unit=arrival_train_type, trackpart=track_part_type), default_initial_value=False)

    available = problem.add_fluent(up.Fluent("available", up.BoolType(), unit=train_unit_type), default_initial_value=False)
    request_open = problem.add_fluent(up.Fluent("request_open", up.BoolType(), request=departure_request_type), default_initial_value=False)
    slot_open = problem.add_fluent(up.Fluent("slot_open", up.BoolType(), slot=request_slot_type), default_initial_value=False)
    slot_filled = problem.add_fluent(up.Fluent("slot_filled", up.BoolType(), slot=request_slot_type), default_initial_value=False)
    compatible = problem.add_fluent(up.Fluent("compatible", up.BoolType(), unit=train_unit_type, slot=request_slot_type), default_initial_value=False)
    matched = problem.add_fluent(up.Fluent("matched", up.BoolType(), unit=train_unit_type, slot=request_slot_type), default_initial_value=False)
    slot_for_request = problem.add_fluent(up.Fluent("slot_for_request", up.BoolType(), slot=request_slot_type, request=departure_request_type), default_initial_value=False)

    explicit_uncoupling = coupling_mode in ["implicit_explicit_uncoupling", "explicit_coupling"]
    explicit_coupling = coupling_mode == "explicit_coupling"

    if explicit_uncoupling:
        part_of_composition = problem.add_fluent(up.Fluent("part_of_composition", up.BoolType(), unit=train_unit_type, composition=arrival_composition_type), default_initial_value=False)
        composition_needs_uncoupling = problem.add_fluent(up.Fluent("composition_needs_uncoupling", up.BoolType(), composition=arrival_composition_type), default_initial_value=False)

        uncouple = up.InstantaneousAction("uncouple", unit=train_unit_type, composition=arrival_composition_type)
        uncouple.add_precondition(part_of_composition(uncouple.unit, uncouple.composition))
        uncouple.add_precondition(composition_needs_uncoupling(uncouple.composition))
        uncouple.add_effect(available(uncouple.unit), True)
        uncouple.add_effect(part_of_composition(uncouple.unit, uncouple.composition), False)
        problem.add_action(uncouple)

    if explicit_coupling:
        slot_coupled = problem.add_fluent(up.Fluent("slot_coupled", up.BoolType(), slot=request_slot_type), default_initial_value=False)
        coupled_to_request = problem.add_fluent(up.Fluent("coupled_to_request", up.BoolType(), unit=train_unit_type, request=departure_request_type), default_initial_value=False)

        couple_to_request = up.InstantaneousAction("couple_to_request", unit=train_unit_type, slot=request_slot_type, request=departure_request_type)
        couple_to_request.add_precondition(matched(couple_to_request.unit, couple_to_request.slot))
        couple_to_request.add_precondition(slot_for_request(couple_to_request.slot, couple_to_request.request))
        couple_to_request.add_effect(slot_coupled(couple_to_request.slot), True)
        couple_to_request.add_effect(coupled_to_request(couple_to_request.unit, couple_to_request.request), True)
        problem.add_action(couple_to_request)

    match = up.InstantaneousAction("match", unit=train_unit_type, slot=request_slot_type)
    match.add_precondition(available(match.unit))
    match.add_precondition(slot_open(match.slot))
    match.add_precondition(compatible(match.unit, match.slot))
    match.add_effect(matched(match.unit, match.slot), True)
    match.add_effect(slot_filled(match.slot), True)
    match.add_effect(available(match.unit), False)
    match.add_effect(slot_open(match.slot), False)
    problem.add_action(match)

    # Example add objects for track parts
    id_to_track_part = {}
    for track_part in location_object["trackParts"]:
        obj = problem.add_object(track_part["name"], track_part_type)
        id_to_track_part[track_part["id"]] = obj

    id_to_unit = {}
    unit_type_by_id = {}

    # Example set initial value for numeric fluents
    for train in all_trains(scenario_object):
        arrival_train = problem.add_object("train" + train["id"], arrival_train_type)
        problem.set_initial_value(arrival(arrival_train), up.Int(int(train["arrival"])))
        problem.set_initial_value(at(arrival_train, id_to_track_part[train["firstParkingTrackPart"]]), True)

        train_members = train["members"]
        composition_obj = None
        if explicit_uncoupling and len(train_members) > 1:
            composition_obj = problem.add_object("composition" + train["id"], arrival_composition_type)
            problem.set_initial_value(composition_needs_uncoupling(composition_obj), True)

        for trainunit in train["members"]:
            unit = trainunit["trainUnit"]
            unit_obj = problem.add_object("unit" + unit["id"], train_unit_type)
            id_to_unit[unit["id"]] = unit_obj
            unit_type_by_id[unit["id"]] = train_unit_type_key(unit)
            if composition_obj is None:
                problem.set_initial_value(available(unit_obj), True)
            else:
                problem.set_initial_value(part_of_composition(unit_obj, composition_obj), True)

    for request in all_train_requests(scenario_object):
        request_name = "request" + request["displayName"]
        request_obj = problem.add_object(request_name, departure_request_type)
        problem.set_initial_value(request_open(request_obj), True)

        for i, requested_unit in enumerate(request["trainUnits"]):
            slot_obj = problem.add_object(f"{request_name}_slot{i}", request_slot_type)
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
    
    writer = PDDLWriter(problem)
    writer.write_problem(output_file)

    if domain_file is not None:
        if os.sep not in domain_file:
            domain_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", domain_file)
        writer.write_domain(domain_file)


if __name__ == "__main__":
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level.upper())
    create_instance_from_scenario(domain_file=args.domain_file, path_to_folder=args.path_to_folder, scenario_file=args.scenario_file, location_file=args.location_file, output_file=args.output_file, coupling_mode=args.coupling_mode)
