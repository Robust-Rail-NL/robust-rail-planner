# Schema status of this repo

Where `planning-approach` stands against the 2.0.0 interchange schema, as of
2026-08-10. The schema lives in `robust-rail-generator/schema/`; the roadmap is
`scenario-planning-inputs/docs/roadmap-2.0.0.md`.

## Layout

On 2026-08-10 `new_pipeline_version` was merged into `release/2.0.0`, replacing
the `src/` package layout with a Docker-first one. The two branches had diverged
from a 2026-08-05 base and neither contained the other's work: the restructure
carried the container, and `release/2.0.0` carried the unified-schema migration.
The merge takes the layout from the former and the Python from the latter.

| now | was |
|---|---|
| `convert_to_pddl/<variant>/convert.py` | `src/convert/<variant>/convert.py` |
| `convert_plan_to_tors/convert_to_tors.py` | `src/robust_rail_planning/converter.py` |
| `plan/{symbolic,enhsp}_planner.jl` | `src/plan/planner.jl` |
| `main.py` (container entrypoint) | `run.py`, `src/robust_rail_planning/cli.py` |
| `requirements.txt` | `pyproject.toml`, `env.yml`, `setup.{sh,ps1,bat}` |

**Deleted, recoverable from git** (`git show 7c0346e:<path>`): `pipeline.py`,
`run.py`, `cli.py`, `evaluate.py`, `generate.py`, `src/local_search/solve.py`,
`src/plan/audit_discrete_plan.py`. The batch-driver role they played now belongs
to `scenario-planning-inputs`' `run_planner.py` / `run_evaluator.py`. Local
search and the discrete-plan auditor have **no** replacement — they were dropped
as part of the restructure, not superseded.

## Done

**The plan emitter writes valid plans.** Checked by building one action of every
kind from the emission helpers and validating against `schema_plan.json`: it was
269 errors, and is now 0. What changed:

| was | now | from |
|---|---|---|
| `resources: [{name, trackPartId}]` | `[{kind, id}]` | Phase 0d |
| `"startTime": "600"` | `600` | Phase 1 |
| `shuntingUnit.members` with embedded units | `memberIDs`, a list of IDs | Phase 1 |
| no `schemaVersion` | `schemaVersion: 1` | Phase 1a |
| `standingType: "InStanding"` on an Arrive | task type `StandIn` / `StandOut` | Phase 3e |
| `trainUnitIds`, top-level `trackParts` | removed | Phase 3e |
| string IDs, `displayName` | numeric IDs, `id` | Phase 3d |

**The whole container path is now covered end to end.** `main.py` runs
scenario → PDDL → plan → TORS JSON on the test fixture and the result validates
against `schema_plan.json` with zero errors.

**Four defects the earlier migration missed** were found by running that path,
all of them consequences of ids becoming integers, and all of them silent:

- `"train" + train["id"]` and three sibling sites — `TypeError`, the loud one.
- `request["displayName"]` — renamed to `id`; would have been a `KeyError`.
- `facility["relatedTrackParts"]` — renamed to `relatedTrackPartIDs`. Read
  through `.get(..., [])`, so it returned no service tracks and the instance
  quietly became unsolvable. It surfaced as the planner reporting "failed to
  find a solution", which reads like a hard problem rather than a broken one.
- `compiled_route_edge` — the movement graph is keyed by `str(id)` and
  `id_to_track_part` by the native id, so **every** route edge was dropped and no
  track was reachable from any other. Same misleading symptom.

The last two are why the fixture is worth keeping in-repo: `test_plan_schema.py`
exercises the emission helpers as pure functions and passed throughout, because
nothing there runs a conversion.

**`tests/fixtures/simple_service/` is migrated**, and both files were validated
against `schema_location.json` / `schema_scenario.json` before being committed.

**Plans are complete, and an unknown action is now loud.** The corridor model
emits `compiled_depart_(aside|bside)_for_request` — four arguments, no slot,
because `compile_precomputed_actions` bakes the matching in — and no pattern
matched it. The departure was dropped, and the trailing moves went with it,
since their pending path is only flushed on a recognised terminating action.
Conversion reported success and produced a plan that merely stopped early.

Fixed by adding the pattern, and by making `convert_plan` raise on any plan line
it cannot match instead of moving to the next one. The silence was the actual
defect: the missing pattern was one instance of it, and the next model change to
rename an action would have gone the same way.

Also corrected an off-by-one alongside it: the request name was read as
`groups[4]`, which is the *track* in the five-argument form, so that lookup
always missed and fell through to a fallback. Both forms end `(…, request,
track)`, so it now counts from the right.

And an `UnboundLocalError` that the same fix exposed: `expanded_path` was bound
only inside `if train in active_trains`, so a train departing without a
preceding move reached the `exit_track` line with the name unbound. Latent while
compiled departures matched nothing; `Location_KleineBinckhorst` produces such a
plan and the test fixture does not, so it took a real location to surface. It is
bound before the branch now.

**`plan_visualizer` reads the current plan and scenario shapes.** Task types,
`memberIDs`, and the `{kind, id}` resource shape; and, since 2026-08-10, flat
members in `initial_train_positions` and lengths resolved through
`trainUnitTypes` in `member_lengths_from_scenario`. Both of the latter crashed on
any scenario with standing trains. All 70 scenario/plan pairs under
`Location_KleineBinckhorst` — including the classified corpus under `fixtures/` —
now render. `member_lengths_from_plan` still returns nothing, by design: lengths
are no longer embedded in a plan, so the scenario is the only source.

## Not done

*(The truncated-plan defect that stood here is fixed — see Done above.)*

**Departure deadlines are not respected.** With complete plans, the evaluator
rejects on timing rather than structure. Measured on both locations:

| location | exit at | requested | late by |
|---|---|---|---|
| SimpleService | 2902 | 2000 | 902s |
| KleineBinckhorst | 6301 | 5400 | 901s |

Both give the same verdict: *"Trains's departure mismatch with Action start/end
time"*. The PDDL model sequences actions and costs movement, but nothing ties an
exit to the request's `departure`, so the planner has no reason to be punctual.
The test fixture shows it too — Exit at 1803 against a requested 1000 — so
`test_plan_meets_its_departure_deadlines` holds it as a strict `xfail` without
needing an evaluator run.

SimpleService also reports `Is Train matched : No` on one request, which may be
the same problem or a separate matching one — worth separating before modelling
either.

**A note on facility time windows, which used to be recorded here.** The
evaluator previously rejected this scenario with "Facility 22 is not available
from 1500 to 2000", and this file claimed that was independent of the truncation
above. It was not: it was downstream of it. Once the converter emitted whole
plans, both trains got scheduled, the service landed inside the window, and that
error stopped occurring altogether. No converter reads `timeWindow` and that is
still true — but there is currently no evidence it matters, and the earlier
entry mistook a symptom of the truncation for a second independent defect.

**`convert_to_pddl/archive/`** was left alone. Those are superseded converters;
several read `request["displayName"]` and still carry the host-path default, and
would need the same treatment if they are still wanted.

**`create_park_action` is unreachable** and emits a `Park` task type that is not
in the schema's enum, so a plan containing one would be rejected. Nothing calls
it and no converted plan contains a Park; documented in place rather than
deleted, since that is a planning decision.

## How to check

```bash
pytest -q                       # 18 passed, 1 xfailed — 11 of them skip without
                                # an instantiated Julia project in plan/
```

CI runs the same suite *inside* the image, where the Julia backend does exist,
so nothing skips there: 18 passed, 1 xfailed. The image is built from the commit
under test, never pulled — a published image is built from whenever someone last
ran docker-push.sh, so testing against it would report on the previous release.

The container path directly:

```bash
python main.py \
  --location tests/fixtures/simple_service/location.json \
  --scenario tests/fixtures/simple_service/scenarios/scenario_simple.json \
  --planner symbolic --output /tmp/plan.json
```
