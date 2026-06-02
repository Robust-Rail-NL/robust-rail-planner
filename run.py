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

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCENARIO_INPUTS = os.path.join(REPO_ROOT, "scenario-planning-inputs")
if not os.path.isdir(SCENARIO_INPUTS):
    SCENARIO_INPUTS = os.path.join(os.path.dirname(REPO_ROOT), "Robust-Rail-NL", "scenario-planning-inputs")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_CONVERT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src", "convert")
CONVERT_SCRIPT = os.path.join(_CONVERT_DIR, "convert.py")
PLANNER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src", "plan", "planner.jl")
PYTHON = sys.executable
JULIA = "julia"


def _find_enhsp_jar():
    """Return path to enhsp.jar, checking the venv-installed up_enhsp package first."""
    import importlib.util
    spec = importlib.util.find_spec("up_enhsp")
    if spec:
        candidate = os.path.join(os.path.dirname(spec.origin), "ENHSP", "enhsp.jar")
        if os.path.isfile(candidate):
            return candidate
    return None


def _find_java17():
    """Return path to a Java 17+ executable, or None if not found."""
    java_home = os.environ.get("JAVA_HOME", "")
    candidates = [
        os.path.join(java_home, "bin", "java") if java_home else None,
        "/opt/homebrew/opt/openjdk@17/bin/java",
        "/usr/local/opt/openjdk@17/bin/java",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None



def discover_converters():
    """Return an ordered dict of label -> path for all available converters."""
    converters = {}
    root_script = os.path.join(_CONVERT_DIR, "convert.py")
    if os.path.isfile(root_script):
        converters["convert.py  (root)"] = root_script
    for name in sorted(os.listdir(_CONVERT_DIR)):
        subdir = os.path.join(_CONVERT_DIR, name)
        candidate = os.path.join(subdir, "convert.py")
        if os.path.isdir(subdir) and os.path.isfile(candidate):
            converters[name] = candidate
    return converters


PLANNER_BACKEND_CHOICES = {
    "ENHSP via Julia": "enhsp",
    "SymbolicPlanners.jl A* HAdd": "symbolic",
}

ACTION_COSTS = {
    "move_aside_empty": 300,
    "move_aside_occupied": 300,
    "move_bside_empty": 300,
    "move_bside_occupied": 300,
    "wait": 300,
    "uncouple": 120,
    "couple_two_units": 180,
    "couple_two_units_same_train": 180,
}


def _action_cost(step):
    name = step.split("(")[0].strip().lower()
    return ACTION_COSTS.get(name, 0)


STYLE = Style([
    ("qmark", "fg:#00aabb bold"),
    ("question", "bold"),
    ("answer", "fg:#00aabb bold"),
    ("pointer", "fg:#00aabb bold"),
    ("selected", "fg:#00aabb"),
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
    """Return (problem_pddl, domain_pddl, plan) paths for a given run."""
    d = _run_dir(location, scenario_name)
    base = os.path.join(d, f"run{run_num}")
    return base + ".pddl", base + "_domain.pddl", base + ".plan"


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def run_convert(location, scenario_file, run_num, convert_script=None):
    if convert_script is None:
        convert_script = CONVERT_SCRIPT
    location_dir = os.path.join(SCENARIO_INPUTS, location)
    scenario_name = scenario_file.replace(".json", "")
    problem_file, domain_file, _ = run_paths(location, scenario_name, run_num)

    os.makedirs(os.path.dirname(problem_file), exist_ok=True)

    print(f"\n  Converter   {os.path.relpath(convert_script, os.path.dirname(os.path.abspath(__file__)))}")
    print(f"  Converting  {scenario_file}")
    print(f"  Problem    ->  {os.path.relpath(problem_file, REPO_ROOT)}")
    print(f"  Domain     ->  {os.path.relpath(domain_file, REPO_ROOT)}")

    result = subprocess.run(
        [PYTHON, convert_script,
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


def run_planner(location, scenario_name, run_num, planner_backend="enhsp"):
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

    # Stream Julia output line-by-line with elapsed timestamps so slow searches are visible.
    env = os.environ.copy()
    if planner_backend == "enhsp":
        jar = _find_enhsp_jar()
        if jar:
            env["ENHSP_JAR"] = jar
        else:
            print("  [WARNING] ENHSP jar not found. Install with: pip install up-enhsp")
        if "JAVA_EXE" not in env:
            java = _find_java17()
            if java:
                env["JAVA_EXE"] = java
            else:
                print("  [WARNING] Java 17 not found. Set JAVA_HOME or install with: brew install openjdk@17")

    start = time.monotonic()
    process = subprocess.Popen(
        [JULIA, "--project=" + os.path.dirname(os.path.abspath(__file__)), PLANNER_SCRIPT, domain_file, problem_file, planner_backend],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
    )
    for line in process.stdout:
        elapsed = time.monotonic() - start
        print(f"  [{elapsed:6.1f}s] {line}", end="", flush=True)
    process.wait()
    print(f"  [{time.monotonic() - start:6.1f}s] done (exit {process.returncode})")

    if process.returncode != 0:
        print("  [planner ERROR]")
        return False

    # planner.jl writes to problem_file.replace(".pddl", ".plan"); that is already plan_file.
    if os.path.exists(plan_file):
        print(f"\n  Plan written to {os.path.relpath(plan_file, REPO_ROOT)}")
        with open(plan_file) as f:
            steps = [l for l in f.read().strip().splitlines() if l.strip()]
        print(f"  Plan length: {len(steps)} steps")
        t = 0
        for i, step in enumerate(steps, 1):
            cost = _action_cost(step)
            t += cost
            suffix = f"  [t={t}s]" if cost > 0 else ""
            print(f"    {i:2}. {step}{suffix}")
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

    planner_backend = "enhsp"
    if action in ("Run planner", "Convert then plan"):
        selected_planner_backend = questionary.select(
            "Planner backend:",
            choices=list(PLANNER_BACKEND_CHOICES.keys()),
            style=STYLE,
        ).ask()
        if selected_planner_backend is None:
            return
        planner_backend = PLANNER_BACKEND_CHOICES[selected_planner_backend]

    if action in ("Convert to PDDL", "Convert then plan"):
        converters = discover_converters()
        if not converters:
            print("  No converter scripts found under src/convert/")
            return
        selected_converter = questionary.select(
            "Converter:",
            choices=list(converters.keys()),
            style=STYLE,
        ).ask()
        if selected_converter is None:
            return
        convert_script = converters[selected_converter]

        run_num = next_run_number(location, scenario_name)
        ok = run_convert(location, scenario, run_num, convert_script=convert_script)
        if not ok and action == "Convert then plan":
            print()
            return
        if action == "Convert then plan":
            run_planner(location, scenario_name, run_num, planner_backend=planner_backend)

    elif action == "Run planner":
        existing = discover_runs(location, scenario_name)
        if not existing:
            print(f"  No existing runs found for {scenario_name}.")
            print("  Run 'Convert to PDDL' first.")
            print()
            return
        choices = [f"run{n}" for n in existing]
        chosen = questionary.select("Run:", choices=choices, style=STYLE).ask()
        if chosen is None:
            return
        run_num = int(chosen[3:])
        run_planner(location, scenario_name, run_num, planner_backend=planner_backend)

    print()


if __name__ == "__main__":
    main()
