# TUSP-SS PDDL Domain Model

## Version History
| Version | Date  | Summary of Changes |
|---------|------|---------------|--------------------|
| v0.1 | 2026-04-28 | Summary of changes |

---

## Subproblems
> Which subproblems are currently modelled

- [ ] Subproblem 1 — Parking
- [ ] Subproblem 2 — Routing
- [ ] Subproblem 3 — Service Scheduling
- [ ] Subproblem 4 — Matching / Arrivals / Departures
- [ ] Subproblem 5 — Combining & Splitting

---

## Types
| Type | Description | Introduced |
|------|-------------|------------|
| `trackpart` | A piece of track on the shunting yard | v0.1 |
| `trainunit` | An individual train unit (atomic) | v0.1 |
| `arrivaltrain` | An arriving train | v0.1 |

---

## Fluents
| Fluent | Signature | Type | Description | Introduced |
|--------|-----------|------|-------------|------------|
| `free` | `(trackpart)` | Bool | Whether a track is unoccupied. Default: true | v0.1 |
| `arrival` | `(arrivaltrain)` | Int | Arrival timestamp of a train | v0.1 |
| `at` | `(arrivaltrain, trackpart)` | Bool | Whether a train is at a given track. Default: false | v0.1 |

---

## Actions
### `move`
- **Parameters:** `t - arrivaltrain`, `l_from - trackpart`, `l_to - trackpart`
- **Preconditions:**
  - `at(t, l_from)`
- **Effects:**
  - `at(t, l_to) = true`
  - `at(t, l_from) = false`
- **Introduced:** v0.1
- **Notes:** 

---

## Initial State
> What gets populated from the JSON inputs

| Entity | Source | What is set |
|--------|--------|-------------|
| `trackpart` objects | `location.json → trackParts` | One object per track part, named by `name` field |
| `arrivaltrain` objects | `scenario.json → in.trains` | One object per arriving train, named `train{id}` |
| `trainunit` objects | `scenario.json → in.trains[].members` | One object per unit, named `unit{id}` — no state set |
| `arrival(train)` | `scenario.json → in.trains[].arrival` | Set to integer arrival timestamp |
| `at(train, track)` | `scenario.json → in.trains[].firstParkingTrackPart` | Set to true for initial parking position |

---

## Known Gaps / TODOs

### Gap 1 — `free` fluent is declared but never maintained
**What's missing:** `free(trackpart)` is initialised to `true` for every track part in the problem file, but no action reads or writes it. `move` neither checks `free(l_to)` as a precondition nor toggles `free` on source/destination as an effect.
**Impact:** The fluent is dead weight. More importantly, nothing currently prevents two trains from occupying the same track part simultaneously.
**Fix:** Either (a) remove `free` and replace with a proper capacity check (see Gap 2), or (b) add `free(l_to)` precondition to `move` and toggle effects `free(l_from) = true`, `free(l_to) = false`. Option (b) is only correct for single-train-per-track models.

---

### Gap 2 — No track capacity enforcement
**What's missing:** There is no `train_length`, `track_length`, or `aside_distance` fluent. The `move` action has no precondition that checks whether the destination track has enough remaining space.
**Impact:** Multiple trains can be moved to the same track until its physical capacity is exceeded, producing plans that are infeasible in the real yard. This is the most critical correctness gap.
**Fix:** Add numeric fluents `train_length(arrivaltrain)` and `track_length(trackpart)`. In `move`, add precondition `track_length(l_to) >= train_length(t)` and effects that decrease `track_length(l_to)` and restore `track_length(l_from)`. (Mirrors the `aside_distance` approach from the thesis baseline.)

---

### Gap 3 — `arrival` fluent is set but never used
**What's missing:** `arrival(train)` is initialised from the scenario JSON but no action has a precondition that reads it. Trains can be moved before they physically arrive at the yard.
**Impact:** Plans may be logically valid in PDDL but temporally infeasible: a train is moved before its arrival time.
**Fix:** Either (a) add a temporal precondition `>= arrival(t)` to `move` if switching to a temporal planner (e.g. TFD), or (b) remove the fluent and encode arrival order through the initial `at` state (trains not yet arrived simply have no `at` fact until their arrival event is triggered). For a classical instantaneous-action model, option (b) is simplest.

---

### Gap 4 — No service subproblem (Subproblem 3)
**What's missing:** No `service_allowed(trackpart)`, `serviced(arrivaltrain)`, or `needs_service(arrivaltrain)` fluents. No `service` action. The goal only requires `parked`, with no dependency on servicing first.
**Impact:** Trains that require cleaning, washing, or inspection are not routed to service tracks. The planner can park them directly without any service detour.
**Fix:** Add `service_allowed` and `serviced` fluents and a `service` action with precondition `at(t, l) ∧ service_allowed(l)` and effect `serviced(t) = true`. Add `serviced(t)` as a precondition of `park` (or add it to the goal).

---

### Gap 5 — No matching subproblem (Subproblem 4)
**What's missing:** No train-type predicates (`train_type_SLT`, `train_type_VIRM`, etc.) and no outbound train-request objects. There is no mechanism to ensure that a parked `arrivaltrain` is of the correct type to satisfy an outbound departure request.
**Impact:** The planner can assign any train to any parking slot regardless of type. In a real scenario, an SLT unit cannot substitute for a VIRM unit on a scheduled service.
**Fix:** Add one boolean fluent per train type (e.g. `is_type_SLT(arrivaltrain)`). Introduce outbound request objects with a required type, and link them to parked trains via a `matched` fluent and a `match` action (or type precondition in `park`).

---

### Gap 6 — `park` uses strict equality on departure rank
**What's missing:** The precondition `departure_rank(t) = entry_distance(l)` requires an exact match. A train with rank 1 cannot park at a track with `entry_distance = 2` even when no rank-2 trains exist (e.g. only one train in the scenario, or all other trains have already parked closer).
**Impact:** The planner may fail to find a solution in small or asymmetric scenarios where the "correct" rank level has no available track but a deeper level is free.
**Fix:** Relax to `departure_rank(t) <= entry_distance(l)` — a train may park *further* from the exit than its rank strictly requires, as long as no earlier-departing train is trapped behind it. This requires also checking that no lower-rank train is blocked (more complex; a simpler proxy is the `<=` relaxation).

---

### Gap 7 — Multi-unit train composition not modelled (Subproblem 5)
**What's missing:** `trainunit` objects are created in the problem file but have no fluents, no `at` or `free` facts, and no actions. Train coupling (combining two units into one consist) and splitting are absent.
**Impact:** Trains are treated as indivisible atoms. The planner cannot reason about coupling two SLT units into a longer consist, which is required for many NS departure schedules.
**Fix:** Add `unit_in_train(trainunit, arrivaltrain)` and `unit_at(trainunit, trackpart)` fluents. Add `couple` and `decouple` actions. This is a significant extension; treat as a separate domain variant.

---

### Gap 8 — No concurrent movement limit
**What's missing:** No `concurrent_movements` or `max_concurrent_movements` fluents. For a classical (instantaneous-action) planner, all moves are sequential by construction; but for temporal planners like TFD, multiple `move` actions may overlap, exceeding the number of available drivers or physical path capacity.
**Impact:** Plans generated by a temporal planner may be physically infeasible (too many trains moving at once on shared infrastructure).
**Fix:** Add integer fluents `concurrent_movements` (initialised to 0) and `max_concurrent_movements` (initialised from config). In a temporal `move`, increment on start and decrement on end; add precondition `concurrent_movements < max_concurrent_movements`. (This is the driver-free concurrency model described in the thesis.)

---

### Gap 9 — No plan-cost metric
**What's missing:** No `total-cost` numeric function and no `:metric minimize (total-cost)` goal qualifier. The planner optimises for nothing; all valid plans are equally acceptable.
**Impact:** Plans may be unnecessarily long. Runtime comparisons across domain variants are not meaningful without a shared cost metric.
**Fix:** Add `(total-cost)` function (initialised to 0). Increment it in each action's effect (e.g. +1 per `move`). Add `(:metric minimize (total-cost))` to the problem.

---

### Gap 10 — Entry and exit tracks not distinguished
**What's missing:** There is no `entry_track(trackpart)` or `exit_track(trackpart)` predicate. The model does not know which track parts are the yard's physical entry/exit gates.
**Impact:** Trains can be moved freely across the entire yard graph with no notion of where they enter or leave. Arrival and departure events cannot be modelled explicitly.
**Fix:** Add boolean fluents `entry_track` and `exit_track`. Populate from `entryTrackPart` / `leaveTrackPart` in the scenario JSON (already parsed in `convert.py` for BFS purposes but not exposed as PDDL facts).
