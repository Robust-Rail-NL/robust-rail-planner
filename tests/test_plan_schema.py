"""The two halves of this repo's contract with the interchange schema.

It reads location and scenario JSON, and it writes plans. Neither side was
checked by anything until 2026-08-08, and both had drifted: the converter was
writing the plan format from before scenario unification, which the evaluator
rejects outright, and had been doing so since Phase 1.

These tests need no planner. The reading helpers and the action builders are
pure functions, so a plan can be assembled from them directly — which is the
only reason this is cheap enough to gate on. Running the real pipeline would
need Julia, a JDK and several minutes.
"""

import json
import os
from pathlib import Path

import pytest

from convert_plan_to_tors import convert_to_tors as C

REPO = Path(__file__).resolve().parent.parent


def _sibling(name, env_var):
    """Locate a sibling repo: an explicit path, a CI checkout, or a clone next door."""
    candidates = [
        os.environ.get(env_var),
        REPO / name,          # CI checks the repo out here
        REPO.parent / name,   # the usual local layout
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    raise RuntimeError(
        f"cannot find {name}. Set {env_var}, or clone it beside this repo. "
        "Deliberately an error rather than a skip: a silently skipped schema "
        "check is indistinguishable from a passing one."
    )


@pytest.fixture(scope="session")
def plan_schema():
    schema_dir = _sibling("robust-rail-generator", "RRN_GENERATOR_DIR") / "schema"
    return json.loads((schema_dir / "schema_plan.json").read_text())


@pytest.fixture(scope="session")
def inputs():
    """A real location and scenario, as the pipeline would be given them."""
    root = _sibling("scenario-planning-inputs", "RRN_INPUTS_DIR") / "Location_KleineBinckhorst"
    scenarios = sorted((root / "scenarios").glob("scenario_*.json"))
    assert scenarios, f"no scenarios under {root}"
    return (
        json.loads((root / "location.json").read_text()),
        json.loads(scenarios[0].read_text()),
    )


def test_reads_the_current_location_and_scenario(inputs):
    """The reading half: lookups build against today's unified inputs.

    Guards the drift that still affects pipeline.py, which reads
    location_solver.json and pairs scenario_solver_*.json with scenario_*.json —
    names and a two-file split that scenario unification removed.
    """
    location, scenario = inputs

    train_lookup = C.build_train_lookup(scenario)
    assert train_lookup, "no trains resolved; scenario shape has changed"
    assert C.build_track_lookup(location), "no tracks resolved"
    assert C.build_track_id_lookup(location), "no track ids resolved"

    # TrainRequest.id, formerly displayName.
    if scenario.get("out"):
        assert C.build_request_lookup(scenario), "no departure requests resolved"


def _one_of_every_action(location, scenario):
    train_lookup = C.build_train_lookup(scenario)
    unit_lookup = C.build_unit_lookup(scenario)
    track_lookup = C.build_track_lookup(location)
    track_id_lookup = C.build_track_id_lookup(location)

    train = next(k for k in train_lookup if k.startswith("train"))
    tracks = [tp["id"] for tp in location["trackParts"]][:3]
    here = C._as_id(tracks[0])

    actions = [
        C.create_arrive_action(train, 100, tracks[0], train_lookup, track_lookup,
                               unit_lookup, track_id_lookup=track_id_lookup),
        C.create_arrive_action(train, 100, tracks[0], train_lookup, track_lookup,
                               unit_lookup, standing_type="InStanding",
                               track_id_lookup=track_id_lookup),
        C.create_exit_action(train, 900, tracks[0], train_lookup, track_lookup,
                             unit_lookup, track_id_lookup=track_id_lookup),
        C.create_exit_action(train, 900, tracks[0], train_lookup, track_lookup,
                             unit_lookup, standing_type="OutStanding",
                             track_id_lookup=track_id_lookup),
        C.create_move_action(train, tracks[0], tracks[1], tracks, train_lookup,
                             track_id_lookup, unit_lookup),
        C.create_wait_action(train, 300, 400, here, train_lookup, unit_lookup),
        C.create_split_action(train, ["1", "2"], 500, 560, here, train_lookup, unit_lookup),
        C.create_service_action(train, 600, 700, here, 3, "Cleaning",
                                train_lookup, unit_lookup),
    ]
    actions += C.create_combine_action([train], "7", 800, 860, here,
                                       train_lookup, unit_lookup)[0]
    return {"schemaVersion": C.SCHEMA_VERSION, "actions": actions}


def test_every_action_kind_validates(plan_schema, inputs):
    """The writing half: one of each action, against the real schema."""
    jsonschema = pytest.importorskip("jsonschema")
    location, scenario = inputs
    plan = _one_of_every_action(location, scenario)

    validator = jsonschema.Draft202012Validator(plan_schema)
    errors = sorted(validator.iter_errors(plan), key=lambda e: list(e.absolute_path))
    assert not errors, "\n".join(
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in errors[:10]
    )


def test_standing_units_use_the_stand_task_types(inputs):
    """standingType is gone; StandIn and StandOut carry that meaning now.

    Worth its own test because deleting the field without translating it would
    still validate — it would just silently lose which units were already in
    the yard.
    """
    location, scenario = inputs
    kinds = {
        a["taskType"].get("predefined")
        for a in _one_of_every_action(location, scenario)["actions"]
    }
    assert {"StandIn", "StandOut"} <= kinds
    assert all(
        "standingType" not in a["shuntingUnit"]
        for a in _one_of_every_action(location, scenario)["actions"]
    )


def test_no_fabricated_arrive_for_non_scenario_su():
    """Arrive/StandIn are only emitted for SUs that are real scenario trains.

    SUs that the planner merely materializes (compiled adopt/start/couple
    request placeholders) never appear in the scenario's in/inStanding lists.
    Fabricating an Arrive for them makes TORS re-add trains that were already
    added by their real arrival, and the second AddShuntingUnit throws.
    """
    root = _sibling("scenario-planning-inputs", "RRN_INPUTS_DIR") / "Location_KleineBinckhorst"
    location = json.loads((root / "location.json").read_text())
    scenario = json.loads(
        (root / "scenarios" / "scenario_KleineBinckhorst_6t_custom_example3.json").read_text()
    )

    train_lookup = C.build_train_lookup(scenario)
    unit_lookup = C.build_unit_lookup(scenario)
    track_lookup = C.build_track_lookup(location)
    track_id_lookup = C.build_track_id_lookup(location)

    real_train_id = scenario["in"][0]["id"]
    phantom_id = scenario["out"][0]["id"]
    assert phantom_id != real_train_id

    def su(id_):
        return {"id": id_, "memberIDs": [id_], "parentIDs": [], "childIDs": []}

    def move(id_, start, end, track):
        return {
            "startTime": start,
            "endTime": end,
            "taskType": {"predefined": "Move"},
            "shuntingUnit": su(id_),
            "location": track,
            "resources": [
                {"kind": "trackPart", "id": track},
                {"kind": "trackPart", "id": track},
            ],
        }

    actions = [
        move(real_train_id, 600, 900, 42),
        move(phantom_id, 3600, 3900, 57),
    ]

    processed = C.post_process_actions(
        actions, train_lookup, unit_lookup, track_lookup, track_id_lookup,
        {}, {real_train_id: 600}, scenario, su_id_fn=C._as_id,
    )

    phantom_tasks = [
        a["taskType"]["predefined"]
        for a in processed
        if a["shuntingUnit"]["id"] == phantom_id
    ]
    assert phantom_tasks == ["Move"], "phantom SU got an Arrive: " + str(phantom_tasks)

    arrivals = [
        a for a in processed
        if a["taskType"]["predefined"] in ("Arrive", "StandIn")
    ]
    assert len(arrivals) == 1, f"expected exactly one Arrive, got {len(arrivals)}"
    assert arrivals[0]["shuntingUnit"]["id"] == real_train_id
    assert arrivals[0]["startTime"] == 600
