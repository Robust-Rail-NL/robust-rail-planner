#!/usr/bin/env python3
"""Container entrypoint for the planner step (replaces the HIP solver step).

Converts a solver-format scenario to PDDL, runs the planner, and converts the
resulting plan back to TORS JSON. Mirrors the old planning-approach's
src/convert, src/plan/planner.jl, and src/robust_rail_planning/converter.py —
not yet ported here, see TODOs below.
"""
from convert_to_pddl.corridor_no_switch_unlimited_order_servicing_discrete_compiled_matching.convert import create_instance_from_scenario
from convert_plan_to_tors.convert_to_tors import convert_plan
from plan.validate_plan import validate_plan
import argparse
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
PLAN_DIR = os.path.join(REPO_ROOT, "plan")
PLANNER_SCRIPTS = {
    "symbolic": os.path.join(PLAN_DIR, "symbolic_planner.jl"),
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
    subprocess.run(
        ["julia", f"--project={PLAN_DIR}", script, domain_path, problem_path, plan_out],
        check=True,
    )


def convert_plan_to_tors(plan_path, scenario_path, location_path):
    return convert_plan(plan_path, scenario_path, location_path)


def main():
    parser = argparse.ArgumentParser(
        description="Planner step: scenario -> plan (TORS JSON)")
    parser.add_argument("--location", required=True,
                        help="Path to location_solver.json inside the container")
    parser.add_argument("--scenario", required=True,
                        help="Path to scenario_solver_*.json inside the container")
    parser.add_argument("--planner", choices=["symbolic", "enhsp"], default="symbolic")
    parser.add_argument("--output",
                        help="Path to write the resulting TORS plan JSON. "
                             "If omitted, the plan JSON is printed to stdout.")
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

    tors_plan = convert_plan_to_tors(raw_plan, args.scenario, args.location)

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(tors_plan, f, indent=4)

    return tors_plan


if __name__ == "__main__":
    main()
