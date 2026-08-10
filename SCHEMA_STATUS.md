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

**`plan_visualizer` reads the current plan and scenario shapes.** Task types,
`memberIDs`, and the `{kind, id}` resource shape; and, since 2026-08-10, flat
members in `initial_train_positions` and lengths resolved through
`trainUnitTypes` in `member_lengths_from_scenario`. Both of the latter crashed on
any scenario with standing trains. All 70 scenario/plan pairs under
`Location_KleineBinckhorst` — including the classified corpus under `fixtures/` —
now render. `member_lengths_from_plan` still returns nothing, by design: lengths
are no longer embedded in a plan, so the scenario is the only source.

## Not done

**The converter stops emitting actions partway through a plan.** On the test
fixture the planner returns eight steps ending in a departure, and
`convert_to_tors` emits three, ending on the service task — no `Exit`. Covered by
`test_main_plan_ends_with_an_exit`, deliberately left as a strict `xfail` rather
than weakened. **Pre-existing**: `new_pipeline_version`'s converter produces the
same three actions on its own fixture, so this is not schema drift. It does mean
no plan this repo currently produces is complete enough for the evaluator to
accept as a full solution.

**Facility time windows are not modelled.** The evaluator rejects the
SimpleService plan with "Facility 22 is not available from 1500 to 2000". No
converter reads `timeWindow`, so the PDDL model cannot respect facility
availability. The location declares no `timeWindow` on that facility at all, so
the evaluator is applying a default from somewhere — worth establishing where
before modelling it. Together with the truncation above, this is why the
pipeline runs end to end and still produces no valid solution: the format
contract holds, the planning model is incomplete.

**`convert_to_pddl/archive/`** was left alone. Those are superseded converters;
several read `request["displayName"]` and still carry the host-path default, and
would need the same treatment if they are still wanted.

**`create_park_action` is unreachable** and emits a `Park` task type that is not
in the schema's enum, so a plan containing one would be rejected. Nothing calls
it and no converted plan contains a Park; documented in place rather than
deleted, since that is a planning decision.

## How to check

```bash
pytest -q                       # 18 passed, 1 xfailed (needs julia for 10 of them)
```

Or the container path directly, which is what CI cannot run:

```bash
python main.py \
  --location tests/fixtures/simple_service/location.json \
  --scenario tests/fixtures/simple_service/scenarios/scenario_simple.json \
  --planner symbolic --output /tmp/plan.json
```
