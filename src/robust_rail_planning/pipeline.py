import os
import sys
import glob
import json
import logging
import importlib.util
from datetime import datetime
import csv, time
import subprocess
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from convert.baseline_no_parameters.convert import create_instance_from_scenario
from .evaluate import evaluate
from .generate import generate
from . import converter
from local_search.solve import solve, UnsolvableScenarioError

# PATHS
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # planning-approach
GENERATE_DIR = os.path.join(os.path.dirname(BASE_DIR), "scenario-planning-inputs", "Location_KleineBinckhorst")
SCENARIOS_DIR = os.path.join(GENERATE_DIR, "scenarios")
RESULTS_FILE = os.path.join(BASE_DIR, "results", "plans.csv")
PLANS_DIR = os.path.join(GENERATE_DIR, "plans")
# Durable copy of every raw PDDL .plan, saved before it goes through the
# converter (PLANS_DIR lives under the inputs tree and may be cleaned/overwritten).
PLAN_ARCHIVE_DIR = os.path.join(BASE_DIR, "results", "plans")

LOCAL_SEARCH_PLANS_DIR = os.path.join(GENERATE_DIR, "plans_local_search")
LOCAL_SEARCH_RESULTS_FILE = os.path.join(BASE_DIR, "results", "plans_local_search.csv")

DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
PLANNER_LOCATION = os.path.abspath(os.path.join(BASE_DIR, "src", "plan", "planner.jl"))

LOCATION_FILE = os.path.join(GENERATE_DIR, "location_solver.json")
DOMAIN_FILE = os.path.join(BASE_DIR, "domain", "domain.pddl")
TOOLS_DIR = os.path.join(BASE_DIR, "tools")
CONVERTER_SCRIPT = os.path.join(os.path.dirname(__file__), "converter.py")
ENHSP_HEURISTIC = "hadd"

RESULTS_FIELDNAMES = [
    "run_id", "scenario", "pddl_file", "plan_file", "runtime_seconds",
    "plan_found", "plan_length", "planner_status", "error",
]

LOCAL_SEARCH_FIELDNAMES = [
    "run_id", "scenario", "plan_file", "runtime_seconds",
    "solver_status", "eval_result_path", "error",
]

logger = logging.getLogger(__name__)

# Per-thread log buffer so each scenario's console output prints as one block
# instead of interleaving across parallel workers.
_thread_local = threading.local()
_console_handler = None


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


class BufferedConsoleHandler(logging.StreamHandler):
    """On worker threads, buffer records into a thread-local list so each
    scenario's console output prints as one contiguous block. On the main
    thread (no buffer set) it behaves like a normal StreamHandler."""

    def emit(self, record):
        buffer = getattr(_thread_local, "buffer", None)
        if buffer is None:
            super().emit(record)
        else:
            buffer.append(record)


def setup_logging(level=logging.INFO):
    global _console_handler
    os.makedirs(LOG_DIR, exist_ok=True)
    log_file = os.path.join(LOG_DIR, f"pipeline_{datetime.now():%Y%m%d_%H%M%S}.log")

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    console_handler = BufferedConsoleHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(ConsoleFormatter())
    _console_handler = console_handler

    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s:%(lineno)d: %(message)s"
    ))

    root.addHandler(console_handler)
    root.addHandler(file_handler)

    logger.info("Log file: %s", log_file)


def _flush_records(records):
    """Replay a worker's buffered console records on the main thread, in order."""
    if _console_handler:
        for record in records:
            _console_handler.emit(record)


def natural_key(path):
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", path)
    ]

# READ SCENARIOS
def read_scenarios(scenarios_dir, n_trains="*", order="*"):
    pattern = os.path.join(scenarios_dir, f"{n_trains}trains", order, "scenario_solver*.json")
    return sorted(glob.glob(pattern), key=natural_key)


def read_example_scenarios(scenarios_dir):
    pattern = os.path.join(scenarios_dir, "scenario_solver_example*.json")
    return sorted(glob.glob(pattern), key=natural_key)

# def read_example_scenarios(scenarios_dir):
#     pattern = os.path.join(scenarios_dir, "scenario_solver_example2.json")
#     return sorted(glob.glob(pattern), key=natural_key)

def init_results_file(results_file, fieldnames=RESULTS_FIELDNAMES):
    os.makedirs(os.path.dirname(results_file), exist_ok=True)

    file_exists = os.path.isfile(results_file)
    file_is_empty = not file_exists or os.path.getsize(results_file) == 0

    if file_is_empty:
        with open(results_file, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()


def append_result(results_file, row, fieldnames=RESULTS_FIELDNAMES):
    with open(results_file, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writerow(row)

# CONVERT SCENARIO TO PDDL
def convert(scenario_path, use_examples=False, write_domain=True):
    """Convert a single scenario to PDDL. Returns the output .pddl path.

    Set write_domain=False to skip (re)writing the shared DOMAIN_FILE. The
    domain is identical for every scenario, so during parallel planning it is
    written exactly once up front and workers only write their own instance
    file, avoiding a write/write (and write/read) race on DOMAIN_FILE.
    """

    if use_examples:
        rel = os.path.join("examples", os.path.basename(scenario_path))
    else:
        rel = os.path.relpath(scenario_path, SCENARIOS_DIR)

    pddl_path = os.path.join(DATA_DIR, os.path.splitext(rel)[0] + ".pddl")
    os.makedirs(os.path.dirname(pddl_path), exist_ok=True)
    os.makedirs(os.path.dirname(DOMAIN_FILE), exist_ok=True)

    create_instance_from_scenario(
        scenario_file=scenario_path,
        output_file=pddl_path,
        location_file=LOCATION_FILE,
        domain_file=DOMAIN_FILE if write_domain else None,
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
def _find_enhsp_jar():
    candidate = os.path.join(TOOLS_DIR, "planners", "enhsp", "enhsp.jar")
    if os.path.isfile(candidate):
        return os.path.abspath(candidate)
    return None

def _find_java():
    java = shutil.which("java")
    if java:
        return java
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidate = os.path.join(java_home, "bin", "java.exe")
        if os.path.isfile(candidate):
            return candidate
    return None

def plan(pddl_path, timeout=None):
    """
    Solve one PDDL instance with ENHSP.
    Always uses the local ENHSP jar. Unified Planning fallback is intentionally disabled.

    Returns:
        tuple: (plan_found, plan_path, plan_length, planner_status)
    """
    rel = os.path.relpath(pddl_path, DATA_DIR)
    plan_path = os.path.join(PLANS_DIR, os.path.splitext(rel)[0] + ".plan")
    os.makedirs(os.path.dirname(plan_path), exist_ok=True)

    enhsp_jar = _find_enhsp_jar()
    java = _find_java()

    if not enhsp_jar:
        logger.error("  local ENHSP jar not available; expected tools/planners/enhsp/enhsp.jar")
        return False, None, 0, "MISSING_ENHSP_JAR"

    if not java:
        logger.error("  Java not available; install Java 17+ or set JAVA_HOME")
        return False, None, 0, "MISSING_JAVA"

    if enhsp_jar and java:
        cmd = [java, "-jar", enhsp_jar, "-sp", plan_path, "-h", ENHSP_HEURISTIC, "-s", "wa_star_4",
               "-o", DOMAIN_FILE, "-f", pddl_path]
        logger.debug("Running: %s", " ".join(cmd))

        timeout_sec = timeout if timeout else 300

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True, text=True,
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired:
            logger.warning("  ENHSP timed out for %s", rel)
            return False, None, 0, "TIMEOUT"

        stdout_lower = proc.stdout.lower()

        if os.path.isfile(plan_path):
            with open(plan_path) as f:
                plan_lines = [l.strip() for l in f if l.strip()]
            plan_length = len(plan_lines)
            logger.info("      solved by ENHSP: %d actions", plan_length)
            logger.debug("Plan written to %s", plan_path)
            return True, plan_path, plan_length, "SOLVED"
        elif "unsolvable" in stdout_lower:
            logger.warning("  problem unsolvable: %s", rel)
            return False, None, 0, "UNSOLVABLE"
        elif "no plan" in stdout_lower or "no solution" in stdout_lower:
            logger.warning("  no plan found: %s", rel)
            return False, None, 0, "NO_PLAN"
        else:
            logger.warning("  no plan found by ENHSP (unknown reason)")
            return False, None, 0, "UNKNOWN"

def plan_with_julia(pddl_path, timeout=300):
    """Solve one PDDL instance with SymbolicPlanners.jl A*."""

    rel = os.path.relpath(pddl_path, DATA_DIR)

    plan_path = os.path.join(PLANS_DIR, os.path.splitext(rel)[0] + ".plan")
    os.makedirs(os.path.dirname(plan_path), exist_ok=True)
    logger.info("      solving %s with Julia A-star", rel)

    cmd = [
        "julia",
        f"--project={BASE_DIR}",
        PLANNER_LOCATION,
        DOMAIN_FILE,
        pddl_path,
        "symbolic",
        plan_path,
    ]
    logger.debug("Running: %s", " ".join(cmd))

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("      Julia planner timed out for %s", rel)
        return False, None, 0, "TIMEOUT"

    if proc.returncode != 0:
        logger.warning("      Julia planner failed for %s (exit %d)", rel, proc.returncode)
        if proc.stdout:
            logger.debug("      Julia stdout:\n%s", proc.stdout)
        return False, None, 0, "FAILED"

    if not os.path.isfile(plan_path):
        logger.warning("      Julia planner did not produce a plan file for %s", rel)
        return False, None, 0, "NO_PLAN"

    with open(plan_path) as f:
        plan_lines = [l.strip() for l in f if l.strip()]
    plan_length = len(plan_lines)
    logger.info("      solved by SymbolicPlanners.jl: %d actions", plan_length)
    return True, plan_path, plan_length, "SOLVED"


def archive_plan(plan_path):
    """Save a copy of the raw PDDL .plan into the results plan archive,
    mirroring its path under PLANS_DIR, before it is handed to the converter.
    Each scenario maps to a unique relative path, so this is parallel-safe.
    Returns the archived path."""
    rel = os.path.relpath(plan_path, PLANS_DIR)
    archived = os.path.join(PLAN_ARCHIVE_DIR, rel)
    os.makedirs(os.path.dirname(archived), exist_ok=True)
    shutil.copy2(plan_path, archived)
    return archived


def process_scenario(scenario_path, idx, total, run_id, use_examples, planner):
    """Run the full per-scenario pipeline (convert -> plan -> convert plan to
    TORS JSON -> evaluate) for one scenario.

    Returns (row, records): the results-CSV row dict, plus this worker's
    buffered console log records to be flushed in order on the main thread.

    Note: write_domain=False here. The shared DOMAIN_FILE is written exactly
    once before any workers start (see run_pipeline), so workers never touch it.
    """
    _thread_local.buffer = []
    started = time.perf_counter()
    rel_scenario = os.path.relpath(scenario_path, SCENARIOS_DIR)
    logger.info("")
    logger.info("[%d/%d] %s", idx, total, rel_scenario)

    pddl_path = None
    plan_path = None
    plan_found = False
    plan_length = 0
    planner_status = ""
    error = ""

    try:
        pddl_path = convert(scenario_path, use_examples=use_examples, write_domain=False)

        if planner == "enhsp":
            plan_found, plan_path, plan_length, planner_status = plan(pddl_path, timeout=600)
        else:
            plan_found, plan_path, plan_length, planner_status = plan_with_julia(pddl_path)

        if plan_found:
            json_path = plan_path.replace(".plan", ".json")
            tors_scenario_path = scenario_path.replace("scenario_solver_", "scenario_")

            archived_plan = archive_plan(plan_path)
            logger.info("      saved raw plan to %s", os.path.relpath(archived_plan, BASE_DIR))

            logger.info("      converting plan to TORS JSON")
            subprocess.run(
                [sys.executable, CONVERTER_SCRIPT,
                 "--plan", plan_path,
                 "--scenario", scenario_path,
                 "--location", LOCATION_FILE,
                 "--output", json_path],
                check=True, capture_output=True, text=True,
            )

            # evaluate(tors_scenario_path, json_path)
        else:
            logger.warning("  skipping evaluation because no plan was found")

    except Exception as exc:
        error = repr(exc)
        logger.error("  planning / evaluation failed: %s", exc)
        logger.debug("  traceback:", exc_info=True)

    elapsed = time.perf_counter() - started

    if error:
        logger.error("  recorded failure after %.2fs", elapsed)
    elif not plan_found:
        logger.warning("  recorded no-plan result after %.2fs", elapsed)
    else:
        logger.info("  done in %.2fs", elapsed)

    row = {
        "run_id": run_id,
        "scenario": rel_scenario,
        "pddl_file": os.path.relpath(pddl_path, DATA_DIR) if pddl_path else "",
        "plan_file": os.path.relpath(plan_path, PLANS_DIR) if plan_path else "",
        "runtime_seconds": f"{elapsed:.4f}",
        "plan_found": plan_found,
        "plan_length": plan_length,
        "planner_status": planner_status,
        "error": error,
    }

    records = _thread_local.buffer
    _thread_local.buffer = None
    return row, records


def process_simple_scenario(planner="astar"):
    """Convert, plan, and evaluate the simple scenario using TORS."""
    solver_scenario_path = os.path.join(SCENARIOS_DIR, "scenario_solver_simple.json")
    tors_scenario_path = os.path.join(SCENARIOS_DIR, "scenario_simple.json")
    location_path = LOCATION_FILE

    # Step 1: Convert to PDDL
    logger.info("Converting simple scenario to PDDL")
    pddl_path = os.path.join(DATA_DIR, "scenario_solver_simple.pddl")
    os.makedirs(os.path.dirname(pddl_path), exist_ok=True)

    create_instance_from_scenario(
        scenario_file=solver_scenario_path,
        output_file=pddl_path,
        location_file=location_path,
        domain_file=DOMAIN_FILE,
    )
    logger.info("      PDDL written to %s", os.path.relpath(pddl_path, BASE_DIR))

    # Step 2: Plan
    logger.info("Planning simple scenario")
    if planner == "enhsp":
        plan_found, plan_path, plan_length, planner_status = plan(pddl_path, timeout=600)
    else:
        plan_found, plan_path, plan_length, planner_status = plan_with_julia(pddl_path)

    if not plan_found:
        logger.error("  no plan found for simple scenario (status=%s)", planner_status)
        return

    logger.info("      plan found: %d actions", plan_length)
    logger.info("      raw plan: %s", os.path.relpath(plan_path, BASE_DIR))

    # Step 3: Convert plan to TORS JSON
    json_path = plan_path.replace(".plan", ".json")
    logger.info("Converting plan to TORS JSON: %s", os.path.relpath(json_path, BASE_DIR))

    result = converter.convert_plan(plan_path, solver_scenario_path, location_path)
    with open(json_path, "w") as f:
        json.dump(result, f, indent=4)

    # Step 4: Evaluate with TORS
    logger.info("Evaluating plan with TORS")
    try:
        eval_result = evaluate(tors_scenario_path, json_path)
        eval_result_path = eval_result.get("eval_result_path", "")
        if eval_result_path:
            logger.info("      TORS evaluation result written to: %s",
                        os.path.relpath(eval_result_path, BASE_DIR))
        logger.info("Simple scenario pipeline complete")
    except Exception as exc:
        logger.error("  TORS evaluation failed: %s", exc)


def _is_tors_plan(path):
    """Check if a JSON file is a TORS plan (has 'actions' key)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return isinstance(data, dict) and "actions" in data
    except Exception:
        return False


def _find_matching_scenario(scenarios_dir, label):
    """Find a TORS scenario matching a label.

    Tries exact match, then strips 'solver_' and globs for scenario_*.json.
    Searches in the scenarios/ subdirectory of test_data.
    """
    import fnmatch
    # Exact match (skip solver scenarios - those are not TORS scenarios)
    exact = os.path.join(scenarios_dir, f"{label}.json")
    if os.path.isfile(exact) and not _is_tors_plan(exact) and "solver_" not in os.path.basename(exact):
        return exact
    # Strip 'solver_' if present and try exact
    if "solver_" in label:
        stripped = label.replace("solver_", "", 1)
        exact2 = os.path.join(scenarios_dir, f"{stripped}.json")
        if os.path.isfile(exact2) and "solver_" not in os.path.basename(exact2):
            return exact2
    # Glob: find scenario_<stripped_base>*.json (non-solver)
    stripped = label.replace("solver_", "", 1) if "solver_" in label else label
    base = stripped.replace("scenario_", "", 1) if stripped.startswith("scenario_") else stripped
    for name in sorted(os.listdir(scenarios_dir)):
        if fnmatch.fnmatch(name, f"scenario_{base}*.json") and "solver_" not in name:
            return os.path.join(scenarios_dir, name)
    return None


def _find_matching_solver_scenario(scenarios_dir, label):
    """Find a solver scenario matching a label.

    Looks for scenario_solver_<base>*.json files that are NOT TORS plans.
    Searches in the scenarios/ subdirectory of test_data.
    """
    import fnmatch
    base = label.replace("scenario_solver_", "", 1) if "scenario_solver_" in label else label
    for name in sorted(os.listdir(scenarios_dir)):
        if not fnmatch.fnmatch(name, f"scenario_solver_{base}*.json"):
            continue
        path = os.path.join(scenarios_dir, name)
        if _is_tors_plan(path):
            continue
        return path
    return None


def run_test_eval():
    """Run pre-made plans from test_data/ through converter and/or TORS.

    Directory layout:
      test_data/plans/     - .plan files and .json TORS plan files
      test_data/scenarios/ - .json solver scenarios and TORS scenarios

    Scans plans/ for plan files and scenarios/ for matching scenarios:
      - .plan files  -> converter (using solver scenario + location file) -> TORS JSON -> TORS eval
      - .json TORS plans (have 'actions' key) -> TORS eval directly
      - .json TORS scenarios (have 'in'/'out' keys) -> used for evaluation
      - .json solver scenarios -> used for conversion

    Matching convention:
      scenario_solver_<name>.plan  -> solver scenario: scenario_solver_<name>_*.json
                                   -> TORS scenario:  scenario_<name>_*.json
      scenario_solver_<name>.json  -> TORS scenario:  scenario_<name>_*.json

    Only the location file (LOCATION_FILE) is loaded from outside test_data/.
    """
    test_data_dir = os.path.join(BASE_DIR, "test_data")
    plans_dir = os.path.join(test_data_dir, "plans")
    scenarios_dir = os.path.join(test_data_dir, "scenarios")

    if not os.path.isdir(plans_dir):
        logger.error("test_data/plans directory not found: %s", plans_dir)
        return
    if not os.path.isdir(scenarios_dir):
        logger.error("test_data/scenarios directory not found: %s", scenarios_dir)
        return

    all_plan_files = sorted(glob.glob(os.path.join(plans_dir, "*")))
    plan_files = [f for f in all_plan_files if f.endswith(".plan")]
    json_plan_files = [f for f in all_plan_files if f.endswith(".json") and _is_tors_plan(f)]

    logger.info("[test-eval] Found %d .plan files, %d .json plan files in test_data/plans/",
                len(plan_files), len(json_plan_files))

    results = []

    # --- .json TORS plans: evaluate directly ---
    for path in json_plan_files:
        label = os.path.splitext(os.path.basename(path))[0]
        tors_scenario = _find_matching_scenario(scenarios_dir, label)

        if not tors_scenario:
            logger.info("")
            logger.info("[test-eval] %s (json plan)", label)
            logger.warning("      no matching TORS scenario found, skipping")
            results.append((label, False, "SKIP: no scenario"))
            continue

        logger.info("")
        logger.info("[test-eval] %s (json plan -> TORS)", label)
        passed = False
        detail = ""
        try:
            logger.info("      evaluating with scenario %s",
                        os.path.basename(tors_scenario))
            evaluate(tors_scenario, path)
            passed = True
            detail = "PASS"
        except Exception as exc:
            detail = f"FAIL: {exc}"
            logger.error("      %s", detail)

        results.append((label, passed, detail))
        logger.info("      -> %s", "PASS" if passed else "FAIL")

    # --- .plan files: converter -> TORS JSON -> TORS eval ---
    for path in plan_files:
        label = os.path.splitext(os.path.basename(path))[0]
        solver_scenario = _find_matching_solver_scenario(scenarios_dir, label)
        tors_scenario = _find_matching_scenario(scenarios_dir, label)

        if not solver_scenario:
            logger.info("")
            logger.info("[test-eval] %s (plan)", label)
            logger.warning("      solver scenario not found, skipping")
            results.append((label, False, "SKIP: no solver scenario"))
            continue
        if not tors_scenario:
            logger.info("")
            logger.info("[test-eval] %s (plan)", label)
            logger.warning("      TORS scenario not found, skipping")
            results.append((label, False, "SKIP: no TORS scenario"))
            continue

        logger.info("")
        logger.info("[test-eval] %s (plan -> converter -> TORS)", label)
        passed = False
        detail = ""
        converted_dir = os.path.join(test_data_dir, "plans_converted")
        os.makedirs(converted_dir, exist_ok=True)
        json_path = os.path.join(converted_dir, f"{label}_converted.json")
        try:
            logger.info("      converting with solver scenario %s",
                        os.path.basename(solver_scenario))
            result = converter.convert_plan(path, solver_scenario, LOCATION_FILE)
            with open(json_path, "w") as f:
                json.dump(result, f, indent=4)
            logger.info("      evaluating with TORS scenario %s",
                        os.path.basename(tors_scenario))
            evaluate(tors_scenario, json_path)
            passed = True
            detail = "PASS"
        except Exception as exc:
            detail = f"FAIL: {exc}"
            logger.error("      %s", detail)

        results.append((label, passed, detail))
        logger.info("      -> %s", "PASS" if passed else "FAIL")

    # Summary
    logger.info("")
    logger.info("[test-eval] === Summary ===")
    n_pass = sum(1 for _, p, _ in results if p)
    n_fail = sum(1 for _, p, _ in results if not p)
    for label, passed, detail in results:
        logger.info("  %s  %s", label, detail)
    logger.info("  %d passed, %d failed, %d total", n_pass, n_fail, len(results))


def run_pipeline(do_generate=False, use_examples=False, planner="astar",
                 do_local_search=False, max_workers=10, simple_scenario=False,
                 test_eval=False):
    if do_generate:
        logger.info("Generating scenarios...")
        generate()

    scenario_paths = (
        read_example_scenarios(SCENARIOS_DIR)
        if use_examples
        else read_scenarios(SCENARIOS_DIR)
    )

    if simple_scenario:
        process_simple_scenario(planner=planner)
        return

    if test_eval:
        run_test_eval()
        return

    if do_local_search:
        run_pipeline_local_search(scenario_paths, max_workers=max_workers)
        return

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    total = len(scenario_paths)
    logger.info("Found %d scenarios", total)

    if total == 0:
        logger.warning("No scenarios found, nothing to do")
        return

    if max_workers is None:
        max_workers = max(1, min(total, os.cpu_count() or 2))

    # Write the shared domain file exactly once, before any workers start.
    # Every scenario produces the same domain, so generating it here removes
    # the write/write and write/read race on DOMAIN_FILE during parallel
    # planning. (This call also writes scenario 0's instance file, which its
    # worker harmlessly rewrites later.)
    logger.info("Writing shared domain file before parallel planning")
    convert(scenario_paths[0], use_examples=use_examples, write_domain=True)

    logger.info("Running planner with up to %d parallel workers", max_workers)
    logger.info("Appending results to %s", RESULTS_FILE)

    init_results_file(RESULTS_FILE)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(process_scenario, scenario_path, i, total,
                            run_id, use_examples, planner): scenario_path
            for i, scenario_path in enumerate(scenario_paths, start=1)
        }

        # Results stream in as workers finish. Only the main thread writes the
        # CSV and flushes console logs, so no locking is required and each
        # scenario's log block stays intact.
        for future in as_completed(futures):
            row, records = future.result()
            _flush_records(records)
            append_result(RESULTS_FILE, row)

    logger.info("")
    logger.info("All %d scenarios processed", total)


def local_search_plan_path(scenario_path):
    """
    Mirror a scenario's path (relative to SCENARIOS_DIR) into the local-search
    plans dir with a .json extension — same naming convention as the regular
    plan jsons, just in a sibling directory.
    """
    rel = os.path.relpath(scenario_path, SCENARIOS_DIR)
    return os.path.join(LOCAL_SEARCH_PLANS_DIR, os.path.splitext(rel)[0] + ".json")


def _is_unsolvable_exc(exc):
    """True if an exception from `solve` represents provable infeasibility
    (no feasible arrival/departure matching) rather than an unexpected crash.
    Checks the message and any captured subprocess output, since a
    CalledProcessError keeps the C# message in stderr/output, not in str(exc).
    """
    parts = [str(exc)]
    for attr in ("stderr", "output", "stdout"):
        val = getattr(exc, attr, None)
        if val:
            parts.append(val if isinstance(val, str) else val.decode("utf-8", "replace"))
    text = "\n".join(parts).lower()
    return "no feasible matching possible" in text or "unsolvable" in text


def process_scenario_local_search(scenario_path, idx, total, run_id):
    """Run the full per-scenario local-search pipeline (solve -> evaluate) for
    one scenario.

    Returns (row, records): the local-search results-CSV row dict, plus this
    worker's buffered console log records to be flushed in order on the main
    thread. Each scenario writes its own plan_path (mirrored from the scenario
    path), so workers never collide and no locking is required.
    """
    _thread_local.buffer = []
    started = time.perf_counter()
    rel_scenario = os.path.relpath(scenario_path, SCENARIOS_DIR)
    logger.info("")
    logger.info("[%d/%d] %s", idx, total, rel_scenario)

    plan_path = local_search_plan_path(scenario_path)
    os.makedirs(os.path.dirname(plan_path), exist_ok=True)

    # Empty plan JSON for the generated config's PlanPath to point at.
    with open(plan_path, "w", encoding="utf-8") as f:
        f.write("{}")

    solver_status = ""
    eval_result_path = ""
    error = ""

    try:
        solve(scenario_path, plan_path)
        solver_status = "SOLVED"

        # Evaluate the plan the solver produced, using TORS.
        tors_scenario_path = scenario_path.replace("scenario_solver_", "scenario_")
        logger.info("      evaluating local search plan")
        # evaluation = evaluate(tors_scenario_path, plan_path)
        # eval_result_path = evaluation.get("eval_result_path", "")

    except UnsolvableScenarioError as exc:
        solver_status = "UNSOLVABLE"

        logger.warning(
            "      scenario marked UNSOLVABLE: %s",
            exc.reason,
        )
        logger.info(
            "      unsolvable scenario path: %s",
            scenario_path,
        )

        if exc.config_path:
            logger.info(
                "      solver config path: %s",
                exc.config_path,
            )

        if exc.returncode is not None:
            logger.info(
                "      solver exit code: %s",
                exc.returncode,
            )

        # Helpful when debugging, but not too noisy for normal runs.
        logger.debug(
            "solver output for unsolvable scenario:\n%s",
            exc.output,
        )
    
    except Exception as exc:
        error = repr(exc)
        logger.error("  failed unexpectedly: %s", exc)
        logger.debug("  traceback:", exc_info=True)

    elapsed = time.perf_counter() - started

    if error:
        logger.error("  recorded failure after %.2fs", elapsed)
    elif solver_status == "UNSOLVABLE":
        logger.warning("  recorded unsolvable result after %.2fs", elapsed)
    else:
        logger.info("  done in %.2fs", elapsed)

    row = {
        "run_id": run_id,
        "scenario": rel_scenario,
        "plan_file": os.path.relpath(plan_path, LOCAL_SEARCH_PLANS_DIR),
        "runtime_seconds": f"{elapsed:.4f}",
        "solver_status": solver_status,
        "eval_result_path": eval_result_path,
        "error": error,
    }

    records = _thread_local.buffer
    _thread_local.buffer = None
    return row, records


def run_pipeline_local_search(scenario_paths, max_workers=10):
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    total = len(scenario_paths)
    logger.info("Found %d scenarios", total)

    if total == 0:
        logger.warning("No scenarios found, nothing to do")
        return

    if max_workers is None:
        max_workers = max(1, min(total, os.cpu_count() or 2))

    logger.info("Writing local-search plans to %s", LOCAL_SEARCH_PLANS_DIR)
    logger.info("Running local search with up to %d parallel workers", max_workers)
    logger.info("Appending results to %s", LOCAL_SEARCH_RESULTS_FILE)

    init_results_file(LOCAL_SEARCH_RESULTS_FILE, LOCAL_SEARCH_FIELDNAMES)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(process_scenario_local_search, scenario_path, i,
                            total, run_id): scenario_path
            for i, scenario_path in enumerate(scenario_paths, start=1)
        }

        # Results stream in as workers finish. Only the main thread writes the
        # CSV and flushes console logs, so no locking is required and each
        # scenario's log block stays intact.
        for future in as_completed(futures):
            row, records = future.result()
            _flush_records(records)
            append_result(LOCAL_SEARCH_RESULTS_FILE, row, LOCAL_SEARCH_FIELDNAMES)

    logger.info("")
    logger.info("All %d scenarios processed", total)


if __name__ == "__main__":
    setup_logging(logging.INFO)
    run_pipeline()
