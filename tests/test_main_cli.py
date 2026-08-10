"""End-to-end test of main.py itself (the container entrypoint), run as a
subprocess exactly like the Dockerfile / real usage does, against the
simple-service fixture."""
import json
import os
import subprocess
import sys

from conftest import LOCATION_FILE, REPO_ROOT, SCENARIO_FILE, requires_julia


@requires_julia
def test_main_produces_a_valid_tors_plan(tmp_path):
    output_file = tmp_path / "plan.json"

    result = subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, "main.py"),
         "--location", LOCATION_FILE,
         "--scenario", SCENARIO_FILE,
         "--planner", "symbolic",
         "--output", str(output_file)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, result.stderr
    assert output_file.exists()

    with open(output_file) as f:
        tors_plan = json.load(f)

    assert tors_plan["actions"], "expected at least one action in the produced plan"
    assert tors_plan["actions"][0]["taskType"]["predefined"] == "Arrive"
    assert tors_plan["actions"][-1]["taskType"]["predefined"] == "Exit"


@requires_julia
def test_main_rejects_an_unsolvable_scenario(tmp_path):
    """The out request asks for a unit type that never arrives, so the
    planner must fail to find a plan and main.py must exit non-zero rather
    than silently emitting an empty/garbage TORS plan."""
    unsolvable_scenario = json.loads(open(SCENARIO_FILE).read())
    unsolvable_scenario["out"]["trainRequests"][0]["trainUnits"][0]["type"]["displayName"] = "NoSuchUnit"
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
