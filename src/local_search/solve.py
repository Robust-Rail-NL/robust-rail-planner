import os
import logging
import subprocess

logger = logging.getLogger(__name__)

# PATHS (mirror the rest of the pipeline)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # planning-approach
GENERATE_DIR = os.path.join(os.path.dirname(BASE_DIR), "scenario-planning-inputs", "Location_KleineBinckhorst")
SCENARIOS_DIR = os.path.join(GENERATE_DIR, "scenarios")
LOCATION_FILE = os.path.join(GENERATE_DIR, "location_solver.json")
CONFIG_DIR = os.path.join(GENERATE_DIR, "config")

# GENERATE_DIR is bind-mounted into the container as /app/database, so every
# file under it is visible at /app/database/<relative path>. The config dir
# lives under GENERATE_DIR too, so the container can read the generated configs.
MOUNT_SOURCE = GENERATE_DIR
MOUNT_TARGET = "/app/database"

# Still the real registry path from the original command. Rename the string
# if local search is actually a different image.
SOLVER_IMAGE = "ghcr.io/robust-rail-nl/hip:latest"


def to_container_path(host_path):
    """
    Map a host path under GENERATE_DIR to its path inside the container,
    which sees that folder as /app/database.

        <GENERATE_DIR>/scenarios/foo.json  ->  /app/database/scenarios/foo.json
    """
    rel = os.path.relpath(host_path, MOUNT_SOURCE).replace(os.sep, "/")
    return f"{MOUNT_TARGET}/{rel}"


# Fixed tuning block, identical for every config. Only the three paths vary.
CONFIG_TEMPLATE = """LocationPath: "{location}"
ScenarioPath: "{scenario}"
PlanPath: "{plan}"
Mode: "{mode}"
DebugLevel: {debug_level}
TabuSearch:
  Iterations: 40
  IterationsUntilReset: 100
  TabuListLength: 16
  Bias: 0.5
SimulatedAnnealing:
  MaxDuration: 3600
  StopWhenFeasible: true
  IterationsUntilReset: 150000
  T: 15
  A: 0.97
  Q: 2000
  Reset: 2000
  Bias: 0.2
  IntensifyOnImprovement: false
"""


def generate_config(scenario_path, plan_path, mode="Standard", debug_level=1):
    """Write a config for one scenario+plan pair and return its host path."""
    os.makedirs(CONFIG_DIR, exist_ok=True)

    # Name the config after the scenario's path relative to SCENARIOS_DIR so
    # nested scenarios (e.g. 3trains/order1/scenario_solver1.json) don't collide.
    scenario_id = os.path.splitext(os.path.relpath(scenario_path, SCENARIOS_DIR))[0].replace(os.sep, "_")
    config_path = os.path.join(CONFIG_DIR, f"{scenario_id}.yaml")

    config_text = CONFIG_TEMPLATE.format(
        location=to_container_path(LOCATION_FILE),
        scenario=to_container_path(scenario_path),
        plan=to_container_path(plan_path),
        mode=mode,
        debug_level=debug_level,
    )

    with open(config_path, "w", encoding="utf-8") as f:
        f.write(config_text)

    logger.debug("wrote config %s", config_path)
    return config_path


def solve(scenario_path, plan_path, mode="Standard", debug_level=1):
    """
    Generate a config for this scenario+plan pair and run the local search
    solver container against it. Returns a dict with stdout and config path.
    """
    config_path = generate_config(scenario_path, plan_path, mode=mode, debug_level=debug_level)

    cmd = [
        "docker", "run", "--rm",
        "--mount", f"type=bind,source={MOUNT_SOURCE},target={MOUNT_TARGET}",
        SOLVER_IMAGE,
        f"--config={to_container_path(config_path)}",
    ]

    logger.info("      running local search solver")
    logger.debug("solver command: %s", " ".join(cmd))

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )

    if result.stdout:
        logger.debug("solver output:\n%s", result.stdout)

    if result.returncode != 0:
        raise RuntimeError(f"local search solver failed with exit code {result.returncode}")

    return {"stdout": result.stdout, "config_path": config_path}