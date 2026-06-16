import os
import glob
import logging
import importlib.util
from datetime import datetime
import csv, time
import subprocess
import re
from unified_planning.io import PDDLReader
from unified_planning.shortcuts import OneshotPlanner
import unified_planning as up
from .convert_no_switches import create_instance_from_scenario
from .evaluate import evaluate

up.shortcuts.get_environment().credits_stream = None  # silence per-call credits banner

# PATHS
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # planning-approach
GENERATE_DIR = os.path.join(os.path.dirname(BASE_DIR), "scenario-planning-inputs", "Location_KleineBinckhorst")
SCENARIOS_DIR = os.path.join(GENERATE_DIR, "scenarios")
RESULTS_FILE = os.path.join(BASE_DIR, "results", "plans.csv")
PLANS_DIR = os.path.join(GENERATE_DIR, "plans")
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
PLANNER_LOCATION = os.path.abspath(os.path.join(BASE_DIR, "src", "plan", "planner.jl"))

LOCATION_FILE = os.path.join(GENERATE_DIR, "location_solver.json")
DOMAIN_FILE = os.path.join(BASE_DIR, "domain", "domain.pddl")

# TORS_BIN = ["docker", "run", "-it", "--rm", "--mount", "type=bind,source=/Users/maytesteeghs/DSAIT/Robust-Rail-NL/planning-approach/data,target=/data", "ghcr.io/robust-rail-nl/tors:latest"]

logger = logging.getLogger(__name__)

# SCENARIO SETTINGS
number_trains = [1, 2, 3, 4, 5, 10]
number_instances = 10
matching = {0: "FIFO", 1: "random", 2: "LIFO"}
default_seed = 42
time_window_per_train = [520]
mixed_traffic = False
min_gap_on_gateway = 180
perform_servicing = False


# LOGGING
class ConsoleFormatter(logging.Formatter):
    """Short, readable console logs."""

    FORMATS = {
        logging.DEBUG: "  debug  %(message)s",
        logging.INFO: "%(message)s",
        logging.WARNING: "  warning  %(message)s",
        logging.ERROR: "  error    %(message)s",
        logging.CRITICAL: "  critical %(message)s",
    }

    def format(self, record):
        formatter = logging.Formatter(self.FORMATS.get(record.levelno, "%(message)s"))
        return formatter.format(record)


def setup_logging(level=logging.INFO):
    os.makedirs(LOG_DIR, exist_ok=True)
    log_file = os.path.join(LOG_DIR, f"pipeline_{datetime.now():%Y%m%d_%H%M%S}.log")

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(ConsoleFormatter())

    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s:%(lineno)d: %(message)s"
    ))

    root.addHandler(console_handler)
    root.addHandler(file_handler)

    logging.getLogger("unified_planning").setLevel(logging.WARNING)

    logger.info("Log file: %s", log_file)

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
    
    
def natural_key(path):
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", path)
    ]

# READ SCENARIOS
def read_scenarios(scenarios_dir, n_trains="*", order="*"):
    pattern = os.path.join(scenarios_dir, f"{n_trains}trains", order, "scenario_solver*.json")
    return sorted(glob.glob(pattern), key=natural_key)


# def read_example_scenarios(scenarios_dir):
#     pattern = os.path.join(scenarios_dir, "scenario_solver_example*.json")
#     return sorted(glob.glob(pattern), key=natural_key)

def read_example_scenarios(scenarios_dir):
    pattern = os.path.join(scenarios_dir, "scenario_solver_example2.json")
    return sorted(glob.glob(pattern), key=natural_key)


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
    
    logging.info("      converted scenario to PDDL")
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
def plan(pddl_path, timeout=None):
    """
    Solve one PDDL instance with ENHSP.

    Returns:
        tuple: (plan_found, plan_path, plan_length, planner_status)
    """
    rel = os.path.relpath(pddl_path, DATA_DIR)
    plan_path = os.path.join(PLANS_DIR, os.path.splitext(rel)[0] + ".plan")
    os.makedirs(os.path.dirname(plan_path), exist_ok=True)

    problem = PDDLReader().parse_problem(DOMAIN_FILE, pddl_path)
    logger.debug("Problem kind for %s: %s", rel, problem.kind)

    with OneshotPlanner(name="enhsp") as planner:
        result = planner.solve(problem, timeout=timeout)

    planner_status = str(result.status)
    logger.debug("ENHSP status for %s: %s", rel, planner_status)

    if result.plan is None:
        logger.warning("  no plan found by ENHSP: %s", planner_status)
        return False, None, 0, planner_status

    plan_actions = list(result.plan.actions)

    with open(plan_path, "w") as f:
        f.write("\n".join(str(a) for a in plan_actions))

    logger.info("      solved by ENHSP: %d actions", len(plan_actions))
    logger.debug("Plan written to %s", plan_path)

    return True, plan_path, len(plan_actions), planner_status

def plan_with_julia(pddl_path, timeout=300):
    """Solve one PDDL instance with SymbolicPlanners.jl. Returns the plan path."""

    rel = os.path.relpath(pddl_path, DATA_DIR)

    plan_path = os.path.join(PLANS_DIR, os.path.splitext(rel)[0] + ".plan")
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

    if process.returncode != 0:
        raise RuntimeError(
            f"Julia planner failed for {rel} with exit code {process.returncode}"
        )

    if not os.path.isfile(plan_path):
        raise RuntimeError(f"Julia planner did not create a plan file for {rel}")

    logger.info("SymbolicPlanners solved %s -> %s", rel, plan_path)

    return plan_path

def init_results_file(results_file):
    os.makedirs(os.path.dirname(results_file), exist_ok=True)

    file_exists = os.path.isfile(results_file)
    file_is_empty = not file_exists or os.path.getsize(results_file) == 0

    if file_is_empty:
        with open(results_file, "w", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "run_id",
                    "scenario",
                    "pddl_file",
                    "plan_file",
                    "runtime_seconds",
                    "plan_found",
                    "plan_length",
                    "planner_status",
                    "error",
                ],
            )
            writer.writeheader()

def append_result(results_file, row):
    with open(results_file, "a", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "run_id",
                "scenario",
                "pddl_file",
                "plan_file",
                "runtime_seconds",
                "plan_found",
                "plan_length",
                "planner_status",
                "error",
            ],
        )
        writer.writerow(row)

def run_pipeline(do_generate=False, use_examples=False):
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    if do_generate:
        logger.info("Generating scenarios...")
        generate()

    scenario_paths = (
        read_example_scenarios(SCENARIOS_DIR)
        if use_examples
        else read_scenarios(SCENARIOS_DIR)
    )

    total = len(scenario_paths)
    logger.info("Found %d scenarios", total)
    logger.info("Appending results to %s", RESULTS_FILE)

    init_results_file(RESULTS_FILE)

    for i, scenario_path in enumerate(scenario_paths, start=1):
        started = time.perf_counter()
        rel_scenario = os.path.relpath(scenario_path, SCENARIOS_DIR)

        logger.info("")
        logger.info("[%d/%d] %s", i, total, rel_scenario)

        pddl_path = None
        plan_path = None
        plan_found = False
        plan_length = 0
        planner_status = ""
        error = ""

        try:
            pddl_path = convert(scenario_path, use_examples=use_examples)

            plan_found, plan_path, plan_length, planner_status = plan(pddl_path)

            if plan_found:
                evaluation = evaluate(scenario_path, plan_path)
            else:
                logger.warning("  skipping evaluation because no plan was found")

        except Exception as exc:
            error = repr(exc)
            logger.exception("  failed unexpectedly")

        elapsed = time.perf_counter() - started

        append_result(
            RESULTS_FILE,
            {
                "run_id": run_id,
                "scenario": rel_scenario,
                "pddl_file": os.path.relpath(pddl_path, DATA_DIR) if pddl_path else "",
                "plan_file": os.path.relpath(plan_path, PLANS_DIR) if plan_path else "",
                "runtime_seconds": f"{elapsed:.4f}",
                "plan_found": plan_found,
                "plan_length": plan_length,
                "planner_status": planner_status,
                "error": error,
            },
        )

        if error:
            logger.error("  recorded failure after %.2fs", elapsed)
        elif not plan_found:
            logger.warning("  recorded no-plan result after %.2fs", elapsed)
        else:
            logger.info("  done in %.2fs", elapsed)
        
if __name__ == "__main__":
    setup_logging(logging.INFO)
    run_pipeline()