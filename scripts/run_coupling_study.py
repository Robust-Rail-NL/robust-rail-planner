import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


MODES = [
    "implicit_free_uncoupling",
    "implicit_explicit_uncoupling",
    "explicit_coupling",
]

DEFAULT_SCENARIOS = [
    "scenario_solver_example1.json",
    "scenario_solver_example2.json",
    "scenario_solver_example3.json",
]


def default_java():
    microsoft_java = Path(r"C:\Program Files\Microsoft\jdk-17.0.18.8-hotspot\bin\java.exe")
    if microsoft_java.exists():
        return str(microsoft_java)
    return "java"


def parse_metric(output, label):
    matches = re.findall(rf"{re.escape(label)}:\s*([^\r\n]+)", output)
    return matches[-1].strip() if matches else None


def parse_result(output):
    solved = "Problem Solved" in output
    unsolvable = "Problem unsolvable" in output or "Problem Detected as Unsolvable" in output
    return {
        "solved": solved,
        "unsolvable": unsolvable,
        "plan_length": parse_metric(output, "Plan-Length"),
        "metric_search": parse_metric(output, "Metric (Search)"),
        "planning_time_ms": parse_metric(output, "Planning Time (msec)"),
        "expanded_nodes": parse_metric(output, "Expanded Nodes"),
        "states_evaluated": parse_metric(output, "States Evaluated"),
        "dead_ends": parse_metric(output, "Number of Dead-Ends detected"),
        "duplicates": parse_metric(output, "Number of Duplicates detected"),
        "ground_actions": parse_metric(output, "|A|"),
    }


def run_command(command, cwd):
    print()
    print("$ " + " ".join(str(part) for part in command))
    completed = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    if completed.stdout:
        print(completed.stdout)
    if completed.stderr:
        print(completed.stderr, file=sys.stderr)
    return completed


def write_summary(path, summary):
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Generate and run the coupling/uncoupling PDDL modelling ladder.")
    parser.add_argument("--scenarios", nargs="+", default=DEFAULT_SCENARIOS)
    parser.add_argument("--modes", nargs="+", choices=MODES, default=MODES)
    parser.add_argument("--no-run", action="store_true", help="Only generate PDDL files; do not run ENHSP.")
    parser.add_argument("--java", default=default_java())
    parser.add_argument("--enhsp-jar", default=None)
    parser.add_argument("--scenario-folder", default=None)
    parser.add_argument("--output-folder", default=None)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    workspace_root = repo_root.parents[1]
    convert_script = repo_root / "src" / "convert" / "convert.py"

    scenario_folder = Path(args.scenario_folder) if args.scenario_folder else workspace_root / "Robust-Rail-NL" / "scenario-planning-inputs" / "Location_KleineBinckhorst"
    output_folder = Path(args.output_folder) if args.output_folder else repo_root / "data" / "coupling-study"
    enhsp_jar = Path(args.enhsp_jar) if args.enhsp_jar else workspace_root / "public" / "tusp-pddl-experiments-setups" / "ENHSP-Public" / "enhsp-dist" / "enhsp.jar"

    output_folder.mkdir(parents=True, exist_ok=True)
    all_summaries = []

    for scenario in args.scenarios:
        scenario_name = Path(scenario).stem
        for mode in args.modes:
            run_id = f"{scenario_name}__{mode}"
            run_folder = output_folder / run_id
            run_folder.mkdir(parents=True, exist_ok=True)

            problem_file = run_folder / "problem.pddl"
            domain_file = run_folder / "domain.pddl"
            plan_file = run_folder / "plan.txt"
            summary_file = run_folder / "summary.json"

            convert_command = [
                sys.executable,
                str(convert_script),
                "-p",
                str(scenario_folder),
                "-s",
                scenario,
                "-o",
                str(problem_file),
                "-d",
                str(domain_file),
                "--coupling-mode",
                mode,
            ]
            convert_result = run_command(convert_command, repo_root)
            if convert_result.returncode != 0:
                write_summary(summary_file, {"run_id": run_id, "scenario": scenario, "mode": mode, "conversion_failed": True})
                continue

            summary = {
                "run_id": run_id,
                "scenario": scenario,
                "mode": mode,
                "problem_file": str(problem_file),
                "domain_file": str(domain_file),
            }

            if not args.no_run:
                planner_command = [
                    args.java,
                    "-jar",
                    str(enhsp_jar),
                    "-sp",
                    str(plan_file),
                    "-h",
                    "hmax",
                    "-s",
                    "wa_star_4",
                    "-o",
                    str(domain_file),
                    "-f",
                    str(problem_file),
                ]
                planner_result = run_command(planner_command, repo_root)
                summary.update(parse_result(planner_result.stdout + planner_result.stderr))
                summary["planner_returncode"] = planner_result.returncode

            write_summary(summary_file, summary)
            all_summaries.append(summary)

    write_summary(output_folder / "summary.json", all_summaries)


if __name__ == "__main__":
    main()
