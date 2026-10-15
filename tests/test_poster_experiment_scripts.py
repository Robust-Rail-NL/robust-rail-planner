"""Tests for the result-reading scripts in experiments/poster-planner-vs-solver/scripts.

They are run over a tiny fake results directory with the same layout as a
run_experiment.py output, covering each failure cause the poster panels rely on.
"""

import csv
import json
import os
import sys

import pytest

SCRIPTS = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "experiments",
    "poster-planner-vs-solver", "scripts",
)
sys.path.insert(0, SCRIPTS)
import results_lib as rl  # noqa: E402


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content if isinstance(content, str) else json.dumps(content))


def _attempt(results, instance, tool, solved, trace="", out="", reason="", **result):
    sub = "planning" if tool == "planner" else os.path.join("local_search", "seed1")
    d = os.path.join(results, instance, sub)
    result.setdefault("plan_produced", True)
    result.setdefault("start_time", "2026-10-14T21:48:21+00:00")
    _write(os.path.join(d, "result.json"), result)
    if solved is not None:
        _write(os.path.join(d, "eval_result.json"), {"solved": solved, "reason": reason})
    if trace:
        _write(os.path.join(d, "eval.txt"), trace)
    if out:
        _write(os.path.join(d, "eval.out"), out)


@pytest.fixture
def results(tmp_path):
    r = str(tmp_path)
    _write(os.path.join(r, "experiment.json"), {"max_duration": 300, "planner": "symbolic-rail"})
    _attempt(r, "custom_1_FIFO_0", "planner", True)
    _attempt(r, "custom_2_FIFO_0", "planner", False, trace="Trains's departure mismatch\n")
    _attempt(r, "custom_3_random_0", "planner", None, plan_produced=False, timed_out=True)
    _attempt(r, "custom_4_LIFO_0", "planner", False, out="Track 906a is reserved\n")
    _attempt(r, "custom_5_FIFO_0", "planner", None, plan_produced=False, timed_out=False)
    _attempt(r, "custom_1_FIFO_0", "solver", True)
    _attempt(r, "custom_2_FIFO_0", "solver", False,
             reason="Adding ShuntingUnit-1 to Track 906b exceeds the maximum length (270.6 > 255.0)")
    _attempt(r, "custom_3_random_0", "solver", False,
             reason="Error in generating action (Combine: [4] and [5]): The action is invalid.")
    rows = [("custom_1_FIFO_0", "planning", "yes", 20), ("custom_2_FIFO_0", "planning", "no", 23),
            ("custom_3_random_0", "planning", "no", 300), ("custom_4_LIFO_0", "planning", "no", 40),
            ("custom_5_FIFO_0", "planning", "no", 12), ("custom_1_FIFO_0", "local_search_seed1", "yes", 2),
            ("custom_2_FIFO_0", "local_search_seed1", "no", 3),
            ("custom_3_random_0", "local_search_seed1", "no", 301)]
    with open(os.path.join(r, "runs.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["instance", "tool", "seed", "plan_found", "timed_out", "valid_plan", "seconds"])
        for instance, tool, valid, secs in rows:
            w.writerow([instance, tool, "", "yes", "no", valid, secs])
    return r


@pytest.mark.parametrize("instance, tool, cause", [
    ("custom_1_FIFO_0", "planner", None),
    ("custom_2_FIFO_0", "planner", "late/early departure"),
    ("custom_3_random_0", "planner", "timeout"),
    ("custom_4_LIFO_0", "planner", "track conflict"),
    ("custom_5_FIFO_0", "planner", "no plan (not a timeout)"),
    ("custom_2_FIFO_0", "solver", "unit longer than track"),
    ("custom_3_random_0", "solver", "invalid Combine action"),
])
def test_failure_cause(results, instance, tool, cause):
    assert rl.failure_cause(results, instance, tool) == cause


def test_per_run_table_has_one_row_per_run(results):
    table = rl.per_run_table(results)
    assert len(table) == 8
    assert {r["tool"] for r in table} == {"planner", "solver"}
    planner_ok = [r for r in table if r["tool"] == "planner" and r["accepted"]]
    assert [r["instance"] for r in planner_ok] == ["custom_1_FIFO_0"]
    assert all(r["failure_cause"] == "" for r in table if r["accepted"])
    assert all(r["failure_cause"] for r in table if not r["accepted"])


def test_poster_page_builds(results):
    import make_poster_page
    html = make_poster_page.build(results, timing_note="a note")
    assert "<svg" in html and "a note" in html and "FIFO" in html


def _repeat_dir(tmp_path, name, rows):
    d = tmp_path / name
    d.mkdir()
    _write(str(d / "experiment.json"), {"max_duration": 300})
    with open(d / "runs.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["instance", "tool", "seed", "plan_found", "timed_out", "valid_plan", "seconds"])
        for instance, valid, secs in rows:
            w.writerow([instance, "planning", "", "yes", "no", valid, secs])
    return str(d)


def test_repeated_planner_medians_use_scenarios_solved_in_every_repetition(tmp_path):
    import make_poster_page
    rep1 = _repeat_dir(tmp_path, "rep1", [("custom_1_FIFO_0", "yes", 20), ("custom_1_FIFO_1", "yes", 22),
                                          ("custom_2_FIFO_0", "yes", 40)])
    rep2 = _repeat_dir(tmp_path, "rep2", [("custom_1_FIFO_0", "yes", 30), ("custom_1_FIFO_1", "no", 300),
                                          ("custom_2_FIFO_0", "yes", 50)])
    # custom_1_FIFO_1 failed in rep2 and is left out; the others use their own median over the reps.
    assert make_poster_page.repeated_planner_medians([rep1, rep2]) == {1: 25, 2: 45}


def test_poster_page_uses_repeats_for_planner_time(results, tmp_path):
    import make_poster_page
    rep = _repeat_dir(tmp_path, "rep", [("custom_1_FIFO_0", "yes", 77)])
    html = make_poster_page.build(results, repeat_dirs=[rep])
    assert ">77<" in html
    assert "median over 1 repetition of those" in html


def test_analyze_run_handles_a_planner_only_run(tmp_path, capsys):
    import analyze_run
    rep = _repeat_dir(tmp_path, "rep", [("custom_1_FIFO_0", "yes", 20), ("custom_2_FIFO_0", "no", 30)])
    analyze_run.print_accepted(analyze_run.tables(rep))
    out = capsys.readouterr().out
    assert "planner" in out and "1/2 = 50%" in out
