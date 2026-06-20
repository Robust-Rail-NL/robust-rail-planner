import argparse
import logging
from robust_rail_planning import run_pipeline, setup_logging


def main():
    parser = argparse.ArgumentParser(description="Robust Rail NL PDDL planning pipeline")
    parser.add_argument("--generate", action="store_true",
                        help="(re)generate scenarios before planning")
    parser.add_argument("--subproblem", choices=["parking", "matching", "combined"],
                        default="parking")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--examples", action="store_true",
                        help="Run only scenario_solver_example*.json files")
    parser.add_argument("--planner", choices=["astar", "enhsp"],
                        default="astar",
                        help="Planner backend to use (default: astar)")
    
    parser.add_argument("--solve", action="store_true",
                        help="Run the local search solver on each plan")
    
    args = parser.parse_args()


    setup_logging(getattr(logging, args.log_level.upper()))

    run_pipeline(
        do_generate=args.generate,
        use_examples=args.examples,
        planner=args.planner,
        do_local_search=args.solve,
    )


if __name__ == "__main__":
    main()
