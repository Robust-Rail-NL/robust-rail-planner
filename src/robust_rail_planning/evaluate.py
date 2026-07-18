import os
import logging
import subprocess

FOR_WINDOWS_FLAG = False

logger = logging.getLogger(__name__)

# planning-approach
BASE_DIR = os.path.dirname(
    os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )
)

# parent folder containing both planning-approach and scenario-planning-inputs
WORKSPACE_DIR = os.path.dirname(BASE_DIR)

GENERATE_DIR = os.path.join(
    WORKSPACE_DIR,
    "scenario-planning-inputs",
    "Location_KleineBinckhorst",
)

SCENARIOS_DIR = os.path.join(GENERATE_DIR, "scenarios")
PLANS_DIR = os.path.join(GENERATE_DIR, "plans")

RESULTS_FILE = os.path.join(BASE_DIR, "results", "plans.csv")
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")

PLANNER_LOCATION = os.path.abspath(
    os.path.join(BASE_DIR, "src", "plan", "planner.jl")
)

LOCATION_FILE = os.path.join(GENERATE_DIR, "location_solver.json")
DOMAIN_FILE = os.path.join(BASE_DIR, "domain", "domain.pddl")

TORS_IMAGE = "ghcr.io/robust-rail-nl/tors:latest"
if FOR_WINDOWS_FLAG:
    # TORS needs its evaluator-format location, which includes distanceEntries.
    _robust_rail_root = os.path.dirname(os.path.dirname(os.path.realpath(GENERATE_DIR)))
    _evaluator_location = os.path.join(
        _robust_rail_root,
        "robust-rail-evaluator",
        "data",
        "Demo",
        "TUSS-Instance-Generator",
        "kleine_binckhorst",
    )
    TORS_LOCATION_DIR = os.environ.get(
        "TORS_LOCATION_DIR",
        _evaluator_location if os.path.isdir(_evaluator_location) else GENERATE_DIR,
    )

    # Resolve host symlinks before mapping paths into Docker. This keeps Windows
    # directory junctions from becoming broken links inside the Linux container.
    DOCKER_MOUNT_ROOT = os.path.commonpath([
        os.path.realpath(BASE_DIR),
        os.path.realpath(GENERATE_DIR),
        os.path.realpath(TORS_LOCATION_DIR),
    ])


def to_container_path(host_path):
    if FOR_WINDOWS_FLAG:
        """
        Convert a resolved host path under DOCKER_MOUNT_ROOT to its Docker path.

        Example:
        /.../workspace/scenario-planning-inputs/Location_KleineBinckhorst/scenario.json

        becomes:
        /data/scenario-planning-inputs/Location_KleineBinckhorst/scenario.json
        """
        real_path = os.path.realpath(host_path)
        return "/data/" + os.path.relpath(real_path, DOCKER_MOUNT_ROOT).replace(os.sep, "/")

    """
    Convert a host path under WORKSPACE_DIR to the matching Docker path.

    Example:
    /.../workspace/scenario-planning-inputs/Location_KleineBinckhorst/scenario.json

    becomes:
    /data/scenario-planning-inputs/Location_KleineBinckhorst/scenario.json
    """
    return "/data/" + os.path.relpath(host_path, WORKSPACE_DIR).replace(os.sep, "/")

TORS_BIN = []
if FOR_WINDOWS_FLAG:
    TORS_BIN = [
        "docker", "run", "--rm",
        "--mount", f"type=bind,source={DOCKER_MOUNT_ROOT},target=/data",
        TORS_IMAGE,
    ]
else:
    TORS_BIN = [
        "docker", "run", "--rm",
        "--mount", f"type=bind,source={WORKSPACE_DIR},target=/data",
        TORS_IMAGE,
    ]

def evaluate(scenario_path, plan_path):
    """Evaluate a single plan against its scenario with TORS, store result, and print output."""

    eval_results_dir = os.path.join(DATA_DIR, "tors_eval_results")
    os.makedirs(eval_results_dir, exist_ok=True)

    scenario_name = os.path.splitext(os.path.basename(scenario_path))[0]
    plan_name = os.path.splitext(os.path.basename(plan_path))[0]

    eval_result_path = os.path.join(
        eval_results_dir,
        f"{scenario_name}__{plan_name}__evaluation_results.txt",
    )

    cmd = TORS_BIN
    if FOR_WINDOWS_FLAG:
         cmd = cmd + [
            "--mode", "EVAL_AND_STORE",
            "--path_location", to_container_path(TORS_LOCATION_DIR),
            "--path_scenario", to_container_path(scenario_path),
            "--path_plan", to_container_path(plan_path),
            "--path_eval_result", to_container_path(eval_result_path),
            "--departure_delay", "86400",
            "--plan_type", "Solver",
        ]
    else:
        cmd = cmd + [
        "--mode", "EVAL_AND_STORE",
        "--path_location", to_container_path(GENERATE_DIR),
        "--path_scenario", to_container_path(scenario_path),
        "--path_plan", to_container_path(plan_path),
        "--path_eval_result", to_container_path(eval_result_path),
        "--departure_delay", "86400",
        "--plan_type", "Solver",
    ]

    logger.info("      evaluating plan with TORS and storing result")
    logger.debug("TORS command: %s", " ".join(cmd))

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )

    if result.stdout:
        logger.debug("TORS output:\n%s", result.stdout)

    if result.returncode != 0:
        raise RuntimeError(f"TORS evaluation failed with exit code {result.returncode}")

    if result.stdout:
        if "The plan is not valid" in result.stdout:
            raise RuntimeError("TORS evaluation failed: plan is not valid")
        if "Scenario failed." in result.stdout:
            raise RuntimeError("TORS evaluation failed: scenario failed")

    if os.path.exists(eval_result_path):
        with open(eval_result_path, "r", encoding="utf-8") as f:
            stored_result = f.read()
        logger.debug("Stored TORS evaluation result from %s:\n%s", eval_result_path, stored_result)
    else:
        logger.warning("Expected eval result file was not created: %s", eval_result_path)

    return {
        "stdout": result.stdout,
        "eval_result_path": eval_result_path,
    }