import argparse
import logging
import os
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.convert.corridor_no_switch_unlimited_order_servicing_discrete.convert import create_instance_from_scenario


parser = argparse.ArgumentParser()
parser.add_argument("-p", "--path-to-folder", default=None)
parser.add_argument("-s", "--scenario-file", default="scenario_solver_example1.json")
parser.add_argument("-l", "--location-file", default="location_solver.json")
parser.add_argument("-o", "--output-file", default=None)
parser.add_argument("-d", "--domain-file", default=None)
parser.add_argument("--matching-variant", type=int, default=0)
parser.add_argument("--log-level", default="ERROR")


if __name__ == "__main__":
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level.upper())
    create_instance_from_scenario(
        domain_file="domain.pddl" if args.domain_file is None else args.domain_file,
        path_to_folder=args.path_to_folder,
        scenario_file=args.scenario_file,
        location_file=args.location_file,
        output_file=args.output_file,
        precompute_matching=True,
        matching_variant=args.matching_variant,
        matching_strategy="order_preserving_auto",
    )
