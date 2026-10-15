#!/usr/bin/env python3
"""Does the planner slow down at the start of a sweep?

    python3 startup_effect.py <results-dir>

Groups the planner's runs by the minute they started and prints, per minute, how
many started, which train counts they cover, how many accepted runs on up to four
trains took longer than 45 s, and the median run time. In the poster sweeps the
runs of the first minute (all one-train scenarios) were much slower than the same
kind of run later, so the planner's one-train median is not comparable with the
others. Only reads <results-dir>.
"""

import collections
import statistics
import sys

import results_lib as rl

SLOW_SECONDS = 45


def main(results):
    exp = rl.load_experiment(results)
    rows = [r for r in rl.runs(results) if r["tool"] == "planner"]
    for r in rows:
        r["start"] = rl.start_time(results, r["instance"], "planner")
    rows = sorted((r for r in rows if r["start"]), key=lambda r: r["start"])
    print(f"{len(rows)} planner runs, budget {exp.get('max_duration')} s, first start {rows[0]['start'][:19]}")
    by_minute = collections.defaultdict(list)
    for r in rows:
        by_minute[r["start"][11:16]].append(r)
    print("minute  runs  trains    slow  median_s")
    for minute, group in sorted(by_minute.items()):
        slow = sum(1 for r in group if r["accepted"] and r["seconds"] > SLOW_SECONDS and r["trains"] <= 4)
        trains = sorted({r["trains"] for r in group})
        median = statistics.median(r["seconds"] for r in group)
        print(f"{minute} {len(group):5d}  {str(trains):9s} {slow:4d}  {median:6.0f}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
