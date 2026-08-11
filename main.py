#!/usr/bin/env python3
"""Container entrypoint for the planner step (an alternative to the HIP solver).

Converts a unified scenario to PDDL, runs the planner, and converts the
resulting plan back to TORS JSON. The three stages live in convert_to_pddl/,
plan/ and convert_plan_to_tors/ respectively; this module only chains them.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

# The stage packages are siblings of this file, so the repo root has to be
# importable. It happens to be the working directory in the image, but not when
# main.py is invoked by absolute path from elsewhere — which is exactly what the
# tests do.
sys.path.insert(0, REPO_ROOT)

from convert_to_pddl.corridor_no_switch_unlimited_order_servicing_discrete_compiled_matching.convert import create_instance_from_scenario  # noqa: E402
from convert_plan_to_tors.convert_to_tors import convert_plan, ScheduleInfeasibleError  # noqa: E402
from plan.validate_plan import validate_plan  # noqa: E402

PLAN_DIR = os.path.join(REPO_ROOT, "plan")
PLANNER_SCRIPTS = {
    "symbolic": os.path.join(PLAN_DIR, "symbolic_planner.jl"),
    "symbolic-rail": os.path.join(PLAN_DIR, "symbolic_planner.jl"),
    "enhsp": os.path.join(PLAN_DIR, "enhsp_planner.jl"),
}


def convert_to_pddl(location_path, scenario_path, domain_out, problem_out):
    create_instance_from_scenario(
        location_file=location_path,
        scenario_file=scenario_path,
        domain_file=domain_out,
        output_file=problem_out,
    )


def run_planner(domain_path, problem_path, planner, plan_out):
    script = PLANNER_SCRIPTS[planner]
    command = [
        "julia", f"--project={PLAN_DIR}", script,
        domain_path, problem_path, plan_out,
    ]
    if planner.startswith("symbolic"):
        command.append(planner)
    subprocess.run(
        command,
        check=True,
    )


def convert_plan_to_tors(plan_path, scenario_path, location_path):
    return convert_plan(plan_path, scenario_path, location_path)


def main():
    parser = argparse.ArgumentParser(
        description="Planner step: scenario -> plan (TORS JSON)")
    parser.add_argument("--location", required=True,
                        help="Path to location.json (inside the container, when containerised)")
    parser.add_argument("--scenario", required=True,
                        help="Path to a scenario_*.json file (inside the container, "
                             "when containerised)")
    parser.add_argument(
        "--planner",
        choices=["symbolic", "symbolic-rail", "enhsp"],
        default="symbolic",
    )
    # Required, though the help text used to promise stdout when omitted: the
    # Julia planner inherits this process's stdout and prints progress to it, so
    # a plan written there would arrive interleaved with search output and be
    # unparseable. Routing the planner to stderr instead would work, but
    # run_planner.py treats a non-empty .err as a signal worth reporting.
    parser.add_argument("--output", required=True,
                        help="Path to write the resulting TORS plan JSON.")
    args = parser.parse_args()

    tmp_dir = tempfile.mkdtemp(prefix="planner-")
    domain_pddl = os.path.join(tmp_dir, "domain.pddl")
    problem_pddl = os.path.join(tmp_dir, "problem.pddl")
    raw_plan = os.path.join(tmp_dir, "plan.pddl")

    convert_to_pddl(args.location, args.scenario, domain_pddl, problem_pddl)
    run_planner(domain_pddl, problem_pddl, args.planner, raw_plan)

    if not validate_plan(domain_pddl, problem_pddl, raw_plan):
        print("Refusing to convert an invalid plan to TORS format.", file=sys.stderr)
        sys.exit(1)

    try:
        tors_plan = convert_plan_to_tors(raw_plan, args.scenario, args.location)
    except ScheduleInfeasibleError as exc:
        for problem in exc.problems:
            print("PROBLEM:", problem, file=sys.stderr)
        print(
            "Plan is schedule-infeasible (%d problem(s)); exiting with error."
            % len(exc.problems),
            file=sys.stderr,
        )
        sys.exit(1)

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(tors_plan, f, indent=4)

    return tors_plan


if __name__ == "__main__":
    main()
