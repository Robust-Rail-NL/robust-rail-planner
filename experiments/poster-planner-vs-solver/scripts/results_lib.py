"""Read a run_experiment.py results directory and classify why each run failed.

Used by analyze_run.py, startup_effect.py and make_poster_page.py. Only reads the
results directory; nothing here writes into it.

A results directory holds one folder per scenario, `custom_<trains>_<matching>_<n>/`,
with the planner's attempt in `planning/` and the solver's in `local_search/seed<k>/`,
plus the summary files `runs.csv`, `experiment.json` and so on.
"""

import csv
import glob
import json
import os
import re

TOOLS = ("planner", "solver")
MATCHINGS = ("FIFO", "LIFO", "random")


def parse_instance(name):
    """`custom_3_FIFO_7` -> (3, 'FIFO', 7)."""
    m = re.match(r"custom_(\d+)_(\w+)_(\d+)$", name)
    return int(m[1]), m[2], int(m[3])


def tool_of(runs_csv_tool):
    """runs.csv calls the solver `local_search_seed<k>` and the planner `planning`."""
    return "solver" if runs_csv_tool.startswith("local") else "planner"


def attempt_dir(results, instance, tool):
    """The folder holding one tool's attempt on one scenario."""
    if tool == "planner":
        return os.path.join(results, instance, "planning")
    seeds = sorted(glob.glob(os.path.join(results, instance, "local_search", "seed*")))
    return seeds[0] if seeds else os.path.join(results, instance, "local_search")


def _json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _text(path):
    try:
        with open(path, errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


def load_experiment(results):
    return _json(os.path.join(results, "experiment.json"))


def accepted(results, instance, tool):
    """True if the TORS evaluator accepted this tool's plan. A run that never
    reached the evaluator (timeout, no plan) is not accepted."""
    return bool(_json(os.path.join(attempt_dir(results, instance, tool),
                                   "eval_result.json")).get("solved"))


def failure_cause(results, instance, tool):
    """A short label for why a run was not accepted, read from the attempt's own
    files. Returns None for an accepted run.

    runs.csv and the runner's own failure csv label most evaluator rejections as
    the generic "plan rejected"; the real reason is in eval.txt (departure
    mismatch, evaluator no-progress abort) or in eval.out / eval.err.
    """
    if accepted(results, instance, tool):
        return None
    d = attempt_dir(results, instance, tool)
    res = _json(os.path.join(d, "result.json"))
    verdict = _json(os.path.join(d, "eval_result.json"))
    trace = _text(os.path.join(d, "eval.txt"))
    out = _text(os.path.join(d, "eval.out")) + _text(os.path.join(d, "eval.err"))
    reason = verdict.get("reason") or ""
    if res.get("timed_out"):
        return "timeout"
    if not verdict and not res.get("plan_produced"):
        return "no plan (not a timeout)"
    if "Issue detected with the Scenario" in out:
        return "scenario infeasible"
    if "departure mismatch" in trace or "should leave at time" in reason + trace:
        return "late/early departure"
    if "no progress" in trace:
        return "evaluator no-progress abort"
    if re.search(r"Track \S+ is reserved", reason) or re.search(r"Track \S+ is reserved", out):
        return "track conflict"
    if "exceeds the maximum length" in reason + trace + out:
        return "unit longer than track"
    if "Parking is not allowed" in reason + trace + out:
        return "parking not allowed"
    m = re.search(r"Error in generating action \((\w+)", reason + trace + out)
    if m:
        return f"invalid {m[1]} action"
    return "other"


def runs(results):
    """One dict per runs.csv row, with trains, matching and tool filled in."""
    with open(os.path.join(results, "runs.csv")) as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["trains"], r["matching"], _ = parse_instance(r["instance"])
        r["tool"] = tool_of(r["tool"])
        r["accepted"] = r["valid_plan"] == "yes"
        r["seconds"] = float(r["seconds"])
    return rows


def start_time(results, instance, tool):
    return _json(os.path.join(attempt_dir(results, instance, tool), "result.json")).get("start_time", "")


def per_run_table(results):
    """Rows for per_run.csv: one per (scenario, tool)."""
    table = []
    for r in runs(results):
        table.append({
            "instance": r["instance"], "trains": r["trains"], "matching": r["matching"],
            "tool": r["tool"], "accepted": int(r["accepted"]), "seconds": r["seconds"],
            "start_time": start_time(results, r["instance"], r["tool"]),
            "failure_cause": failure_cause(results, r["instance"], r["tool"]) or "",
        })
    return sorted(table, key=lambda t: (t["tool"], t["trains"], t["matching"], t["instance"]))
