import os
import sys
import json
import logging
import argparse
import unified_planning.shortcuts as up
from unified_planning.io import PDDLReader, PDDLWriter

parser = argparse.ArgumentParser()
parser.add_argument("-p", "--path-to-folder", required=False, default=None)
parser.add_argument("-s", "--scenario-file", required=False, default="scenario_solver_example1.json")
parser.add_argument("-l", "--location-file", required=False, default="location_solver.json")
parser.add_argument("-o", "--output-file", required=False, default=None)
parser.add_argument("-d", "--domain-file", required=False, default=None)
parser.add_argument("--coupling-mode", choices=["implicit_free_uncoupling", "implicit_explicit_uncoupling", "explicit_coupling"], default="implicit_free_uncoupling", required=False)
parser.add_argument("--log-level", default="ERROR", required=False)


def train_unit_type_key(train_unit):
    unit_type = train_unit["type"]
    return (
        unit_type.get("displayName"),
        int(unit_type.get("carriages", 0)),
        float(unit_type.get("length", 0.0)),
    )


def type_key_to_pddl_name(key):
    return f"type_{key[0].replace('-','_').replace(' ','_')}_{key[1]}_{int(key[2])}"


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
    if path_to_folder is None:
        path_to_folder = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "scenario-planning-inputs", "Location_KleineBinckhorst")

    if location_file is None:
        location_file = os.path.join(path_to_folder, "location_solver.json")
    elif not os.sep in location_file:
        location_file = os.path.join(path_to_folder, location_file)

    if not os.sep in scenario_file:
        scenario_name = scenario_file.replace(".json", "")
        scenario_file = os.path.join(path_to_folder, "scenarios", scenario_file)
    else:
        scenario_name = scenario_file.split(os.sep)[-1].replace(".json", "")

    location_object = json.load(open(location_file))
    scenario_object = json.load(open(scenario_file))

    problem = up.Problem(scenario_name)

    # --- TYPES ---
    # track_part_type = up.UserType("trackpart")
    train_unit_type = up.UserType("trainunit")
    arrival_train_type = up.UserType("arrivaltrain")
    departure_request_type = up.UserType("departurerequest")
    request_slot_type = up.UserType("requestslot")
    # arrival_composition_type = up.UserType("arrivalcomposition")
    unit_type_pddl = up.UserType("unittype")

    # --- FLUENTS ---
    # free = problem.add_fluent(up.Fluent("free", up.BoolType(), trackpart=track_part_type), default_initial_value=True)
    arrival = problem.add_fluent(up.Fluent("arrival", up.IntType(), train=arrival_train_type))
    # at = problem.add_fluent(up.Fluent("at", ...), default_initial_value=False)
    # parking_allowed = problem.add_fluent(up.Fluent("parking_allowed", ...), default_initial_value=False)
    # parked = problem.add_fluent(up.Fluent("parked", ...), default_initial_value=False)

    available = problem.add_fluent(up.Fluent("available", up.BoolType(), unit=train_unit_type), default_initial_value=False)
    request_open = problem.add_fluent(up.Fluent("request_open", up.BoolType(), request=departure_request_type), default_initial_value=False)
    slot_open = problem.add_fluent(up.Fluent("slot_open", up.BoolType(), slot=request_slot_type), default_initial_value=False)
    slot_filled = problem.add_fluent(up.Fluent("slot_filled", up.BoolType(), slot=request_slot_type), default_initial_value=False)
    compatible = problem.add_fluent(up.Fluent("compatible", up.BoolType(), unit=train_unit_type, slot=request_slot_type), default_initial_value=False)
    matched = problem.add_fluent(up.Fluent("matched", up.BoolType(), unit=train_unit_type, slot=request_slot_type), default_initial_value=False)
    slot_for_request = problem.add_fluent(up.Fluent("slot_for_request", up.BoolType(), slot=request_slot_type, request=departure_request_type), default_initial_value=False)

    has_type = problem.add_fluent(up.Fluent("has_type", up.BoolType(), unit=train_unit_type, utype=unit_type_pddl), default_initial_value=False)
    requires_type = problem.add_fluent(up.Fluent("requires_type", up.BoolType(), slot=request_slot_type, utype=unit_type_pddl), default_initial_value=False)
    member_of_arrival = problem.add_fluent(up.Fluent("member_of_arrival", up.BoolType(), unit=train_unit_type, train=arrival_train_type), default_initial_value=False)
    same_arrival = problem.add_fluent(up.Fluent("same_arrival", up.BoolType(), unit_a=train_unit_type, unit_b=train_unit_type), default_initial_value=False)
    departure_time = problem.add_fluent(up.Fluent("departure", up.IntType(), request=departure_request_type))
    arrival_ready = problem.add_fluent(up.Fluent("arrival_ready", up.BoolType(), unit=train_unit_type, request=departure_request_type), default_initial_value=False)
    request_fulfilled = problem.add_fluent(up.Fluent("request_fulfilled", up.BoolType(), request=departure_request_type), default_initial_value=False)
    slots_filled_count = problem.add_fluent(up.Fluent("slots_filled_count", up.IntType(), request=departure_request_type))
    slots_total = problem.add_fluent(up.Fluent("slots_total", up.IntType(), request=departure_request_type))
    no_split_needed = problem.add_fluent(up.Fluent("no_split_needed", up.BoolType(), request=departure_request_type), default_initial_value=False)

    # Ordering: first_slot(slot) marks slot 0 of each request — no predecessor, always matchable.
    # prev_slot(slot, prev) links slot i to slot i-1 within the same request.
    # In match: slot 0 can always be matched; slot i>0 requires slot_filled(prev).
    # No extra actions needed — ordering is enforced entirely by match preconditions.
    first_slot = problem.add_fluent(up.Fluent("first_slot", up.BoolType(), slot=request_slot_type), default_initial_value=False)
    prev_slot = problem.add_fluent(up.Fluent("prev_slot", up.BoolType(), slot=request_slot_type, prev=request_slot_type), default_initial_value=False)

    # --- COUPLING/UNCOUPLING ---
    explicit_uncoupling = coupling_mode in ["implicit_explicit_uncoupling", "explicit_coupling"]
    explicit_coupling = coupling_mode == "explicit_coupling"

    if explicit_uncoupling:
        # part_of_composition = problem.add_fluent(...)
        # composition_needs_uncoupling = problem.add_fluent(...)
        # uncouple = up.InstantaneousAction("uncouple", ...)
        pass

    if explicit_coupling:
        # slot_coupled = problem.add_fluent(...)
        # coupled_to_request = problem.add_fluent(...)
        # couple_to_request = up.InstantaneousAction("couple_to_request", ...)
        pass

    # --- ACTIONS ---

    # match_first: assigns a unit to slot 0 of a request (no ordering constraint needed)
    match_first = up.InstantaneousAction("match_first", unit=train_unit_type, slot=request_slot_type, request=departure_request_type)
    match_first.add_precondition(available(match_first.unit))
    match_first.add_precondition(slot_open(match_first.slot))
    match_first.add_precondition(compatible(match_first.unit, match_first.slot))
    match_first.add_precondition(slot_for_request(match_first.slot, match_first.request))
    match_first.add_precondition(request_open(match_first.request))
    match_first.add_precondition(arrival_ready(match_first.unit, match_first.request))
    match_first.add_precondition(first_slot(match_first.slot))
    match_first.add_effect(matched(match_first.unit, match_first.slot), True)
    match_first.add_effect(slot_filled(match_first.slot), True)
    match_first.add_effect(available(match_first.unit), False)
    match_first.add_effect(slot_open(match_first.slot), False)
    match_first.add_increase_effect(slots_filled_count(match_first.request), 1)
    problem.add_action(match_first)

    # match_next: assigns a unit to slot i>0, requires the previous slot to already be filled
    match_next = up.InstantaneousAction("match_next", unit=train_unit_type, slot=request_slot_type, request=departure_request_type, predecessor=request_slot_type)
    match_next.add_precondition(available(match_next.unit))
    match_next.add_precondition(slot_open(match_next.slot))
    match_next.add_precondition(compatible(match_next.unit, match_next.slot))
    match_next.add_precondition(slot_for_request(match_next.slot, match_next.request))
    match_next.add_precondition(request_open(match_next.request))
    match_next.add_precondition(arrival_ready(match_next.unit, match_next.request))
    match_next.add_precondition(prev_slot(match_next.slot, match_next.predecessor))
    match_next.add_precondition(slot_filled(match_next.predecessor))
    match_next.add_effect(matched(match_next.unit, match_next.slot), True)
    match_next.add_effect(slot_filled(match_next.slot), True)
    match_next.add_effect(available(match_next.unit), False)
    match_next.add_effect(slot_open(match_next.slot), False)
    match_next.add_increase_effect(slots_filled_count(match_next.request), 1)
    problem.add_action(match_next)

    close_request = up.InstantaneousAction("close_request", request=departure_request_type)
    close_request.add_precondition(request_open(close_request.request))
    close_request.add_precondition(up.Equals(slots_filled_count(close_request.request), slots_total(close_request.request)))
    close_request.add_effect(request_open(close_request.request), False)
    close_request.add_effect(request_fulfilled(close_request.request), True)
    problem.add_action(close_request)

    # park = up.InstantaneousAction('park', ...)
    # problem.add_action(park)

    # --- TRACK PARTS ---
    # id_to_track_part = {}
    # for track_part in location_object["trackParts"]:
    #     ...

    type_key_to_obj = {}
    for train in all_trains(scenario_object):
        for trainunit in train["members"]:
            key = train_unit_type_key(trainunit["trainUnit"])
            if key not in type_key_to_obj:
                type_key_to_obj[key] = problem.add_object(type_key_to_pddl_name(key), unit_type_pddl)
    for request in all_train_requests(scenario_object):
        for requested_unit in request["trainUnits"]:
            key = train_unit_type_key(requested_unit)
            if key not in type_key_to_obj:
                type_key_to_obj[key] = problem.add_object(type_key_to_pddl_name(key), unit_type_pddl)

    id_to_unit = {}
    unit_type_by_id = {}
    unit_arrival_by_id = {}
    train_to_units = {}

    for train in all_trains(scenario_object):
        arrival_train = problem.add_object("train" + train["id"], arrival_train_type)
        arr_time = int(train["arrival"])
        problem.set_initial_value(arrival(arrival_train), up.Int(arr_time))
        # problem.set_initial_value(at(arrival_train, ...), True)

        train_to_units[train["id"]] = []
        # composition_obj = None
        # if explicit_uncoupling and len(train["members"]) > 1: ...

        for trainunit in train["members"]:
            unit = trainunit["trainUnit"]
            unit_obj = problem.add_object("unit" + unit["id"], train_unit_type)
            key = train_unit_type_key(unit)
            id_to_unit[unit["id"]] = unit_obj
            unit_type_by_id[unit["id"]] = key
            unit_arrival_by_id[unit["id"]] = arr_time
            train_to_units[train["id"]].append(unit["id"])

            # if composition_obj is None:
            problem.set_initial_value(available(unit_obj), True)
            # else:
            #     problem.set_initial_value(part_of_composition(unit_obj, composition_obj), True)

            problem.set_initial_value(member_of_arrival(unit_obj, arrival_train), True)
            problem.set_initial_value(has_type(unit_obj, type_key_to_obj[key]), True)

    for train_id, unit_ids in train_to_units.items():
        for uid_a in unit_ids:
            for uid_b in unit_ids:
                if uid_a != uid_b:
                    problem.set_initial_value(same_arrival(id_to_unit[uid_a], id_to_unit[uid_b]), True)

    for request in all_train_requests(scenario_object):
        request_name = "request" + request["displayName"]
        request_obj = problem.add_object(request_name, departure_request_type)
        problem.set_initial_value(request_open(request_obj), True)
        problem.set_initial_value(request_fulfilled(request_obj), False)

        dep_time = int(request.get("departure", 0))
        problem.set_initial_value(departure_time(request_obj), up.Int(dep_time))
        problem.set_initial_value(slots_total(request_obj), up.Int(len(request["trainUnits"])))
        problem.set_initial_value(slots_filled_count(request_obj), up.Int(0))

        request_type_counts = {}
        for requested_unit in request["trainUnits"]:
            k = train_unit_type_key(requested_unit)
            request_type_counts[k] = request_type_counts.get(k, 0) + 1
        for train_id, unit_ids in train_to_units.items():
            available_counts = {}
            for uid in unit_ids:
                k = unit_type_by_id[uid]
                available_counts[k] = available_counts.get(k, 0) + 1
            if all(available_counts.get(k, 0) >= v for k, v in request_type_counts.items()):
                problem.set_initial_value(no_split_needed(request_obj), True)
                break

        slot_objects = []
        for i, requested_unit in enumerate(request["trainUnits"]):
            slot_obj = problem.add_object(f"{request_name}_slot{i}", request_slot_type)
            requested_key = train_unit_type_key(requested_unit)

            problem.set_initial_value(slot_open(slot_obj), True)
            problem.set_initial_value(slot_for_request(slot_obj, request_obj), True)

            if i == 0:
                # Slot 0: mark as first slot — matched by match_first, no predecessor needed
                problem.set_initial_value(first_slot(slot_obj), True)
            else:
                # Slot i>0: record its predecessor — matched by match_next after predecessor is filled
                problem.set_initial_value(prev_slot(slot_obj, slot_objects[i - 1]), True)

            # if explicit_coupling:
            #     problem.add_goal(slot_coupled(slot_obj))
            # else:
            problem.add_goal(slot_filled(slot_obj))

            for unit_id, unit_obj in id_to_unit.items():
                if unit_type_by_id[unit_id] == requested_key:
                    problem.set_initial_value(compatible(unit_obj, slot_obj), True)

            problem.set_initial_value(requires_type(slot_obj, type_key_to_obj[requested_key]), True)

            for unit_id, unit_obj in id_to_unit.items():
                if unit_arrival_by_id[unit_id] <= dep_time:
                    problem.set_initial_value(arrival_ready(unit_obj, request_obj), True)

            slot_objects.append(slot_obj)

        problem.add_goal(request_fulfilled(request_obj))
        problem.add_goal(no_split_needed(request_obj))

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