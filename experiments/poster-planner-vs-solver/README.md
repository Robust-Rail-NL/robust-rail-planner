# Planner vs solver on generated Kleine Binckhorst scenarios

This folder holds the data and scripts behind the three poster panels (one table, two charts) that compare the planner with the solver (`robust-rail-solver`, called "local search" in the runner's output).

## What was run

- **Location:** Kleine Binckhorst, no servicing.
- **Scenarios:** 1 to 5 trains, FIFO, LIFO and random matching, 20 scenarios per cell, so 300 scenarios in total. Generator seed 42. The full generator configuration is `scenario_config` in `experiment.json`.
- **Tools:** the planner (`symbolic-rail`) and the solver ran on the identical 300 scenarios.
- **Budget:** `run-300s/` gives each tool 300 s per scenario and is the run the poster uses. `run-600s/` repeats the whole experiment with 600 s.
- **Success:** a scenario counts as solved only if the TORS evaluator accepts the plan. A tool producing a plan is not enough.
- **Timing repeats:** `run-timing-rep1/` to `run-timing-rep5/` repeat the planner on the 1 to 3 train scenarios (180 scenarios, 300 s, planner only) five times, each with its own shuffled task order and the same planner image. The poster's planner times for 1 to 3 trains come from these.
- **Where it ran:** the DelftBlue cluster, one array task per (scenario, tool) attempt, using `robust-rail-general`'s SLURM and apptainer path (see its `docs/slurm-apptainer.md`).

## Versions

| Part | Version |
|---|---|
| Planner | `ghcr.io/robust-rail-nl/planner:edge`, the same `.sif` file in both runs |
| Generator, solver, evaluator | the `stable` channel of each |

The planner image is identified in each attempt's `run_stamp.json` as `sif:448696320:1792009671000000000`, which is the size in bytes (448,696,320) and modification time (2026-10-14 20:27:51 UTC) of the staged `.sif` file. It is not a registry digest. The tip of `edge` at that time had a tree identical to commit `77bf765` (the end of `fix-whole-departure-and-arrival-options`, which is also what `main` contained after PR #53). I checked this with `git diff` between those commits, not against the image itself. The five timing repeats used the same `.sif` file. That image writes plans with `schemaVersion` 1 and emits no `Reverse` actions, and the evaluator accepts such plans.

## Results (300 s per scenario)

Accepted by the evaluator, out of 20 scenarios per cell:

| trains | FIFO planner | FIFO solver | LIFO planner | LIFO solver | random planner | random solver |
|---|---|---|---|---|---|---|
| 1 | 20 | 20 | 20 | 20 | 20 | 13 |
| 2 | 19 | 20 | 19 | 20 | 19 | 13 |
| 3 | 18 | 20 | 20 | 20 | 11 | 7 |
| 4 | 16 | 20 | 16 | 19 | 7 | 3 |
| 5 | 14 | 17 | 12 | 18 | 8 | 6 |
| all | 87 | 97 | 87 | 97 | 65 | 42 |

Overall the planner accepts 239 of 300 scenarios and the solver 236. The planner is clearly ahead on random matching (65% against 42%), and the solver is ahead on FIFO and LIFO (97% against 87%). The planner needs about 23 to 29 s per scenario, the solver about 2 to 3 s. The planner's times for 1 to 3 trains are medians over the five timing repeats; 4 and 5 trains come from the single sweep.

The planner's 61 failures: 33 late or early departures (the PDDL model has no notion of time, so a plan can be in an order that cannot meet a departure), 26 search timeouts, and 2 "track reserved" conflicts. Timeouts start at 3 trains and late departures at 2 trains.

## Things to know before using the numbers

- **The budget is a limit on the planner's numbers.** Some of the planner's timeouts could in principle solve with more time. In `run-600s/` none did: the planner accepts exactly the same 239 scenarios in both runs (the same set), and two of the 26 timeouts finished with a late departure instead. So the 300 s budget did not undercount the planner here.
- **Run times at the start of a sweep.** The runs that start in the first minute or two of a sweep can be much slower than later ones: in `run-300s/` the first 25 runs (all 1-train scenarios) took 55 to 72 s against about 22 s later. The cause has not been established. Acceptance is not affected. `run-*/startup_effect.txt` has the per-minute table. This is why the poster's 1 to 3 train planner times come from the shuffled repeats, where the slowdown is spread over all scenarios and shows up mainly in the first repetition.
- **`feasibility.csv` is not independent ground truth.** The runner marks a scenario feasible if some tool solved it, infeasible if the evaluator flagged the scenario itself, and unresolved otherwise. In these runs nothing was flagged infeasible.
- **Failure reasons.** The runner's own `planner_failures.csv` and `solver_failures.csv` label most evaluator rejections as "plan rejected". `per_run.csv` gives each failure a cause read from the attempt's `eval.txt`, `eval.out` and `eval.err` (see `scripts/results_lib.py` for the rules).
- **No servicing.** `perform_servicing` is false in `experiment.json`.

## Files

- `run-300s/`, `run-600s/`: `experiment.json` (the configuration), `runs.csv`, `feasibility.csv`, `planner_failures.csv`, `solver_failures.csv`, `coverage_analysis.txt` (the runner's own summary), `per_run.csv` (one row per scenario and tool, with outcome, run time, start time and failure cause), `summary.txt` (output of `analyze_run.py`) and `startup_effect.txt` (output of `startup_effect.py`).
- `run-timing-rep1/` to `run-timing-rep5/`: the same files for each timing repeat (planner only).
- `poster/`: the one-page summary built from `run-300s/`, as `poster-results.html` and `poster-results.pdf`.
- `scripts/`: the scripts below.
- `../../tests/test_poster_experiment_scripts.py`: tests for the scripts over a small fake results directory.

The raw results directories (every scenario, plan and TORS trace, about 58 MB per sweep) are not stored in git. They will be attached to a GitHub release of this repository as one tarball, `poster-sweeps-raw.tar.gz`, containing `run-300s/`, `run-600s/` and `timing-rep1/` to `timing-rep5/`; the release link and checksum are added here when it exists. The scripts below read such a directory, so with the raw data they reproduce everything in this folder.

## Reproducing the tables and charts

The scripts use only the Python standard library (the PDF needs Chromium on the PATH). From this folder, with a results directory `RESULTS`:

```bash
python3 scripts/analyze_run.py RESULTS --per-run per_run.csv       # tables and failure causes
python3 scripts/startup_effect.py RESULTS                          # per-minute start-of-sweep timing
python3 scripts/make_poster_page.py RESULTS poster.html --pdf poster.pdf \
    --timing-repeats REPEAT1 REPEAT2 ...    # optional: planner times from repeated runs
```

`make_poster_page.py` reads the setup line (images, budget, scenarios per cell) from the results directory's `experiment.json`, so a different run produces a matching page. With `--timing-repeats`, the planner's time for the train counts in those directories is the median, over the scenarios every repetition solved, of each scenario's median time across the repetitions. The poster here was made with `run-300s` and the five timing repeats. The summary files in `run-300s/` and `run-600s/` were generated this way from the raw directories.

To rerun the experiment itself, use `experiment.json` with `robust-rail-general` (`run_experiment.py`, or the SLURM array path for a cluster) after making sure the images are the versions above.
