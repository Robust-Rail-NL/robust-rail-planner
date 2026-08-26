import json
import os
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "simple_service")
# Migrated to the unified schema on 2026-08-10, and validated against the
# generator's schema_location/schema_scenario before being committed. They were
# location_solver.json / scenario_solver_simple.json in the pre-unification
# shape, which every converter in this repo would now reject.
#
# Kept in-repo rather than pointed at robust-rail-general: this is a
# deliberately minimal five-track corridor with one train and one request, and
# the assertions in test_convert_to_pddl.py name its tracks and goal directly.
# Real-world inputs are covered by test_plan_schema.py, which does read the
# sibling repo.
LOCATION_FILE = os.path.join(FIXTURES_DIR, "location.json")
SCENARIO_FILE = os.path.join(FIXTURES_DIR, "scenarios", "scenario_simple.json")

SYMBOLIC_PLANNER_SCRIPT = os.path.join(REPO_ROOT, "plan", "symbolic_planner.jl")
PLAN_PROJECT_DIR = os.path.join(REPO_ROOT, "plan")

sys.path.insert(0, REPO_ROOT)

def _julia_backend_available():
    """Whether the Julia planner can actually run, not merely whether julia exists.

    GitHub's ubuntu runners ship a julia on PATH, so a which() check passes there
    and every planner test then fails on `Package PDDL is required but does not
    seem to be installed`. What these tests need is the instantiated project in
    plan/, so that is what gets checked.
    """
    if shutil.which("julia") is None:
        return False
    probe = subprocess.run(
        ["julia", f"--project={PLAN_PROJECT_DIR}", "-e", "using PDDL, SymbolicPlanners"],
        capture_output=True,
        timeout=300,
    )
    return probe.returncode == 0


requires_julia = pytest.mark.skipif(
    not _julia_backend_available(),
    reason="the Julia planner backend is unavailable; run "
           "`julia --project=plan -e 'using Pkg; Pkg.instantiate()'`",
)


@pytest.fixture(scope="session")
def location_object():
    with open(LOCATION_FILE) as f:
        return json.load(f)


@pytest.fixture(scope="session")
def scenario_object():
    with open(SCENARIO_FILE) as f:
        return json.load(f)


@pytest.fixture(scope="session")
def pddl_files(tmp_path_factory):
    """Convert the simple-service fixture scenario+location into a PDDL domain/problem pair."""
    from convert_to_pddl.baseline_no_parameters.convert import create_instance_from_scenario

    out_dir = tmp_path_factory.mktemp("pddl")
    domain_file = str(out_dir / "domain.pddl")
    problem_file = str(out_dir / "problem.pddl")

    create_instance_from_scenario(
        location_file=LOCATION_FILE,
        scenario_file=SCENARIO_FILE,
        domain_file=domain_file,
        output_file=problem_file,
    )
    return {"domain": domain_file, "problem": problem_file}


@pytest.fixture(scope="session")
def raw_plan_file(pddl_files, tmp_path_factory):
    """Run the real Julia/SymbolicPlanners.jl backend on the fixture's PDDL problem."""
    if not _julia_backend_available():
        pytest.skip("the Julia planner backend is unavailable")

    out_dir = tmp_path_factory.mktemp("plan")
    plan_file = str(out_dir / "plan.plan")

    subprocess.run(
        ["julia", f"--project={PLAN_PROJECT_DIR}", SYMBOLIC_PLANNER_SCRIPT,
         pddl_files["domain"], pddl_files["problem"], plan_file],
        check=True,
        cwd=REPO_ROOT,
        timeout=180,
    )
    return plan_file


@pytest.fixture(scope="session")
def tors_plan(raw_plan_file):
    """Convert the planner's raw output into the final TORS JSON structure."""
    from convert_plan_to_tors.convert_to_tors import convert_plan

    return convert_plan(raw_plan_file, SCENARIO_FILE, LOCATION_FILE)
