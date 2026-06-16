"""
Interactive runner for the planning-approach pipeline.
Discover locations and scenarios, then convert to PDDL and/or run the planner.
"""
import os
import sys
import ast
import subprocess
import time
import questionary
from questionary import Style

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCENARIO_INPUTS = os.path.join(REPO_ROOT, "scenario-planning-inputs")
if not os.path.isdir(SCENARIO_INPUTS):
    SCENARIO_INPUTS = os.path.join(os.path.dirname(REPO_ROOT), "Robust-Rail-NL", "scenario-planning-inputs")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CONVERT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src", "convert")
PLANNER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src", "plan", "planner.jl")
PYTHON = sys.executable
JULIA = "julia"

SUBPROBLEM_CHOICES = {
    "Parking only": "parking",
    "Coupling / matching only": "matching",
    "Parking + coupling / matching": "combined",
}

COUPLING_MODE_CHOICES = {
    "Implicit coupling, free uncoupling": "implicit_free_uncoupling",
    "Implicit coupling, explicit uncoupling": "implicit_explicit_uncoupling",
    "Explicit coupling and explicit uncoupling": "explicit_coupling",
}

PLANNER_BACKEND_CHOICES = {
    "ENHSP via Julia": "enhsp",
    "SymbolicPlanners.jl A* HAdd": "symbolic",
}

STYLE = Style([
    ("qmark", "fg:#00aabb bold"),
    ("question", "bold"),
    ("answer", "fg:#00aabb bold"),
    ("pointer", "fg:#00aabb bold"),
    ("selected", "fg:#00aabb"),
    ("separator", "fg:#555555"),
])

VALIDATOR_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "src",
    "plan",
    "validate_plan.py"
)


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


def discover_convert_scripts():
    return sorted(
        f for f in os.listdir(CONVERT_DIR)
        if f.endswith(".py") and f.startswith("convert")
    )


def discover_convert_script_arguments(convert_script):
    """Return the long argparse flags declared by a converter script."""
    try:
        with open(convert_script, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=convert_script)
    except OSError:
        return set()

    arguments = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "add_argument":
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("--"):
                arguments.add(arg.value)
    return arguments


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

def run_convert(location, scenario_file, run_num, convert_script, subproblem="parking", coupling_mode="implicit_free_uncoupling"):
    location_dir = os.path.join(SCENARIO_INPUTS, location)
    scenario_name = scenario_file.replace(".json", "")
    problem_file, domain_file, _ = run_paths(location, scenario_name, run_num)

    os.makedirs(os.path.dirname(problem_file), exist_ok=True)

    print(f"\n  Converting  {scenario_file}")
    if needs_subproblem:
        print(f"  Subproblem ->  {subproblem}")
    if needs_coupling_mode and subproblem in ("matching", "combined"):
        print(f"  Coupling   ->  {coupling_mode}")
    print(f"  Problem    ->  {os.path.relpath(problem_file, REPO_ROOT)}")
    print(f"  Domain     ->  {os.path.relpath(domain_file, REPO_ROOT)}")

    command = [PYTHON, convert_script, "-p", location_dir, "-s", scenario_file, "-o", problem_file, "-d", domain_file]
    if needs_subproblem:
        command.extend(["--subproblem", subproblem])
    if needs_coupling_mode and subproblem in ("matching", "combined"):
        command.extend(["--coupling-mode", coupling_mode])

    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  [convert ERROR]\n{result.stderr}")
        return False
    print("  Done.")
    return True


def run_planner(location, scenario_name, run_num):
    problem_file, domain_file, plan_file = run_paths(location, scenario_name, run_num)

    if not os.path.exists(problem_file):
        print(f"  Problem file not found: {os.path.relpath(problem_file, REPO_ROOT)}")
        print("  Run 'Convert to PDDL' first.")
        return False
    if not os.path.exists(domain_file):
        print(f"  Domain file not found: {os.path.relpath(domain_file, REPO_ROOT)}")
        print("  Run 'Convert to PDDL' first.")
        return False

    print(f"\n  Planning  {os.path.relpath(problem_file, REPO_ROOT)}")
    print(f"  Domain    {os.path.relpath(domain_file, REPO_ROOT)}\n")
    print(f"  Planner   {planner_backend}\n")

    start = time.monotonic()
    process = subprocess.Popen(
        [JULIA, PLANNER_SCRIPT, domain_file, problem_file],
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

    # planner.jl writes to problem_file.replace(".pddl", ".plan") — that is already plan_file
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

    scenario_path = os.path.abspath(
        os.path.join(SCENARIO_INPUTS, location, "scenarios", scenario_file)
    )
    location_folder = os.path.abspath(
        os.path.join(SCENARIO_INPUTS, location)
    )
    plan_file = os.path.abspath(plan_file)
    eval_txt = os.path.abspath(eval_txt)

    if not os.path.exists(plan_file):
        print(f"  Plan file not found: {os.path.relpath(plan_file, REPO_ROOT)}")
        print("  Run the planner first.")
        return False

    if not os.path.isdir(location_folder):
        print(f"  Location folder not found: {location_folder}")
        return False

    if not os.path.exists(os.path.join(location_folder, "location.json")):
        print(f"  location.json not found in: {location_folder}")
        print("  --path_location must point to the folder containing location.json")
        return False

    if not os.path.exists(scenario_path):
        print(f"  Scenario file not found: {scenario_path}")
        return False

    os.makedirs(os.path.dirname(eval_txt), exist_ok=True)

    print(f"\n  Evaluating  {os.path.relpath(plan_file, REPO_ROOT)}")
    print(f"  Location    {location_folder}")
    print(f"  Scenario    {scenario_path}")
    print(f"  Result   →  {os.path.relpath(eval_txt, REPO_ROOT)}\n")

    env = os.environ.copy()


    if eval_proc.returncode != 0:
        print(f"  [evaluator ERROR] exit code {eval_proc.returncode}")

        if eval_proc.stdout:
            print("\n  [stdout]")
            print(eval_proc.stdout)

        if eval_proc.stderr:
            print("\n  [stderr]")
            print(eval_proc.stderr)

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
        choices=["Convert to PDDL", "Run planner", "Convert then plan"],
        style=STYLE,
    ).ask()
    if action is None:
        return

    scenario_name = scenario.replace(".json", "")
    print()

    if action in ("Convert to PDDL", "Convert then plan"):
        run_num = next_run_number(location, scenario_name)
        ok = run_convert(location, scenario, run_num)
        if not ok and action == "Convert then plan":
            print()
            return
        if action == "Convert then plan":
            run_planner(location, scenario_name, run_num)

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
        run_num = int(chosen[3:])
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