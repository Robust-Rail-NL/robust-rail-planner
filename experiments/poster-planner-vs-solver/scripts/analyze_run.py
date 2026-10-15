#!/usr/bin/env python3
"""Summarise a planner-vs-solver run: accepted rate and median time by train
count and matching, and the failure causes of each tool.

    python3 analyze_run.py <results-dir> [--per-run per_run.csv]

Only reads <results-dir>. `--per-run` writes one row per (scenario, tool) with the
outcome, run time, start time and failure cause.
"""

import argparse
import collections
import csv
import statistics

import results_lib as rl


def tables(results):
    rows = rl.runs(results)
    cells = collections.defaultdict(lambda: {"n": 0, "ok": 0, "secs": []})
    for r in rows:
        c = cells[(r["tool"], r["trains"], r["matching"])]
        c["n"] += 1
        if r["accepted"]:
            c["ok"] += 1
            c["secs"].append(r["seconds"])
    return cells


def print_accepted(cells):
    trains = sorted({k[1] for k in cells})
    matchings = [m for m in rl.MATCHINGS if any(k[2] == m for k in cells)]
    print("Accepted / total (median seconds of accepted runs)")
    print("trains " + "".join(f"| {m} {t} " for m in matchings for t in rl.TOOLS))
    for n in trains:
        line = f"{n:6d} "
        for m in matchings:
            for t in rl.TOOLS:
                c = cells[(t, n, m)]
                med = f"{statistics.median(c['secs']):.0f}s" if c["secs"] else "-"
                line += f"| {c['ok']}/{c['n']} ({med}) "
        print(line)
    print("\nTotals")
    for m in matchings:
        for t in rl.TOOLS:
            ok = sum(cells[(t, n, m)]["ok"] for n in trains)
            n_all = sum(cells[(t, n, m)]["n"] for n in trains)
            print(f"  {m:6s} {t:8s} {ok}/{n_all} = {100 * ok / n_all:.0f}%")
    for t in rl.TOOLS:
        ok = sum(c["ok"] for k, c in cells.items() if k[0] == t)
        n_all = sum(c["n"] for k, c in cells.items() if k[0] == t)
        print(f"  all    {t:8s} {ok}/{n_all} = {100 * ok / n_all:.0f}%")


def print_causes(table):
    for t in rl.TOOLS:
        failed = [r for r in table if r["tool"] == t and r["failure_cause"]]
        by_cause = collections.Counter(r["failure_cause"] for r in failed)
        print(f"\n{t} failure causes (total {len(failed)})")
        for cause, n in by_cause.most_common():
            per_train = collections.Counter(r["trains"] for r in failed if r["failure_cause"] == cause)
            print(f"  {n:3d}  {cause}   by trains: {dict(sorted(per_train.items()))}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results")
    ap.add_argument("--per-run", metavar="CSV")
    args = ap.parse_args()
    table = rl.per_run_table(args.results)
    print_accepted(tables(args.results))
    print_causes(table)
    if args.per_run:
        with open(args.per_run, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(table[0]))
            w.writeheader()
            w.writerows(table)


if __name__ == "__main__":
    main()
