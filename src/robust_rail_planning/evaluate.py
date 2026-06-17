import os
import logging
import subprocess

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


def to_container_path(host_path):
    """
    Convert a host path under WORKSPACE_DIR to the matching Docker path.

    Example:
    /.../workspace/scenario-planning-inputs/Location_KleineBinckhorst/scenario.json

    becomes:
    /data/scenario-planning-inputs/Location_KleineBinckhorst/scenario.json
    """
    return "/data/" + os.path.relpath(host_path, WORKSPACE_DIR).replace(os.sep, "/")


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

    cmd = TORS_BIN + [
        "--mode", "EVAL_AND_STORE",
        "--path_location", to_container_path(GENERATE_DIR),
        "--path_scenario", to_container_path(scenario_path),
        "--path_plan", to_container_path(plan_path),
        "--path_eval_result", to_container_path(eval_result_path),
        "--departure_delay", "100000",
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
        logger.info("TORS output:\n%s", result.stdout)

    if result.returncode != 0:
        raise RuntimeError(
            f"TORS evaluation failed with exit code {result.returncode}\n\n"
            f"Command was:\n{' '.join(cmd)}\n\n"
            f"Output was:\n{result.stdout}"
        )

    if os.path.exists(eval_result_path):
        with open(eval_result_path, "r", encoding="utf-8") as f:
            stored_result = f.read()
        logger.info("Stored TORS evaluation result from %s:\n%s", eval_result_path, stored_result)
    else:
        logger.warning("Expected eval result file was not created: %s", eval_result_path)

    return {
        "stdout": result.stdout,
        "eval_result_path": eval_result_path,
    }