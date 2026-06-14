import os
import glob
import logging
import importlib.util
from datetime import datetime
import csv, time
import subprocess
from unified_planning.io import PDDLReader
from unified_planning.shortcuts import OneshotPlanner
import unified_planning as up
from .convert_no_switches import create_instance_from_scenario

up.shortcuts.get_environment().credits_stream = None  # silence per-call credits banner

# PATHS
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # planning-approach
GENERATE_DIR = os.path.join(os.path.dirname(BASE_DIR), "scenario-planning-inputs", "Location_KleineBinckhorst")
SCENARIOS_DIR = os.path.join(GENERATE_DIR, "scenarios")
PLANS_DIR = os.path.join(GENERATE_DIR, "plans")
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
PLANNER_LOCATION = os.path.abspath(os.path.join(BASE_DIR, "src", "plan", "planner.jl"))

LOCATION_FILE = os.path.join(GENERATE_DIR, "location.json")
DOMAIN_FILE = os.path.join(BASE_DIR, "domain", "domain.pddl")

TORS_BIN = ["docker", "run", "-it", "--rm", "--mount", "type=bind,source=/Users/maytesteeghs/DSAIT/Robust-Rail-NL/planning-approach/data,target=/data", "ghcr.io/robust-rail-nl/tors:latest"]

logger = logging.getLogger(__name__)

# SCENARIO SETTINGS
number_trains = [5, 10, 15, 20, 25, 30, 31, 32, 33, 34, 35]
number_instances = 10
matching = {0: "FIFO", 1: "random", 2: "LIFO"}
default_seed = 42
time_window_per_train = [520]
mixed_traffic = False
min_gap_on_gateway = 180
perform_servicing = False


# LOGGING
def setup_logging(level=logging.INFO):
    os.makedirs(LOG_DIR, exist_ok=True)
    log_file = os.path.join(LOG_DIR, f"pipeline_{datetime.now():%Y%m%d_%H%M%S}.log")
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(log_file)],
    )
    logging.getLogger("unified_planning").setLevel(logging.WARNING)
    logger.info("Logging to %s", log_file)


# GENERATE SCENARIOS
def generate():
    spec = importlib.util.spec_from_file_location("generate", os.path.join(GENERATE_DIR, "generate.py"))
    generate_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generate_module)
    generate_module.generate_scenarios(
        number_trains=number_trains, number_instances=number_instances, matching=matching,
        default_seed=default_seed, time_window_per_train=time_window_per_train,
        mixed_traffic=mixed_traffic, min_gap_on_gateway=min_gap_on_gateway,
        perform_servicing=perform_servicing,
    )

# READ SCENARIOS
def read_scenarios(scenarios_dir, n_trains="*", order="*"):
    pattern = os.path.join(scenarios_dir, f"{n_trains}trains", order, "scenario_solver*.json")
    return sorted(glob.glob(pattern))

# Examples
def read_example_scenarios(scenarios_dir):
    pattern = os.path.join(scenarios_dir, "scenario_solver_example*.json")
    return sorted(glob.glob(pattern))


# CONVERT SCENARIO TO PDDL
def convert(scenario_path, use_examples=False):
    """Convert a single scenario to PDDL. Returns the output .pddl path."""

    if use_examples:
        rel = os.path.join("examples", os.path.basename(scenario_path))
    else:
        rel = os.path.relpath(scenario_path, SCENARIOS_DIR)

    pddl_path = os.path.join(DATA_DIR, os.path.splitext(rel)[0] + ".pddl")

    os.makedirs(os.path.dirname(pddl_path), exist_ok=True)

    create_instance_from_scenario(
        scenario_file=scenario_path,
        output_file=pddl_path,
        location_file=LOCATION_FILE,
        domain_file=DOMAIN_FILE,
    )

    return pddl_path


def debug_problem(problem):
    logger.info("Problem name: %s", problem.name)
    logger.info("Objects: %d", len(list(problem.all_objects)))
    logger.info("Fluents: %d", len(problem.fluents))
    logger.info("Actions: %d", len(problem.actions))
    logger.info("Goals:")
    for g in problem.goals:
        logger.info("  %s", g)

    logger.info("Initial values:")
    for fluent, value in problem.initial_values.items():
        logger.info("  %s := %s", fluent, value)

    for action in problem.actions:
        logger.info("Action: %s", action.name)
        for p in action.preconditions:
            logger.info("  pre: %s", p)
        for e in action.effects:
            logger.info("  eff: %s", e)
            
# PLAN INSTANCE
def plan(pddl_path, timeout=300):
    """Solve one PDDL instance with ENHSP. Returns the plan path."""
    rel = os.path.relpath(pddl_path, DATA_DIR)
    plan_path = os.path.join(PLANS_DIR, os.path.splitext(rel)[0] + ".plan")
    os.makedirs(os.path.dirname(plan_path), exist_ok=True)

    problem = PDDLReader().parse_problem(DOMAIN_FILE, pddl_path)
    logger.debug("Problem kind for %s: %s", rel, problem.kind)

    # debug_problem(problem)

    with OneshotPlanner(name="enhsp") as planner:
        result = planner.solve(problem, timeout=timeout)

    logger.info("ENHSP status for %s: %s", rel, result.status)

    if result.plan is None:
        raise RuntimeError(f"No plan found for {rel}: {result.status}")

    with open(plan_path, "w") as f:
        f.write("\n".join(str(a) for a in result.plan.actions))

    logger.info("ENHSP solved %s (%d actions) -> %s", rel, len(result.plan.actions), plan_path)
    return plan_path

def plan_with_julia(pddl_path, timeout=300):
    """Solve one PDDL instance with SymbolicPlanners.jl. Returns the plan path."""

    rel = os.path.relpath(pddl_path, DATA_DIR)

    plan_path = os.path.join(
        PLANS_DIR,
        os.path.splitext(rel)[0] + ".plan"
    )

    os.makedirs(os.path.dirname(plan_path), exist_ok=True)

    logger.info("Solving %s with SymbolicPlanners.jl", rel)

    process = subprocess.Popen(
    [
        "julia",
        f"--project={BASE_DIR}",
        PLANNER_LOCATION,
        DOMAIN_FILE,
        pddl_path,
        "symbolic",
        plan_path,
    ],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
)

    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, _ = process.communicate()
        raise RuntimeError(f"Julia planner timed out for {rel}")

    if stdout:
        logger.info("Julia stdout for %s:\n%s", rel, stdout)

    print(f"  [done (exit {process.returncode})]")

    if process.returncode != 0:
        raise RuntimeError(
            f"Julia planner failed for {rel} with exit code {process.returncode}"
        )

    if not os.path.isfile(plan_path):
        raise RuntimeError(f"Julia planner did not create a plan file for {rel}")

    logger.info("SymbolicPlanners solved %s -> %s", rel, plan_path)

    return plan_path


def evaluate(scenario_path, plan_path):
    """Evaluate a single plan against its scenario. Returns the evaluation result."""
    pass  # TODO: implement


def run_pipeline(do_generate=False, use_examples=False):
    if do_generate:
        generate()

    if use_examples:
        scenario_paths = read_example_scenarios(SCENARIOS_DIR)
    else:
        scenario_paths = read_scenarios(SCENARIOS_DIR)

    logger.info("Found %d scenarios to process", len(scenario_paths))

    for scenario_path in scenario_paths:
        rel = os.path.relpath(scenario_path, SCENARIOS_DIR)
        logger.info("Processing %s", rel)

        pddl_path = convert(scenario_path, use_examples=use_examples)

        # plan_path = plan(pddl_path, timeout=5000)
        plan_path = plan_with_julia(pddl_path, timeout=5000)
        evaluation = evaluate(scenario_path, plan_path)


if __name__ == "__main__":
    setup_logging(logging.INFO)
    run_pipeline()