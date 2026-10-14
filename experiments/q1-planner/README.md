# Q1: how many generated scenarios does the planner solve?

This experiment answers Q1 of the BNAIC 2026 demo poster: for how many generated Kleine Binckhorst scenarios does the planner produce a plan that the TORS evaluator accepts? It uses the planner on this branch (`fix-whole-departure-and-arrival-options`, commit `95f9c2c`) without the CP scheduling post-processing. That variant is documented separately on branch `cp-scheduling-experiment`, and gives the same result.

## Setup

- **Location:** Kleine Binckhorst.
- **Scenarios:** 1 to 5 trains; FIFO, LIFO and random departure order; 5 scenarios each, so 75 in total. Generator seed 42.
- **Trains:** 1 to 3 units each, drawn from VIRM-4, VIRM-6, ICM-3, ICM-4, SNG-3, SNG-4, SLT-4 and SLT-6. 30% in-standing trains, 10% out-standing, no servicing.
- **Planner:** `symbolic-rail` (SymbolicPlanners.jl with the h_rail heuristic), 600 s per scenario, 4 scenarios in parallel.
- **Evaluation:** a scenario counts as solved only if TORS accepts the plan. The planner's own success is not enough.

The full configuration is `experiment.json`.

## Result

**60 of the 75 scenarios (80%) are accepted by TORS.**

| Trains | FIFO | LIFO | random | Total |
|---|---|---|---|---|
| 1 | 5/5 | 5/5 | 5/5 | 15/15 |
| 2 | 5/5 | 5/5 | 5/5 | 15/15 |
| 3 | 4/5 | 5/5 | 3/5 | 12/15 |
| 4 | 5/5 | 4/5 | 3/5 | 12/15 |
| 5 | 2/5 | 2/5 | 2/5 | 6/15 |
| **Total** | | | | **60/75** |

Every 4- and 5-train scenario has an in-standing train. Without in-standing trains (the 45 scenarios with 1-3 trains), 42 of 45 are accepted.

HIP was not run on these scenarios, so it is not known whether all 75 are solvable at all. None fails on train length, which was a generator problem in earlier runs (see "How the result came about").

## The 15 failures

| Scenario | In-standing train | Failure |
|---|---|---|
| `custom_3_FIFO_0` | no | late departure: leaves 2460, due 2400 (+60 s) |
| `custom_3_random_2` | no | no plan within 600 s |
| `custom_3_random_3` | no | no plan within 600 s |
| `custom_4_LIFO_2` | yes | late departure: leaves 3244, due 2880 (+364 s) |
| `custom_4_random_1` | yes | no plan within 600 s |
| `custom_4_random_3` | yes | late departure: leaves 3410, due 3360 (+50 s) |
| `custom_5_FIFO_0` | yes | late departure: leaves 4234, due 3840 (+394 s) |
| `custom_5_FIFO_3` | yes | late departure: leaves 4050, due 3840 (+210 s) |
| `custom_5_FIFO_4` | yes | late departure: leaves 3930, due 3840 (+90 s) |
| `custom_5_LIFO_0` | yes | late departure: leaves 4264, due 3840 (+424 s) |
| `custom_5_LIFO_3` | yes | late departure: leaves 3900, due 3840 (+60 s) |
| `custom_5_LIFO_4` | yes | late departure: leaves 3870, due 3840 (+30 s) |
| `custom_5_random_2` | yes | no plan within 600 s |
| `custom_5_random_3` | yes | no plan within 600 s |
| `custom_5_random_4` | yes | late departure: leaves 4140, due 3840 (+300 s) |

**Late departures (10).** The PDDL model has no notion of time: it gets the order of actions right but not how long they take, so it can choose an order in which a train cannot reach its departure in time. Re-timing the actions does not help; the CP scheduling experiment shows that none of these 10 plans can meet its departure times in the order the planner chose. Fixing this needs time in the model or a deadline-aware heuristic.

**No plan within 600 s (5).** All five have random departure order, which is the hardest for the search.

**Misleading TORS message for `custom_5_LIFO_3`.** TORS reports this one as train 7 leaving 1860 s *early*. In fact the train serves request 8 and leaves 60 s late. Requests 7 and 8 have the same composition, and when no request matches an Exit's exact time, TORS names the first request with that composition in its error (`FormatExitMismatchError` in `cTORS/src/engine/Plan.cpp`). The converter is not at fault.

## How the result came about

This branch adds four commits to `main` (`51c2655`). Each step below was a separate run of 75 scenarios with the same settings. The first two runs used the generator branch `round-unit-lengths-to-metres`, so their scenarios differ from the last two; compare totals, not single scenarios.

| Planner | Generator | Accepted |
|---|---|---|
| `main` (`51c2655`) | `round-unit-lengths-to-metres` | 14/75 |
| + `7119a90`, `a4595ed` | `round-unit-lengths-to-metres` | 35/75 |
| + `7119a90`, `a4595ed` | `main` | 42/75 |
| + `9755aae`, `95f9c2c` (this branch) | `main` | **60/75** |

What each commit fixes:

- **`7119a90` Keep a train whole when it departs in reversed unit order.** A train that enters and leaves through the same gateway leaves with its units in reverse order. TORS accepts a departing train in either direction (`ShuntingUnit::MatchesShuntingUnit`), but the converter only allowed the exact order, so it planned a pointless split and recombine of about 300 s. This caused late departures even with a single train.
- **`a4595ed` Let the planner choose among fitting arrival tracks.** Every arrival was forced onto the nearest parkable track, almost always the dead-end 906b, from which every later move needs a reversal (about 440 s). The planner may now choose among the fitting tracks.
- **`9755aae` Only offer arrival tracks reached through switches alone.** Corrects the previous commit: `arrive_su` does not check the route to the arrival track, so a train could "arrive" on 104a across a train standing on 52.
- **`95f9c2c` Give in-standing trains their own shunting-unit ids.** The converter derived ids from PDDL names by stripping the prefix, so `train_in_standing_0` and incoming `train0` both became 0 and were merged. TORS could not execute those plans ("made no progress ... stuck at T0"). This made every scenario with an in-standing train fail.

The generator change: the branch `round-unit-lengths-to-metres` rounds unit lengths to whole metres, which makes 3 x VIRM-4 327 m instead of 325.68 m, longer than the 326 m arrival track 906a. TORS rejects such a scenario when loading it, before looking at any plan, so it could not be solved by any tool. The generator on `main` uses exact lengths.

## Files

- `experiment.json`: the configuration that was run.
- `runs.csv`: one row per scenario, with plan length, run time and lateness.
- `planner_failures.csv`: the 15 failures and their reason as recorded by the runner.

The scenarios, plans and TORS evaluations themselves are not stored here; reproducing the run regenerates them.

## Reproducing

Versions used:

| Part | Version |
|---|---|
| Planner | this branch, commit `95f9c2c` |
| Scenario generator | `robust-rail-generator` `main` |
| Evaluator | TORS `2.2.0-edge+20261012.c658d69` |
| Experiment runner | `robust-rail-general` branch `experimentation-updates` (`3832111`) |

`experiment.json` uses `"local"` for all three images, so they must carry these local tags: `planner:latest`, `generator:latest` and `tors:latest`. A local tag is never pulled, so the run cannot silently switch versions.

1. Build the images. From this branch in `robust-rail-planner`:

   ```bash
   docker build --build-arg VERSION=fix-95f9c2c -t planner:latest .
   ```

   From `main` in `robust-rail-generator`:

   ```bash
   docker build -t generator:latest .
   ```

   Pull TORS edge and tag it locally:

   ```bash
   docker pull ghcr.io/robust-rail-nl/tors:edge && docker tag ghcr.io/robust-rail-nl/tors:edge tors:latest
   ```

   Check the evaluator version with `docker image inspect tors:latest --format '{{index .Config.Labels "org.opencontainers.image.version"}}'`. `tors:edge` is a floating tag, so a later pull may give a newer build than the one used here.

2. Run the experiment from `robust-rail-general` (branch `experimentation-updates`):

   ```bash
   uv run python run_experiment.py <path to this folder>/experiment.json
   ```

   Results land in `robust-rail-general/results/q1_planner_fix2_genmain_kleinebinckhorst_1to5/`. Each scenario's TORS evaluation is `custom_<trains>_<order>_<n>/planning/eval.txt`, with the short verdict in `eval_result.json`. The run takes about an hour with `"jobs": 4`.

3. The summary table is printed at the end of the run and written to `coverage_analysis.txt` in the results folder.

On Windows, a planner container that hits the 600 s limit keeps running afterwards. That is a bug in `robust-rail-general/scripts/docker_utils.py`: `os.killpg` does not exist on Windows, and the resulting error skips the `docker kill`. It does not change the results, but leftover containers slow down the rest of the run. Stop them with `docker ps` and `docker rm -f <name>`, or apply the fix proposed to `experimentation-updates`.
