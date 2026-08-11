"""End-to-end test of main.py itself (the container entrypoint), run as a
subprocess exactly like the Dockerfile / real usage does, against the
simple-service fixture."""
import json
import os
import subprocess
import sys

import pytest

from conftest import LOCATION_FILE, REPO_ROOT, SCENARIO_FILE, requires_julia


def _run_main(output_file, scenario=SCENARIO_FILE):
    return subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, "main.py"),
         "--location", LOCATION_FILE,
         "--scenario", scenario,
         "--planner", "symbolic",
         "--output", str(output_file)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )


@requires_julia
def test_main_produces_a_valid_tors_plan(tmp_path):
    output_file = tmp_path / "plan.json"
    result = _run_main(output_file)

    assert result.returncode == 0, result.stderr
    assert output_file.exists()

    with open(output_file) as f:
        tors_plan = json.load(f)

    assert tors_plan["actions"], "expected at least one action in the produced plan"
    assert tors_plan["actions"][0]["taskType"]["predefined"] == "Arrive"


@requires_julia
def test_main_output_validates_against_the_plan_schema(tmp_path):
    """The container's output has to satisfy the schema the evaluator reads.

    This is the assertion the whole migration exists for: the converter used to
    emit the pre-unification shape (members, standingType, string ids), which
    the evaluator rejects outright.
    """
    jsonschema = pytest.importorskip("jsonschema")
    from test_plan_schema import _sibling  # the shared sibling-repo lookup

    output_file = tmp_path / "plan.json"
    result = _run_main(output_file)
    assert result.returncode == 0, result.stderr

    schema_dir = _sibling("robust-rail-generator", "RRN_GENERATOR_DIR") / "schema"
    schema = json.loads((schema_dir / "schema_plan.json").read_text())
    plan = json.loads(output_file.read_text())

    errors = sorted(
        jsonschema.Draft202012Validator(schema).iter_errors(plan),
        key=lambda e: list(e.absolute_path),
    )
    assert not errors, "\n".join(
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in errors[:10]
    )


@requires_julia
def test_main_plan_ends_with_an_exit(tmp_path):
    """A plan that stops before the departure is not a solution.

    Was a strict xfail until the converter learned the corridor model's
    compiled_depart_*_for_request: that action matched no pattern, so the
    departure was dropped and the two moves after the service task went with it,
    while conversion still reported success.
    """
    output_file = tmp_path / "plan.json"
    assert _run_main(output_file).returncode == 0

    tors_plan = json.loads(output_file.read_text())
    assert tors_plan["actions"][-1]["taskType"].get("predefined") == "Exit"


@requires_julia
@pytest.mark.xfail(
    reason="The PDDL model sequences actions and costs movement, but nothing "
           "ties a departure to the request's `departure` time, so the planner "
           "has no reason to be punctual. On this fixture the Exit lands at "
           "1803 against a requested 1000. Measured the same way on both real "
           "locations, where the evaluator rejects with \"Trains's departure "
           "mismatch with Action start/end time\": SimpleService 2902 vs 2000, "
           "KleineBinckhorst 6301 vs 5400. This is the cheap local proxy for a "
           "defect that otherwise needs a tors run. See SCHEMA_STATUS.md.",
    strict=True,
)
def test_plan_meets_its_departure_deadlines(tmp_path):
    """No train should leave later than the request asked for.

    Compares the latest Exit against the latest requested departure rather than
    pairing each train with its own request: matching a shunting unit back to a
    request needs the unit lookup, and this fixture has a single request, so the
    two formulations coincide here. If a multi-request fixture is added, tighten
    this to a per-request comparison.
    """
    output_file = tmp_path / "plan.json"
    assert _run_main(output_file).returncode == 0

    plan = json.loads(output_file.read_text())
    scenario = json.loads(open(SCENARIO_FILE).read())

    exits = [a for a in plan["actions"] if a["taskType"].get("predefined") == "Exit"]
    assert exits, "no Exit action to check"

    latest_exit = max(int(a["startTime"]) for a in exits)
    latest_requested = max(int(r["departure"]) for r in scenario["out"])
    assert latest_exit <= latest_requested, (
        f"last train leaves at {latest_exit}, "
        f"{latest_exit - latest_requested}s after the requested {latest_requested}"
    )


@requires_julia
def test_main_rejects_an_unsolvable_scenario(tmp_path):
    """The out request asks for a unit type that never arrives, so the
    planner must fail to find a plan and main.py must exit non-zero rather
    than silently emitting an empty/garbage TORS plan."""
    unsolvable_scenario = json.loads(open(SCENARIO_FILE).read())
    # out is a bare list since the unification, and a unit's type is the
    # (typePrefix, carriages) pair rather than a nested type.displayName.
    unsolvable_scenario["out"][0]["trainUnits"][0]["typePrefix"] = "NoSuchUnit"
    scenario_path = tmp_path / "scenario_unsolvable.json"
    scenario_path.write_text(json.dumps(unsolvable_scenario))

    output_file = tmp_path / "plan.json"

    result = subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, "main.py"),
         "--location", LOCATION_FILE,
         "--scenario", str(scenario_path),
         "--planner", "symbolic",
         "--output", str(output_file)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode != 0
    assert not output_file.exists()
