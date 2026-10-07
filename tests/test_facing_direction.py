"""Facing-direction tests for the compiled_matching model.

TORS refuses to combine two shunting units unless they entered their shared
track from the same side (CombineAction.cpp compares each unit's "previous"
track). The model mirrors that with the `came_from_a_side_su` fluent: moves set it
from the side they land on, coupling requires it to match, and an arriving unit
starts out facing the side its entryTrackPart plugs into.

These run the real converter rather than reusing conftest's `pddl_files`, which
drives the baseline_no_parameters variant.
"""

import json
import os
import re

import pytest

from convert_to_pddl.corridor_no_switch_unlimited_order_servicing_discrete_compiled_matching.convert import (
    create_instance_from_scenario,
)

FIXTURES_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "simple_service"
)
LOCATION_FILE = os.path.join(FIXTURES_DIR, "location.json")
SCENARIO_FILE = os.path.join(FIXTURES_DIR, "scenarios", "scenario_simple.json")

# Fixture track ids: 0=bumper_in, 1=rail_transit, 2=rail_service, 3=rail_park,
# 4=bumper_out. The arriving train rests on rail_park, whose aSide is
# [rail_service] and whose bSide is [bumper_out], so an entryTrackPart of 2
# means it entered on the a-side and 4 means the b-side.
RAIL_SERVICE = 2
RAIL_PARK = 3
BUMPER_OUT = 4

ARRIVING_SU = "su_train9001"

# The writer emits boolean fluents as bare atoms: listed in (:init) is True.
CAME_FROM_A_INIT = re.compile(r"\(came_from_a_side_su (\S+)\)")


def _convert(tmp_path, scenario_file=SCENARIO_FILE, location_file=LOCATION_FILE):
    domain_file = str(tmp_path / "domain.pddl")
    problem_file = str(tmp_path / "problem.pddl")
    create_instance_from_scenario(
        location_file=location_file,
        scenario_file=scenario_file,
        domain_file=domain_file,
        output_file=problem_file,
    )
    with open(domain_file) as f:
        domain = f.read()
    with open(problem_file) as f:
        problem = f.read()
    return domain, problem


def _convert_with_entry_track(tmp_path, entry_track_part):
    """Convert the fixture with the arriving train's entryTrackPart overridden."""
    with open(SCENARIO_FILE) as f:
        scenario = json.load(f)
    scenario["in"][0]["entryTrackPart"] = entry_track_part
    scenario_file = str(tmp_path / "scenario.json")
    with open(scenario_file, "w") as f:
        json.dump(scenario, f)
    return _convert(tmp_path, scenario_file=scenario_file)


@pytest.fixture(scope="module")
def compiled_matching_pddl(tmp_path_factory):
    return _convert(tmp_path_factory.mktemp("facing"))


def test_domain_declares_the_facing_fluent(compiled_matching_pddl):
    domain, _ = compiled_matching_pddl

    assert "(came_from_a_side_su ?shunting_unit - shuntingunit)" in domain


@pytest.mark.parametrize(
    "action",
    [
        "move_aside_empty_su",
        "move_aside_occupied_su",
        "move_bside_empty_su",
        "move_bside_occupied_su",
    ],
)
def test_every_move_sets_facing_from_the_side_it_lands_on(compiled_matching_pddl, action):
    domain, _ = compiled_matching_pddl
    body = _action_body(domain, action)

    assert (
        "(when (land_on_a ?l_from ?l_to) (came_from_a_side_su ?su))" in body
    ), f"{action} must set came_from_a_side_su when it lands on the a-side"
    assert (
        "(when (land_on_b ?l_from ?l_to) (not (came_from_a_side_su ?su)))" in body
    ), f"{action} must clear came_from_a_side_su when it lands on the b-side"


def test_coupling_requires_both_units_to_face_the_same_way(compiled_matching_pddl):
    domain, _ = compiled_matching_pddl
    body = _action_body(domain, "compiled_couple_front")

    assert (
        "(or (and (came_from_a_side_su ?request_su) (came_from_a_side_su ?source_su))"
        " (and (not (came_from_a_side_su ?request_su)) (not (came_from_a_side_su ?source_su))))"
    ) in body


def test_uncoupling_hands_the_facing_to_the_detached_unit(compiled_matching_pddl):
    domain, _ = compiled_matching_pddl

    for action in ("compiled_uncouple_front", "compiled_uncouple_back"):
        body = _action_body(domain, action)
        assert "(when (came_from_a_side_su ?parent_su) (came_from_a_side_su ?child_su))" in body
        assert "(when (not (came_from_a_side_su ?parent_su)) (not (came_from_a_side_su ?child_su)))" in body


def test_adopting_a_unit_onto_a_request_keeps_the_facing(compiled_matching_pddl):
    domain, _ = compiled_matching_pddl
    body = _action_body(domain, "compiled_adopt_composition")

    assert "(when (came_from_a_side_su ?source_su) (came_from_a_side_su ?request_su))" in body
    assert "(when (not (came_from_a_side_su ?source_su)) (not (came_from_a_side_su ?request_su)))" in body


def test_arrival_facing_follows_the_a_side_entry_track(tmp_path):
    _, problem = _convert_with_entry_track(tmp_path, RAIL_SERVICE)

    assert ARRIVING_SU in CAME_FROM_A_INIT.findall(problem)


def test_arrival_facing_follows_the_b_side_entry_track(tmp_path):
    _, problem = _convert_with_entry_track(tmp_path, BUMPER_OUT)

    assert ARRIVING_SU not in CAME_FROM_A_INIT.findall(problem)


def test_resting_on_the_arrival_track_still_reads_the_entry_track(tmp_path):
    """No redirect happens here: the train already rests where it arrived, so the
    facing has to come from entryTrackPart rather than from a move path."""
    _, problem = _convert_with_entry_track(tmp_path, RAIL_SERVICE)

    assert f"(su_arrival_track {ARRIVING_SU} rail_park)" in problem
    assert ARRIVING_SU in CAME_FROM_A_INIT.findall(problem)


@pytest.fixture(scope="module")
def multi_unit_pddl(tmp_path_factory):
    """A scenario with a multi-unit arrival, which is what makes the converter
    emit adopt_composition and the uncouple actions at all."""
    root = _kleinebinckhorst()
    scenario_file = os.path.join(root, "fixtures", "feasible", "scenario_marginal_length_s13.json")
    if not os.path.exists(scenario_file):
        pytest.skip("no multi-unit KleineBinckhorst scenario available")
    return _convert(
        tmp_path_factory.mktemp("facing_multi"),
        scenario_file=scenario_file,
        location_file=os.path.join(root, "location.json"),
    )


def test_adopting_a_unit_onto_a_request_keeps_the_facing(multi_unit_pddl):
    domain, _ = multi_unit_pddl
    body = _action_body(domain, "compiled_adopt_composition")

    assert "(when (came_from_a_side_su ?source_su) (came_from_a_side_su ?request_su))" in body
    assert "(when (not (came_from_a_side_su ?source_su)) (not (came_from_a_side_su ?request_su)))" in body


def test_starting_a_request_from_a_source_keeps_the_facing(multi_unit_pddl):
    domain, _ = multi_unit_pddl
    body = _action_body(domain, "compiled_start_request")

    assert "(when (came_from_a_side_su ?source_su) (came_from_a_side_su ?request_su))" in body
    assert "(when (not (came_from_a_side_su ?source_su)) (not (came_from_a_side_su ?request_su)))" in body


def test_redirected_arrival_lands_on_the_parkable_track(multi_unit_pddl):
    """906a is not parkable, so the arrival is redirected onto 906b and the facing
    has to come off the path between the two rather than off entryTrackPart."""
    _, problem = multi_unit_pddl

    assert "(su_arrival_track su_train0 o_906b)" in problem
    assert "su_train0" in CAME_FROM_A_INIT.findall(problem)


def _kleinebinckhorst():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for candidate in (
        os.environ.get("RRN_INPUTS_DIR"),
        os.path.join(repo_root, "robust-rail-general"),
        os.path.join(os.path.dirname(repo_root), "robust-rail-general"),
    ):
        if candidate and os.path.isdir(candidate):
            return os.path.join(os.path.abspath(candidate), "Location_KleineBinckhorst")
    pytest.skip("cannot find robust-rail-general; set RRN_INPUTS_DIR")


def _action_body(domain, name):
    start = domain.index(f":action {name}")
    end = domain.find(":action", start + 1)
    return domain[start:end if end != -1 else len(domain)]