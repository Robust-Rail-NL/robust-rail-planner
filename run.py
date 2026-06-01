"""
Interactive runner for the planning-approach pipeline.
Discover locations and scenarios, then convert to PDDL and/or run the planner.
"""
import os
import sys
import subprocess
import time
import questionary
from questionary import Style

REPO_ROOT       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCENARIO_INPUTS = os.path.join(REPO_ROOT, "scenario-planning-inputs")
DATA_DIR        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CONVERT_SCRIPT  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src", "convert", "v1", "convert_v1.py")
PLANNER_SCRIPT  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src", "plan", "planner.jl")
TORS_BIN        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "robust-rail-evaluator", "build", "TORS")
PYTHON          = sys.executable
JULIA           = "julia"

STYLE = Style([
    ("qmark",     "fg:#00aabb bold"),
    ("question",  "bold"),
    ("answer",    "fg:#00aabb bold"),
    ("pointer",   "fg:#00aabb bold"),
    ("selected",  "fg:#00aabb"),
    ("separator", "fg:#555555"),
])


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

def discover_locations():
    return sorted(
        d for d in os.listdir(SCENARIO_INPUTS)
        if os.path.isdir(os.path.join(SCENARIO_INPUTS, d)) and d.startswith("Location_")
    )


def discover_scenarios(location):
    scenario_dir = os.path.join(SCENARIO_INPUTS, location, "scenarios")
    if not os.path.isdir(scenario_dir):
        return []
    return sorted(
        f for f in os.listdir(scenario_dir)
        if f.startswith("scenario_solver_") and f.endswith(".json")
    )


def discover_runs(location, scenario_name):
    """Return existing run numbers for a given location + scenario, sorted ascending."""
    d = _run_dir(location, scenario_name)
    if not os.path.isdir(d):
        return []
    nums = []
    for f in os.listdir(d):
        if f.startswith("run") and f.endswith(".pddl") and not f.endswith("_domain.pddl"):
            try:
                nums.append(int(f[3:-5]))
            except ValueError:
                pass
    return sorted(nums)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _location_short(location):
    return location.replace("Location_", "")


def _run_dir(location, scenario_name):
    return os.path.join(DATA_DIR, _location_short(location), scenario_name)


def next_run_number(location, scenario_name):
    existing = discover_runs(location, scenario_name)
    return (existing[-1] + 1) if existing else 1


def run_paths(location, scenario_name, run_num):
    """Return (problem_pddl, domain_pddl, plan, eval_txt) paths for a given run."""
    d = _run_dir(location, scenario_name)
    base = os.path.join(d, f"run{run_num}")
    return base + ".pddl", base + "_domain.pddl", base + ".plan", base + "_eval.txt"


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def run_convert(location, scenario_file, run_num):
    location_dir  = os.path.join(SCENARIO_INPUTS, location)
    scenario_name = scenario_file.replace(".json", "")
    problem_file, domain_file, _, __ = run_paths(location, scenario_name, run_num)

    os.makedirs(os.path.dirname(problem_file), exist_ok=True)

    print(f"\n  Converting  {scenario_file}")
    print(f"  Problem  →  {os.path.relpath(problem_file, REPO_ROOT)}")
    print(f"  Domain   →  {os.path.relpath(domain_file,  REPO_ROOT)}")

    result = subprocess.run(
        [PYTHON, CONVERT_SCRIPT,
         "-p", location_dir,
         "-s", scenario_file,
         "-o", problem_file,
         "-d", domain_file],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  [convert ERROR]\n{result.stderr}")
        return False
    print("  Done.")
    return True


def run_planner(location, scenario_name, run_num):
    problem_file, domain_file, plan_file, _ = run_paths(location, scenario_name, run_num)

    if not os.path.exists(problem_file):
        print(f"  Problem file not found: {os.path.relpath(problem_file, REPO_ROOT)}")
        print("  Run 'Convert to PDDL' first.")
        return False
    if not os.path.exists(domain_file):
        print(f"  Domain file not found: {os.path.relpath(domain_file, REPO_ROOT)}")
        print("  Run 'Convert to PDDL' first.")
        return False

    print(f"\n  Planning  {os.path.relpath(problem_file, REPO_ROOT)}")
    print(f"  Domain    {os.path.relpath(domain_file,   REPO_ROOT)}\n")

    start = time.monotonic()
    process = subprocess.Popen(
        [JULIA, "--project", PLANNER_SCRIPT, domain_file, problem_file],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    for line in process.stdout:
        elapsed = time.monotonic() - start
        print(f"  [{elapsed:6.1f}s] {line}", end="", flush=True)
    process.wait()
    print(f"  [{time.monotonic() - start:6.1f}s] done (exit {process.returncode})")

    if process.returncode != 0:
        print("  [planner ERROR]")
        return False

    if os.path.exists(plan_file):
        print(f"\n  Plan written to {os.path.relpath(plan_file, REPO_ROOT)}")
        with open(plan_file) as f:
            steps = f.read().strip().splitlines()
        print(f"  Plan length: {len(steps)} steps")
        for i, step in enumerate(steps, 1):
            print(f"    {i:2}. {step}")
    return True


def run_evaluator(location, scenario_file, run_num):
    scenario_name = scenario_file.replace(".json", "")
    _, __, plan_file, eval_txt = run_paths(location, scenario_name, run_num)

    scenario_path = os.path.join(SCENARIO_INPUTS, location, "scenarios", scenario_file)
    location_folder = os.path.join(SCENARIO_INPUTS, location)

    if not os.path.exists(plan_file):
        print(f"  Plan file not found: {os.path.relpath(plan_file, REPO_ROOT)}")
        print("  Run the planner first.")
        return False

    if not os.path.exists(TORS_BIN):
        print(f"  TORS binary not found: {TORS_BIN}")
        print("  Build robust-rail-evaluator first.")
        return False

    print(f"\n  Evaluating  {os.path.relpath(plan_file, REPO_ROOT)}")
    print(f"  Scenario    {os.path.relpath(scenario_path, REPO_ROOT)}")
    print(f"  Result   →  {os.path.relpath(eval_txt, REPO_ROOT)}\n")

    eval_proc = subprocess.run([
        TORS_BIN,
        "--mode",             "EVAL_AND_STORE",
        "--path_location",    location_folder,
        "--path_scenario",    scenario_path,
        "--path_plan",        plan_file,
        "--path_eval_result", eval_txt,
        "--departure_delay",  "0",
        "--plan_type",        "Solver",
    ], capture_output=True, text=True)

    if eval_proc.returncode != 0:
        print(f"  [evaluator ERROR]\n{eval_proc.stderr}")
        return False

    print("  Evaluation passed.")
    if eval_proc.stdout:
        print(eval_proc.stdout)
    print(f"  Result written to {os.path.relpath(eval_txt, REPO_ROOT)}")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("\n  Robust-Rail Planning Pipeline\n")

    locations = discover_locations()
    if not locations:
        print("No locations found in scenario-planning-inputs/")
        return

    location = questionary.select("Location:", choices=locations, style=STYLE).ask()
    if location is None:
        return

    scenarios = discover_scenarios(location)
    if not scenarios:
        print(f"No solver scenarios found for {location}")
        return

    scenario = questionary.select("Scenario:", choices=scenarios, style=STYLE).ask()
    if scenario is None:
        return

    action = questionary.select(
        "Action:",
        choices=[
            "Convert to PDDL",
            "Run planner",
            "Evaluate plan",
            "Convert then plan",
            "Convert, plan, then evaluate",
        ],
        style=STYLE,
    ).ask()
    if action is None:
        return

    scenario_name = scenario.replace(".json", "")
    print()

    if action == "Convert to PDDL":
        run_num = next_run_number(location, scenario_name)
        run_convert(location, scenario, run_num)

    elif action == "Run planner":
        existing = discover_runs(location, scenario_name)
        if not existing:
            print(f"  No existing runs found for {scenario_name}.")
            print("  Run 'Convert to PDDL' first.")
            print()
            return
        chosen = questionary.select("Run:", choices=[f"run{n}" for n in existing], style=STYLE).ask()
        if chosen is None:
            return
        run_planner(location, scenario_name, int(chosen[3:]))

    elif action == "Evaluate plan":
        existing = discover_runs(location, scenario_name)
        if not existing:
            print(f"  No existing runs found for {scenario_name}.")
            print("  Run 'Convert to PDDL' and the planner first.")
            print()
            return
        chosen = questionary.select("Run:", choices=[f"run{n}" for n in existing], style=STYLE).ask()
        if chosen is None:
            return
        run_evaluator(location, scenario, int(chosen[3:]))

    elif action == "Convert then plan":
        run_num = next_run_number(location, scenario_name)
        ok = run_convert(location, scenario, run_num)
        if ok:
            run_planner(location, scenario_name, run_num)

    elif action == "Convert, plan, then evaluate":
        run_num = next_run_number(location, scenario_name)
        ok = run_convert(location, scenario, run_num)
        if ok:
            ok = run_planner(location, scenario_name, run_num)
        if ok:
            run_evaluator(location, scenario, run_num)

    print()


if __name__ == "__main__":
    main()