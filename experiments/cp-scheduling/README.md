# CP scheduling post-processing: does it help?

This branch (`cp-scheduling-experiment`) adds Mayte's CP scheduler from `CP-scheduling-model` (commit `4c4ce1a`, cherry-picked from `59aff44`) on top of `fix-whole-departure-and-arrival-options`, and records an experiment that checks whether it gets more plans accepted by TORS. **Short answer: no.** As committed, the scheduler never re-times a plan; with that fixed, it still cannot rescue any of the rejected plans, because those plans are too slow in the order the planner chose.

## What the scheduler does

`convert_plan_to_tors/schedule.py` (`assign_times`) runs at the end of `convert_plan`. It keeps every TORS action the converter produced and only changes their times, with a CP-SAT model: each train's actions in their converted order, one train at a time on a track while driving, standing only where parking is allowed, and arrivals and on-time departures pinned to the scenario's instants. It minimises the makespan. If the model is infeasible, the converter's own times are kept. The approach follows [LonyuNaz/tusp-pddl-post-processing](https://github.com/LonyuNaz/tusp-pddl-post-processing). Commit `501ffa0` prints the outcome (`CP schedule: {...}`) to stderr, so it ends up in each attempt's `planner.err`.

## Result

The same 75 Kleine Binckhorst scenarios (1-5 trains, FIFO/LIFO/random, 5 each) were run with and without the scheduler. Both runs accept **60 of 75**, the same scenarios:

| Trains | Accepted |
|---|---|
| 1 | 15/15 |
| 2 | 15/15 |
| 3 | 12/15 |
| 4 | 12/15 |
| 5 | 6/15 |

The 15 failures are 10 late departures and 5 planner timeouts (600 s). The timeouts never reach the scheduler.

## Problem 1: the scheduler is always infeasible (`GAP = 1`)

In the experiment, `assign_times` returned `INFEASIBLE` for **all 70** converted plans, including the 60 that TORS accepts. So it never changed a single time.

The cause is `GAP = 1` in `schedule.py`: it requires one second between consecutive actions of a train, because the converter it was written against left that second (`su_clock = end + 1`). The converter on `main` no longer does (commit `f2219eb`, "removed the 1 second gaps"), so a converted plan has no slack for it. With arrivals and departures pinned, a tight plan comes up one second short per step. Example, `custom_1_FIFO_0`: arrive at T0, move 180 s, wait 360 s, move 180 s, exit at T720. That is exactly 720 s, but the model needs 724 s.

With `GAP = 0` the model works: 54 of the 70 plans get a schedule. Leaving out the converter's fixed-length `Wait` actions (waiting is already modelled by the scheduler's own gap intervals) brings that to 60.

## Problem 2: the rejected plans cannot be rescued by re-timing

The scheduler only pins a departure that is already on time. A late Exit is left free, so minimising the makespan moves it as early as possible, which TORS rejects as well (it needs the exact departure time). To rescue a late departure, every Exit has to be pinned to the departure time of its own request.

With that (and `GAP = 0`, flexible waits), all **10** rejected plans are `INFEASIBLE`: in the order the planner chose, with the real move durations, those trains cannot reach their departures on time, however the times are arranged. The remaining failures are therefore a planning problem, not a scheduling one. The planner has no notion of time, so it can pick an order that is too slow. Fixing that needs time in the PDDL model or a deadline-aware heuristic, not better post-processing.

Per-plan details are in `diagnose_output.txt`. Summary (TORS verdict, then solver status for GAP=1 / GAP=0 / GAP=0 without Waits / pinned Exits):

| Plans | TORS | GAP=1 | GAP=0 | GAP=0, no Waits | Exits pinned |
|---|---|---|---|---|---|
| 51 | accepted | INFEASIBLE | OPTIMAL | OPTIMAL | - |
| 6 | accepted | INFEASIBLE | INFEASIBLE | INFEASIBLE | - |
| 3 | accepted | INFEASIBLE | INFEASIBLE | OPTIMAL | - |
| 4 | rejected | INFEASIBLE | INFEASIBLE | INFEASIBLE | INFEASIBLE |
| 3 | rejected | INFEASIBLE | OPTIMAL | OPTIMAL | INFEASIBLE |
| 3 | rejected | INFEASIBLE | INFEASIBLE | OPTIMAL | INFEASIBLE |

## Reproducing

Versions used:

| Part | Version |
|---|---|
| Planner | this branch, image built with `--build-arg VERSION=cp-501ffa0` |
| Scenario generator | `robust-rail-generator` `main` (exact unit lengths, no rounding) |
| Evaluator | TORS `2.2.0-edge+20261012.c658d69` |
| Experiment runner | `robust-rail-general` branch `experimentation-updates` (`3832111`) |

`experiment.json` uses `"local"` for all three images, so the images must carry these local tags: `planner:latest`, `generator:latest` and `tors:latest`. A local tag is never pulled, so the run cannot silently switch versions.

1. Build the images. From this branch in `robust-rail-planner`:

   ```bash
   docker build --build-arg VERSION=cp-501ffa0 -t planner:latest .
   ```

   From `main` in `robust-rail-generator`:

   ```bash
   docker build -t generator:latest .
   ```

   Pull TORS edge and tag it locally:

   ```bash
   docker pull ghcr.io/robust-rail-nl/tors:edge && docker tag ghcr.io/robust-rail-nl/tors:edge tors:latest
   ```

   Check that the evaluator is the version above with `docker image inspect tors:latest --format '{{index .Config.Labels "org.opencontainers.image.version"}}'`. `tors:edge` is a floating tag, so a later pull may give a newer build.

2. Run the experiment from `robust-rail-general` (branch `experimentation-updates`):

   ```bash
   uv run python run_experiment.py <path to this folder>/experiment.json
   ```

   Results land in `robust-rail-general/results/q1_planner_cp_genmain_kleinebinckhorst_1to5/`. Each scenario's TORS evaluation is `custom_<trains>_<order>_<n>/planning/eval.txt`, and the scheduler's outcome is the `CP schedule:` line in `planning/planner.err`. It takes about an hour with `"jobs": 4`.

3. Run the diagnosis on those results inside the planner image (it needs ortools and the code under `/app`). From `Robust-Rail-NL`:

   ```bash
   docker run --rm --entrypoint python3 --mount type=bind,source=$PWD/robust-rail-general/Location_KleineBinckhorst,target=/app/database,readonly --mount type=bind,source=$PWD/robust-rail-general/results/q1_planner_cp_genmain_kleinebinckhorst_1to5,target=/app/results,readonly --mount type=bind,source=$PWD/robust-rail-planner/experiments/cp-scheduling,target=/app/io,readonly planner:latest /app/io/diagnose_cp.py
   ```

   It only re-solves the CP model on the stored plans; it does not run the planner again.

On Windows, a timed-out planner container kept running after its 600 s budget. That is a bug in `robust-rail-general/scripts/docker_utils.py` (`os.killpg` does not exist on Windows, and the resulting error skipped the `docker kill`), not related to this experiment. Stop leftovers with `docker ps` and `docker rm -f <name>`, or apply the fix proposed to `experimentation-updates`.
