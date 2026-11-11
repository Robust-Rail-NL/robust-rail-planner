# Release notes

## 0.6.0 — 2026-11-11

Everything merged since `v0.5.0`. Most of it is the PDDL and TORS converters: scenario classes that the planner reported as unsolvable or that the evaluator rejected are now planned and accepted, and the planner prints per-stage timing.

### Output: per-stage timing and search statistics (#60)

`main.py` now prints, on stdout and flushed, so that a run killed at its time limit keeps the stages that finished:

```
TIMING stage=<pddl_conversion|search|planner_process|enhsp_process|validation|json_conversion|total> seconds=<float>
STATS expanded=<int> plan_length=<int>
```

The symbolic planner script times the planner call and reports SymbolicPlanners' expanded-node count; the ENHSP script echoes ENHSP's output while parsing its planning time, expanded nodes and plan length. `search` is each planner's own search time and is not comparable between ENHSP (excludes JVM start-up, parsing, grounding) and symbolic-rail (excludes Julia start-up); `planner_process` covers the whole planner subprocess for both and is the comparable one. Planner behaviour is otherwise unchanged.

### Converter fixes

- **One `Service` action per unit and task type (#59).** The evaluator services every member of a shunting unit that needs a task type in a single visit; the converter emitted one `Service` per member, so the second action found nothing to do and the evaluator rejected the plan ("Could not find task <type> for any train in ShuntingUnit-N"). It now emits a single action per (unit, task type) that lasts as long as the members' tasks together. Found on generated scenarios where every unit of an arriving composition has the same servicing task.
- **Trains that need servicing can split before departing whole; a split-off unit can depart on its own (#55).** A train that arrives and departs in one composition was marked `compiled_must_stay_coupled`, which made scenarios with servicing unsolvable when the whole train does not fit on the cleaning track. A composition with a service task is no longer marked that way (adopting the train whole stays allowed), and `compiled_uncouple_front/back` clear `compiled_whole_target`. A departure of a single unit that is served by a unit split off a longer arrival used to be missing from the event chain, so the planner proved the problem unsolvable; it now falls back to the unit's single-unit SU.
- **No second `StandOut` for units parked out in a slot (#54).** The pass that exits parked-out units at the scenario end compared an integer id against string names and never matched, so every such unit got a second `StandOut` and the evaluator rejected the plan ("No outStanding train request matches trains N").
- **Whole-departure and arrival options (#53).** Keeps a train whole when it departs in reversed unit order, and adjusts the arrival options (two changes to `convert_to_pddl/convert.py`; see the PR for the second). On a small Kleine Binckhorst experiment (8 scenarios), accepted plans went from 1 to 4 of the 6 feasible ones.
- **Facing direction and a wider corridor (#50).** Tracks which side a shunting unit entered its track from (`came_from_a_su`), and widens the corridor heuristic from 3 to 4 hops, so fewer plans are rejected with "shunting units not facing the same direction" and scenarios that failed at 3 hops (`feasible_small` s16, s17) are solved.
- **Service chaining and smaller fixes (#41, closes #37 and #39).** A `Service` action could run at the same time as a `Combine` or `Split` that moves the same wagon, because the service branch of the post-processing was never reached; fixed, together with five smaller findings in `convert_to_tors.py` and the compiled-matching converter.
- **Standing trains (#42).** `_ensure_position` called a function that had been renamed, so any scenario with a train in `inStanding` crashed with a `NameError` on the first action that referenced it.
- **Smaller:** removed the 1 second gaps between converted actions (#43); an empty output path for `convert_to_pddl` now writes to the planner's `data` directory.

### Housekeeping

- Older PDDL converter models are archived, with one main version kept and the tests updated (#52).
- The poster experiment scripts are shipped in the Docker image, with documentation fixes; the pre-commit and pre-push hooks and the `docker-push*.sh` scripts are tracked, and a push script logs out of ghcr.io when it exits.

Plans can change (this is the point of the fixes): scenarios that had no plan, or a plan the evaluator rejected, now get a different plan.

### Compatibility

No change to the command line. Output files are unchanged; stdout gains the `TIMING`/`STATS` lines above. Use it with an evaluator that has the same `schemaVersion` as the plans (2).

### Reproducibility note

The solver-versus-planner experiment grid (robust-rail-general, `experiments/issa-grid/`) ran the build `robust-rail-planner 0.5.0-edge+20261106.2f2f34b`, which has the same source tree as this release, for both ENHSP and symbolic-rail.

## 0.5.0 — 2026-09-17

*(v0.4.0 was bumped in the `VERSION` file back in August but never actually tagged/released — this release covers everything merged since that point, not just the most recent work.)*

### Scheduling engine and PDDL model overhaul

The bulk of this release: `convert_to_tors_improvements` (#32) and `fix-remaining-model-bugs-clean` (#33), merged together. Highlights:

- Reworked the plan-to-TORS scheduling engine around a simpler per-SU `runs`/incremental-clock model.
- Compiled matching is now direction-aware: when a train's arrival and departure sides differ, unit order is reversed accordingly before assigning departure slots, and existing valid compositions are correctly reused instead of being needlessly uncoupled and recoupled.
- Composition changes now require the shunting unit to be stationary; back-uncoupling is restricted to compositions of 3+ units.
- Service requirements are tracked per train unit and task instead of on the whole composition, so units keep their service requirements through coupling/uncoupling.
- Yard-entry track selection now accounts for total incoming train length, falling back to a parkable track that actually fits when the configured one doesn't.

**Net result:** the `feasible_small` fixture set (20 KleineBinckhorst scenarios) now passes end-to-end (ENHSP solve → convert → TORS evaluate) on 18 of 18 solvable scenarios, up from 10/18 before either PR.

### Also since v0.4.0

- Unified the interchange schema/format shared with the generator, solver, and evaluator, including a substantial test-suite reorganization onto a proper `tests/fixtures/` structure (#26).
- Small early fix to the plan-to-TORS converter (#25), superseded in substance by the scheduling rewrite above.
- Added a `stable`/`edge` Docker image-channel convention matching the other three repos, including `docker-push-edge.sh` and version-string reporting at startup (#30).

### Known limitations, tracked separately

A handful of correctness gaps came out of a final review before this merge — none currently reproduce on the `feasible_small` fixture set above, but they're real code-level gaps worth tracking:

- #35 — no replacement for the old cross-SU same-track collision ledger (the only thing currently preventing double-booking is an incidental, `Move`-specific side effect of the scheduling pass, not a real check).
- #36 — `remove_loops()` was removed with no replacement, so a backtracking move sequence can re-embed a reversal instead of collapsing it (currently just a deprecation warning from the evaluator, not yet a hard failure).
- #37 — a dead-code branch means `Service` actions skip their wagon-conflict rescheduling check.
- #38 — departure admission scheduling lost independence between unrelated trains (everything is now forced into one strict global time order, even trains with no real dependency on each other).
- #39 — a small batch of lower-priority items (a fragile identity-vs-structural-equality lookup, a memberIDs-ordering overwrite, a missing parkable-track guard, and two pure code-quality items).

Versions before 0.5.0 (0.3.0, 0.4.0) have git tags but no release notes; see the git history.
