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
EVALUATOR_ROOT = os.path.abspath(
    os.environ.get(
        "ROBUST_RAIL_EVALUATOR_ROOT",
        os.path.join(REPO_ROOT, "robust-rail-evaluator"),
    )
)

TORS_BIN = os.environ.get("TORS_BIN")
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

def find_tors_binary():
    """
    Find the robust-rail-evaluator CLI executable.

    Priority:
    1. TORS_BIN environment variable
    2. Common CMake output locations
    3. Any executable file under robust-rail-evaluator/build
    """
    if TORS_BIN:
        candidate = os.path.abspath(TORS_BIN)
        if os.path.isfile(candidate):
            return candidate

    candidates = [
        os.path.join(EVALUATOR_ROOT, "build", "TORS"),
        os.path.join(EVALUATOR_ROOT, "build", "cTORS", "TORS"),
        os.path.join(EVALUATOR_ROOT, "build", "cTORS", "tors"),
        os.path.join(EVALUATOR_ROOT, "build", "Release", "TORS"),
        os.path.join(EVALUATOR_ROOT, "build", "Debug", "TORS"),
    ]

    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    build_dir = os.path.join(EVALUATOR_ROOT, "build")
    if os.path.isdir(build_dir):
        for root, _, files in os.walk(build_dir):
            for name in files:
                path = os.path.join(root, name)
                if os.path.isfile(path) and os.access(path, os.X_OK):
                    # Prefer names that look like the evaluator binary.
                    if name.lower() in {"tors", "ctors"}:
                        return path

    return None

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

    tors_bin = find_tors_binary()

    if tors_bin is None:
        print("  TORS evaluator binary not found.")
        print(f"  Looked under: {EVALUATOR_ROOT}")
        print()
        print("  Build the evaluator first, for example:")
        print("    cd /workspace/robust-rail-evaluator")
        print("    mkdir -p build")
        print("    cd build")
        print('    cmake .. -DCONDA_ENV="$CONDA_PREFIX"')
        print("    cmake --build . -j")
        print()
        print("  Then check what was built:")
        print("    find /workspace/robust-rail-evaluator/build -type f -executable -ls")
        print()
        print("  If the executable has a different name, run this script with:")
        print("    export TORS_BIN=/full/path/to/the/executable")
        return False

    if not os.access(tors_bin, os.X_OK):
        print(f"  TORS exists but is not executable: {tors_bin}")
        print(f"  Try: chmod +x {tors_bin}")
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
    print(f"  TORS        {tors_bin}")
    print(f"  Location    {location_folder}")
    print(f"  Scenario    {scenario_path}")
    print(f"  Result   →  {os.path.relpath(eval_txt, REPO_ROOT)}\n")

    env = os.environ.copy()

    eval_proc = subprocess.run(
        [
            tors_bin,
            "--mode", "EVAL_AND_STORE",
            "--path_location", location_folder,
            "--path_scenario", scenario_path,
            "--path_plan", plan_file,
            "--path_eval_result", eval_txt,
            "--departure_delay", "0",
            "--plan_type", "Solver",
        ],
        cwd=EVALUATOR_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

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