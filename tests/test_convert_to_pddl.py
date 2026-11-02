"""Unit tests for the scenario+location -> PDDL conversion stage, using the
simple-service fixture (tests/fixtures/simple_service/)."""


def test_produces_domain_and_problem_files(pddl_files):
    import os

    assert os.path.exists(pddl_files["domain"])
    assert os.path.exists(pddl_files["problem"])


def test_problem_declares_all_non_switch_track_parts(pddl_files):
    with open(pddl_files["problem"]) as f:
        problem_pddl = f.read()

    for track_name in ("rail_transit", "rail_service", "rail_park"):
        assert track_name in problem_pddl


def test_problem_wires_up_the_track_chain(pddl_files):
    with open(pddl_files["problem"]) as f:
        problem_pddl = f.read()

    assert "(connected_bside rail_transit rail_service)" in problem_pddl
    assert "(connected_bside rail_service rail_park)" in problem_pddl


def test_problem_marks_the_service_track_and_exit(pddl_files):
    with open(pddl_files["problem"]) as f:
        problem_pddl = f.read()

    assert "(service_allowed rail_service)" in problem_pddl
    assert "(facility_type rail_service cleaning)" in problem_pddl
    assert "(departure_exit_b rail_park)" in problem_pddl


def test_problem_goal_requires_the_single_departure(pddl_files):
    with open(pddl_files["problem"]) as f:
        problem_pddl = f.read()

    assert "(= (num_of_departed_trains) 1)" in problem_pddl
    assert "(request_departed request1)" in problem_pddl


# --- split-related converter behaviour -------------------------------------
# These build small variants of the simple-service fixture with a two-unit
# arrival (TestUnit 9101 + OtherUnit 9102) and inspect the generated PDDL text,
# so they need neither a planner nor the Julia backend.

import copy
import json

import pytest
import re

from convert_to_pddl.convert import create_instance_from_scenario

from conftest import LOCATION_FILE, SCENARIO_FILE

CLEANING = [{"type": {"other": "Cleaning"}, "duration": 300}]


def _request(request_id, unit_types):
    return {
        "id": request_id, "leaveTrackPart": 4, "lastParkingTrackPart": 3,
        "arrival": 1000 + request_id, "departure": 1000 + request_id,
        "standingIndex": None,
        "trainUnits": [
            {"typePrefix": t, "carriages": 1, "id": None, "tasks": None}
            for t in unit_types
        ],
    }


def _convert_two_unit_arrival(tmp_path, requests, first_unit_tasks=None):
    """Convert the fixture with a TestUnit+OtherUnit arrival and the given
    departure requests (a list of unit-type lists); return (problem, domain)."""
    with open(SCENARIO_FILE) as f:
        scenario = json.load(f)
    scenario["trainUnitTypes"].append(
        dict(scenario["trainUnitTypes"][0], typePrefix="OtherUnit")
    )
    scenario["in"][0]["members"] = [
        {"typePrefix": "TestUnit", "carriages": 1, "id": 9101,
         "tasks": copy.deepcopy(first_unit_tasks or [])},
        {"typePrefix": "OtherUnit", "carriages": 1, "id": 9102, "tasks": []},
    ]
    scenario["out"] = [_request(i + 1, types) for i, types in enumerate(requests)]
    scenario_file = tmp_path / "scenario.json"
    scenario_file.write_text(json.dumps(scenario))
    problem_file = tmp_path / "problem.pddl"
    domain_file = tmp_path / "domain.pddl"
    create_instance_from_scenario(
        location_file=LOCATION_FILE,
        scenario_file=str(scenario_file),
        domain_file=str(domain_file),
        output_file=str(problem_file),
    )
    return problem_file.read_text(), domain_file.read_text()


def test_split_off_unit_departing_alone_joins_the_departure_order(tmp_path):
    # Each unit leaves as its own single-unit request, so both only become
    # trains by being split off the arrival. Neither used to be chained into
    # the event order, so compiled_departure_ready could never reach them and
    # the problem was unsolvable.
    problem, _ = _convert_two_unit_arrival(tmp_path, [["TestUnit"], ["OtherUnit"]])

    assert "(next_after_arrival_to_departure su_train9001 su_unit9101)" in problem
    assert "(next_after_departure_to_departure su_unit9101 su_unit9102)" in problem


def test_serviced_train_may_split_before_departing_whole(tmp_path):
    problem, _ = _convert_two_unit_arrival(
        tmp_path, [["TestUnit", "OtherUnit"]], first_unit_tasks=CLEANING
    )

    # Adopting the arrival whole as the request stays possible ...
    assert "(compiled_whole_target su_train9001 su_request1)" in problem
    # ... but the train is not pinned together, so its units can be serviced alone.
    assert "(compiled_must_stay_coupled su_train9001)" not in problem


def test_unserviced_train_departing_whole_stays_coupled(tmp_path):
    # Control for the test above: without a service task nothing changes.
    problem, _ = _convert_two_unit_arrival(tmp_path, [["TestUnit", "OtherUnit"]])

    assert "(compiled_whole_target su_train9001 su_request1)" in problem
    assert "(compiled_must_stay_coupled su_train9001)" in problem


@pytest.mark.parametrize("side", ["front", "back"])
def test_uncouple_clears_whole_target_of_the_parent(tmp_path, side):
    # A parent that has split off a unit no longer holds the whole request, so
    # it must not be able to depart as that request: the plan would leave the
    # split-off unit behind (TORS: "No outgoing train found matching train IDs").
    _, domain = _convert_two_unit_arrival(
        tmp_path, [["TestUnit", "OtherUnit"]], first_unit_tasks=CLEANING
    )

    var = f"?compiled_uncouple_{side}_request_su"
    clears = (
        f"(forall ({var} - shuntingunit) "
        f"(when (compiled_whole_target ?parent_su {var}) "
        f"(not (compiled_whole_target ?parent_su {var}))))"
    )
    action = re.search(
        rf"\(:action compiled_uncouple_{side}\b.*?(?=\(:action |\Z)", domain, re.DOTALL
    ).group(0)
    assert clears in action
