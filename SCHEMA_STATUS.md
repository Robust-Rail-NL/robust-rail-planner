# Schema status of this repo

Where `planning-approach` stands against the 2.0.0 interchange schema, as of
2026-08-08. The schema lives in `robust-rail-generator/schema/`; the roadmap is
`scenario-planning-inputs/docs/roadmap-2.0.0.md`.

## Done

**`converter.py` writes valid plans.** Checked by building one action of every
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

**`plan_visualizer` reads them.** Task types, `memberIDs`, and the `{kind, id}`
resource shape.

## Not done

**`pipeline.py` still assumes the pre-unification two-file world.** It reads
`location_solver.json` (Phase 1 renamed it to `location.json`, so this path does
not exist), and it pairs `scenario_solver_*.json` with `scenario_*.json` via
`.replace("scenario_solver_", "scenario_")` in several places. Unification
collapsed those into one file per scenario, so this is a design change rather
than a rename: someone has to decide what the pipeline's inputs are now. Note
`GENERATE_DIR` points into `../scenario-planning-inputs/Location_KleineBinckhorst`,
so it breaks against the current contents of that repo.

**`member_lengths_from_scenario` in the visualizer reads the old scenario
shape** — `scenario["in"]["trains"]`, `member["trainUnit"]`, `type.length`.
Pre-unification, and broken independently of anything done today. Its companion
`member_lengths_from_plan` now returns nothing by design, because lengths are no
longer embedded in a plan; the scenario is the right source, once that function
is updated.

**`test_data/` is stale and cannot simply be re-converted.** Its scenarios are
pre-unification (`in: {"trains": [...]}`) and its `.plan` files were produced
against them, with train IDs that no longer exist. Regenerating means re-running
the planner, not re-running the converter.

**`src/convert/` variants and `src/convert/archive/`** were left alone. They are
alternative and superseded converters; several read `request["displayName"]` and
would need the same treatment if they are still wanted.

**`create_park_action` is unreachable** and emits a `Park` task type that is not
in the schema's enum, so a plan containing one would be rejected. Nothing calls
it and no converted plan contains a Park; documented in place rather than
deleted, since that is a planning decision.

## How to check

The emission helpers are pure functions, so a plan can be assembled from them
and validated without running a planner:

```python
from robust_rail_planning import converter as C
# build actions with C.create_*_action(...), wrap as
# {"schemaVersion": C.SCHEMA_VERSION, "actions": [...]}, then validate against
# robust-rail-generator/schema/schema_plan.json
```
