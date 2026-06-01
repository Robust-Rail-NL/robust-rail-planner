import os
import sys
import subprocess
import csv
from datetime import datetime

# --- Paths ---
script_dir      = os.path.dirname(__file__)
scenarios_dir   = os.path.abspath(os.path.join(script_dir, "..", "generate", "scenarios"))
location_file   = os.path.abspath(os.path.join(script_dir, "..", "generate", "location.json"))
convert_dir     = os.path.abspath(os.path.join(script_dir, "..", "convert"))
data_dir        = os.path.abspath(os.path.join(script_dir, "..", "data"))
path_to_planner = os.path.abspath(os.path.join(script_dir, "..", "plan", "planner.jl"))
TORS_BIN        = os.path.abspath(os.path.join(script_dir, "..", "robust-rail-evaluator", "build", "TORS"))


def evaluate_plan(
    solver_path: str,
    plan_path: str,
    result_txt: str,
    location_file: str = location_file,
    departure_delay: int = 0,
    plan_type: str = "Solver",
) -> bool:
    """
    Evaluate a single plan against a scenario using the TORS evaluator.

    Args:
        solver_path:     Path to the scenario JSON file.
        plan_path:       Path to the plan file (.plan).
        result_txt:      Path where the evaluation result .txt will be written.
        location_file:   Path to location.json (the file itself, not the folder).
        departure_delay: Allowed departure delay in seconds (default 0).
        plan_type:       "Solver" or "Evaluator" (default "Solver").

    Returns:
        True if the evaluator exited successfully, False otherwise.
    """
    if not os.path.isfile(plan_path):
        print(f"[evaluate_plan] Plan file not found: {plan_path}")
        return False

    os.makedirs(os.path.dirname(result_txt), exist_ok=True)

    eval_proc = subprocess.run([
        TORS_BIN,
        "--mode",             "EVAL_AND_STORE",
        "--path_location",    os.path.dirname(location_file),
        "--path_scenario",    solver_path,
        "--path_plan",        plan_path,
        "--path_eval_result", result_txt,
        "--departure_delay",  str(departure_delay),
        "--plan_type",        plan_type,
    ], capture_output=True, text=True)

    if eval_proc.returncode != 0:
        print(f"[evaluate_plan] Evaluator error for {os.path.basename(plan_path)}:\n{eval_proc.stderr}")
        return False

    return True


def run_pipeline():
    # --- Run folder (one per script invocation) ---
    run_name = datetime.now().strftime("run_%Y%m%d_%H%M%S")
    run_dir  = os.path.join(data_dir, "runs", run_name)
    os.makedirs(run_dir, exist_ok=True)
    print(f"Run: {run_name}  →  {run_dir}")

    convert_variants = [d for d in os.listdir(convert_dir) if os.path.isdir(os.path.join(convert_dir, d))]

    for variant_name in convert_variants:
        variant_dir = os.path.join(convert_dir, variant_name)
        domain_path = os.path.abspath(os.path.join(variant_dir, "domain.pddl"))

        converter_files = [f for f in os.listdir(variant_dir) if f.startswith("convert_") and f.endswith(".py")]
        if not converter_files:
            print(f"[{variant_name}] No converter found, skipping...")
            continue
        path_to_converter = os.path.join(variant_dir, converter_files[0])

        variant_run_dir = os.path.join(run_dir, variant_name)
        os.makedirs(variant_run_dir, exist_ok=True)

        summary_rows = []

        for n_trains in os.listdir(scenarios_dir):
            for order_name in os.listdir(os.path.join(scenarios_dir, n_trains)):
                solver_dir = os.path.join(scenarios_dir, n_trains, order_name, "solver")
                if not os.path.isdir(solver_dir):
                    continue

                pddl_dir = os.path.join(scenarios_dir, n_trains, order_name, "pddl", variant_name)
                os.makedirs(pddl_dir, exist_ok=True)

                for file in os.listdir(solver_dir):
                    if not (file.startswith("scenario_solver_") and file.endswith(".json")):
                        continue

                    solver_path = os.path.join(solver_dir, file)
                    pddl_path   = os.path.abspath(os.path.join(pddl_dir, file.replace(".json", ".pddl")))
                    plan_path   = pddl_path.replace(".pddl", ".plan")

                    # --- Convert ---
                    print(f"[{variant_name}] Converting {file}...")
                    subprocess.run([sys.executable, path_to_converter,
                        "-s", solver_path,
                        "-l", location_file,
                        "-o", pddl_path,
                        "-d", domain_path,
                    ])

                    # --- Plan ---
                    print(f"[{variant_name}] Planning {file}...")
                    subprocess.run(["julia", "--project", path_to_planner, domain_path, pddl_path])

                    # --- Evaluate ---
                    if not os.path.isfile(plan_path):
                        print(f"[{variant_name}] No plan output for {file}, skipping evaluation.")
                        summary_rows.append({
                            "variant":          variant_name,
                            "n_trains":         n_trains,
                            "order":            order_name,
                            "scenario":         file,
                            "plan_found":       False,
                            "eval_success":     None,
                            "eval_result_file": "",
                        })
                        continue

                    result_subdir = os.path.join(variant_run_dir, n_trains, order_name)
                    result_txt    = os.path.join(result_subdir, file.replace(".json", "_eval.txt"))

                    print(f"[{variant_name}] Evaluating {file}...")
                    eval_ok = evaluate_plan(
                        solver_path=solver_path,
                        plan_path=plan_path,
                        result_txt=result_txt,
                    )

                    summary_rows.append({
                        "variant":          variant_name,
                        "n_trains":         n_trains,
                        "order":            order_name,
                        "scenario":         file,
                        "plan_found":       True,
                        "eval_success":     eval_ok,
                        "eval_result_file": result_txt,
                    })

        # --- Summary CSV for this variant ---
        summary_path = os.path.join(variant_dir, f"summary_{run_name}.csv")
        fieldnames = ["variant", "n_trains", "order", "scenario", "plan_found", "eval_success", "eval_result_file"]
        with open(summary_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(summary_rows)

        total  = len(summary_rows)
        found  = sum(r["plan_found"] for r in summary_rows)
        passed = sum(r["eval_success"] is True for r in summary_rows)
        print(f"[{variant_name}] Done — {passed}/{found}/{total} eval passed / plans found / total")
        print(f"[{variant_name}] Summary → {summary_path}")

    print(f"\nAll variants complete. Run folder: {run_dir}")


if __name__ == "__main__":
    run_pipeline()