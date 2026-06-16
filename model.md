# TUSP-SS PDDL Domain Model

## Version History
| Version | Date | Summary of Changes |
|---------|------|--------------------|
| v0.1 | 2026-04-28 | Initial model |
| v0.2    | 2026-05-12 | Added `park` action, `parking_allowed` and `parked` fluents |
| v0.3    | 2026-05-12 | Parking subproblem: `connected` on `move`; `entry_distance` + `departure_rank` on `park` |
| v0.4    | 2026-05-19 | Matching/coupling variants: request slots, `match`, optional `uncouple`, and optional `couple_to_request` |
| v0.5 | 2026-05-18 | Added the routing subproblem: `depart` action, `departure_exit` fluent, capacity tracking with `astack_distance`, `bstack_distance`, `train_length`, `track_length`, and `track_is_parked_at` |
| v0.6    | 2026-05-25 | Explicit coupling now requires physical two-unit assembly: same track, valid coupling track, and correct order |
| v0.7    | 2026-05-31 | Merged routing direction model with shunting-unit split, coupling, movement, and departure |
| v0.8    | 2026-05-27 | Cost metric: `total_cost` (seconds), move cost = 300s, `(:metric minimize (total_cost))`. Arrival timing: `has_arrived`, `entry_track_of`, `arrive`, and `wait` actions; inbound trains deferred from init |
| v0.9    | 2026-05-27 | Coupling costs added: `uncouple` = 120s, `couple_two_units` / `couple_two_units_same_train` = 180s (from NS scenario `splitDuration`/`combineDuration`) |
| v0.10   | 2026-06-16 | Unified shunting-unit converter: arrival-train movement layer retired; dead `couple_two_units` / `couple_two_units_same_train` removed; `outStanding` requests are parking-only; per-scenario movement corridor; servicing subproblem (`service_su`); arrival precedence (`arrive_su`); SU-layer parking (`park_su`) and `must_depart_su`; fix: `depart_aside_su` / `depart_bside_su` increment `num_of_departed_trains`. Feature flags + per-combination converter folders. See "Unified Shunting-Unit Converter" below. |

---

## Subproblems
> Which subproblems are currently modelled

- [x] Subproblem 1 — Parking
- [x] Subproblem 2 — Routing
- [x] Subproblem 3 — Service Scheduling (v0.10, opt-in via `--servicing`)
- [x] Subproblem 4 — Matching / Arrivals / Departures (matching + arrival precedence)
- [x] Subproblem 5 — Combining & Splitting

---

## Branch Additions
- `convert.py` can emit `parking`, `matching`, or `combined` variants with `--subproblem`.
- Matching adds request slots and the `match` action; compatibility uses unit type, carriage count, and length.
- `--coupling-mode` can switch between free uncoupling, explicit uncoupling, and explicit coupling.
- Explicit uncoupling adds `uncouple`; explicit coupling adds `couple_to_request`.
- Physical explicit coupling uses `shuntingunit` objects for the current movable composition after split/couple actions.
- Routing branch converter variants are documented separately in `src/convert/md_files`.
- `run.py` exposes subproblem, coupling-mode, and planner-backend choices.

---

## Types
| Type | Description | Introduced |
|------|-------------|------------|
| `trackpart` | A piece of track on the shunting yard | v0.1 |
| `trainunit` | An individual train unit (atomic) | v0.1 |
| `arrivaltrain` | An arriving train | v0.1 |
| `departurerequest` | An outgoing train request used by matching | coupling/parking branch |
| `requestslot` | One required unit position inside an outgoing request | coupling/parking branch |
| `arrivalcomposition` | A multi-unit incoming composition that may need uncoupling | coupling/parking branch |
| `shuntingunit` | A movable physical train composition used after split/couple actions | v0.7 |

---

## Fluents
| Fluent | Signature | Type | Description | Introduced |
|--------|-----------|------|-------------|------------|
| `arrival` | `(arrivaltrain)` | Int | Scheduled arrival time in seconds. Used by `arrive` action. | v0.1 |
| `at` | `(arrivaltrain, trackpart)` | Bool | Whether a train is currently at a given track. Default: false | v0.1 |
| `parking_allowed` | `(trackpart)` | Bool | Whether a track permits parking. Set from `parkingAllowed` in location JSON. Default: false | v0.2 |
| `parked` | `(arrivaltrain)` | Bool | Whether a train has been successfully parked. Default: false | v0.2 |
| `departed` | `(arrivaltrain)` | Bool | Whether a train has left the yard. Default: false | v0.4 |
| `connected_aside` | `(trackpart, trackpart)` | Bool | Directed connection via a-side. Set from `aSide` in location JSON. Default: false | v0.3 |
| `connected_bside` | `(trackpart, trackpart)` | Bool | Directed connection via b-side. Set from `bSide` in location JSON. Default: false | v0.3 |
| `departure_exit_a` | `(trackpart)` | Bool | Track is a yard exit reachable via a-side. Default: false | v0.5 |
| `departure_exit_b` | `(trackpart)` | Bool | Track is a yard exit reachable via b-side. Default: false | v0.5 |
| `entry_distance` | `(trackpart)` | Int | Normalised BFS hop-distance from yard exit. Rank 1 = closest. Default: 0 (non-parking tracks). | v0.3 |
| `departure_rank` | `(arrivaltrain)` | Int | Rank of departure time among inbound trains (1 = first to depart). Ties share rank (lenient). | v0.3 |
| `train_length` | `(arrivaltrain)` | Real | Total physical length in metres, summed from unit types. Default: 0 | v0.5 |
| `track_length` | `(trackpart)` | Real | Maximum capacity in metres. Non-parking tracks use 10⁹ (effectively infinite). | v0.5 |
| `aside_distance` | `(arrivaltrain)` | Real | Distance of the train's a-side from the track's a-side origin. Default: 0 | v0.5 |
| `astack_distance` | `(trackpart)` | Real | Free space measured from the a-side; increases as trains leave from that end. Default: 0 | v0.5 |
| `bstack_distance` | `(trackpart)` | Real | Occupied space measured from the b-side; increases as trains arrive from outside. Default: 0 | v0.5 |
| `number_of_trains_on_track` | `(trackpart)` | Int | Count of trains currently on the track. Default: 0 | v0.5 |
| `track_is_parked_at` | `(trackpart)` | Bool | Whether at least one parked train occupies this track. Default: false | v0.5 |
| `num_of_departed_trains` | `()` | Int | Running count of trains that have departed. Default: 0 | v0.5 |
| `allowed_to_move` | `(arrivaltrain)` | Bool | Movement permission token; set by `start_move`, cleared by `end_move` / `depart`. Default: false | v0.5 |
| `concurrent_movements` | `()` | Int | Number of trains currently holding a movement token. Default: 0 | v0.5 |
| `total_cost` | `()` | Int | Accumulated plan time in seconds. Minimised as the plan metric. Default: 0 | v0.7 |
| `has_arrived` | `(arrivaltrain)` | Bool | Whether a train is physically present in the yard. True from init for standing trains; set by `arrive` for inbound trains. Default: false | v0.7 |
| `entry_track_of` | `(arrivaltrain, trackpart)` | Bool | Designates the entry track for an inbound train. Used by `arrive` to enforce correct placement. Default: false | v0.7 |
| `available` | `(trainunit)` | Bool | Whether a unit can be assigned to a request slot | v0.4 |
| `slot_open` | `(requestslot)` | Bool | Whether a request slot is unfilled | v0.4 |
| `slot_filled` | `(requestslot)` | Bool | Whether a request slot has been assigned a unit | v0.4 |
| `compatible` | `(trainunit, requestslot)` | Bool | Whether a unit matches a slot by type, carriage count, and length | v0.4 |
| `matched` | `(trainunit, requestslot)` | Bool | Records that a unit has been assigned to a slot | v0.4 |
| `request_open` | `(departurerequest)` | Bool | Whether a departure request is still unfulfilled | v0.4 |
| `slot_for_request` | `(requestslot, departurerequest)` | Bool | Links a slot to its parent request | v0.4 |
| `slot_before` | `(requestslot, requestslot)` | Bool | Orders the two slots of a two-unit outgoing request | v0.6 |
| `unit_in_train` | `(trainunit, arrivaltrain)` | Bool | Links a unit to the physical train it arrived in | v0.6 |
| `unit_before` | `(trainunit, trainunit)` | Bool | Preserves unit order inside an incoming multi-unit train | v0.6 |
| `coupling_allowed` | `(trackpart)` | Bool | Whether physical coupling may happen on a track. Baseline: same as `parking_allowed`. | v0.6 |
| `part_of_composition` | `(trainunit, arrivalcomposition)` | Bool | Used when explicit uncoupling is enabled | v0.4 |
| `composition_needs_uncoupling` | `(arrivalcomposition)` | Bool | Marks a multi-unit composition that must be split before matching | v0.4 |
| `slot_coupled` | `(requestslot)` | Bool | Used when explicit coupling is enabled | v0.6 |
| `physically_coupled` | `(trainunit, trainunit)` | Bool | Records that two units were physically assembled | v0.6 |
| `request_assembled` | `(departurerequest)` | Bool | Goal fluent for explicit two-unit coupling | v0.6 |

---

## Actions

### `wait`
- **Parameters:** none
- **Effects:** `total_cost := total_cost + 300`
- **Introduced:** v0.7
- **Notes:** Advances the clock by one 5-minute step without moving anything. Used by the planner only when it must idle waiting for an inbound train to arrive. The `total_cost` minimisation objective ensures it is never called unnecessarily.

### `arrive`
- **Parameters:** `t - arrivaltrain`, `l - trackpart`
- **Preconditions:**
  - `not has_arrived(t)`
  - `entry_track_of(t, l)`
  - `total_cost >= arrival(t)`
- **Effects:**
  - `has_arrived(t) = true`
  - `at(t, l) = true`
  - `aside_distance(t) = bstack_distance(l)` (train placed at the current b-side end)
  - `bstack_distance(l) += train_length(t)`
  - `number_of_trains_on_track(l) += 1`
- **Introduced:** v0.7
- **Notes:** Inbound trains (`in.trains` with `arrival > 0`) are absent from the initial state. The planner calls `arrive` once `total_cost` has reached the train's scheduled arrival time, using `wait` actions to advance the clock if no other work is available. Standing trains (`inStanding`) and trains with `arrival = 0` are pre-placed in the initial state with `has_arrived = true`.

### `start_move` / `end_move`
- **Parameters:** `t - arrivaltrain` (+ `l - trackpart` for `end_move`)
- **Purpose:** Bracket every movement sequence; enforce the `max_concurrent_movements = 1` limit.
- **`start_move` preconditions:** `not allowed_to_move(t)`, `concurrent_movements < 1`, `has_arrived(t)`
- **`start_move` effects:** `allowed_to_move(t) = true`, `concurrent_movements += 1`
- **`end_move` preconditions:** `allowed_to_move(t)`, `at(t, l)`, `parking_allowed(l)`
- **`end_move` effects:** `allowed_to_move(t) = false`, `concurrent_movements -= 1`
- **Introduced:** v0.5

### `move_aside_empty` / `move_aside_occupied`
- **Parameters:** `t - arrivaltrain`, `l_from - trackpart`, `l_to - trackpart`
- **Preconditions (both):** `allowed_to_move(t)`, `at(t, l_from)`, `connected_aside(l_from, l_to)`, `not parked(t)`, `aside_distance(t) <= astack_distance(l_from)`, `train_length(t) <= track_length(l_to)`
- **`_empty` additionally:** `number_of_trains_on_track(l_to) == 0`
- **`_occupied` additionally:** `number_of_trains_on_track(l_to) > 0`, `train_length(t) <= track_length(l_to) - bstack_distance(l_to)`
- **Effects:** update `at`, `number_of_trains_on_track`, `aside_distance`, `astack_distance`, `bstack_distance` on both tracks; `total_cost += 300`
- **Introduced:** v0.5

### `move_bside_empty` / `move_bside_occupied`
- Same structure as aside variants but use `connected_bside` and check the b-side stack.
- **Introduced:** v0.5

### `park`
- **Parameters:** `t - arrivaltrain`, `l - trackpart`
- **Preconditions:** `at(t, l)`, `parking_allowed(l)`
- **Effects:** `parked(t) = true`, `track_is_parked_at(l) = true`
- **Introduced:** v0.2
- **Notes:** Cost: 0. Parking is a goal-completing action and is not penalised.

### `depart_aside` / `depart_bside`
- **Parameters:** `t - arrivaltrain`, `l - trackpart`
- **Preconditions:** `allowed_to_move(t)`, `at(t, l)`, `departure_exit_a/b(l)`, position check on correct side
- **Effects:** `at(t, l) = false`, `departed(t) = true`, `num_of_departed_trains += 1`, position fluents updated, `concurrent_movements -= 1`, `allowed_to_move(t) = false`
- **Introduced:** v0.5
- **Notes:** Cost: 0.

### `match`
- **Parameters:** `unit - trainunit`, `slot - requestslot`
- **Preconditions:** `available(unit)`, `slot_open(slot)`, `compatible(unit, slot)`
- **Effects:** `matched(unit, slot) = true`, `slot_filled(slot) = true`, `available(unit) = false`, `slot_open(slot) = false`
- **Introduced:** v0.4

### `uncouple`
- **Parameters:** `unit - trainunit`, `composition - arrivalcomposition`
- **Description:** Releases a unit from a multi-unit incoming composition so it can be matched independently. Active when `--coupling-mode` is `implicit_explicit_uncoupling` or `explicit_coupling`.
- **Effects:** `available(unit) = true`, `part_of_composition(unit, composition) = false`, `total_cost += 120`
- **Introduced:** v0.4 / cost added v0.8

### `couple_two_units`
- **Parameters:** `unit_a/unit_b - trainunit`, `train_a/train_b - arrivaltrain`, `track - trackpart`, `slot_a/slot_b - requestslot`, `request - departurerequest`
- **Preconditions:** both units matched to ordered slots of the same request; each unit in its corresponding train; both trains on the same `coupling_allowed` track; `aside_distance(train_a) < aside_distance(train_b)`
- **Effects:** `slot_coupled`, `coupled_to_request`, `physically_coupled(unit_a, unit_b)`, `request_assembled(request)` all set true; `total_cost += 180`
- **Introduced:** v0.6 / cost added v0.8

### `couple_two_units_same_train`
- Same as `couple_two_units` but both units are in the same incoming train; uses `unit_before(unit_a, unit_b)` for order.
- **Effects:** same as `couple_two_units`; `total_cost += 180`
- **Introduced:** v0.6 / cost added v0.8

---

## Initial State
> What gets populated from the JSON inputs

| Entity | Source | What is set |
|--------|--------|-------------|
| `trackpart` objects | `location_solver.json → trackParts` | One object per track part, named by `name` field |
| `arrivaltrain` objects | `scenario_solver.json → in.trains` | One object per arriving train, named `train{id}` |
| `arrivaltrain` objects | `scenario_solver.json → inStanding.trains` | One object per standing train, named `train_in_standing_{index}` |
| `trainunit` objects | `scenario_solver.json → in.trains[].members` | One object per unit, named `unit{id}` |
| `arrival(train)` | `scenario_solver.json → in.trains[].arrival` | Set to integer arrival time in seconds |
| `train_length(train)` | `scenario_solver.json → in.trains[].members[].trainUnit.type.length` | Sum of all unit lengths |
| `at(train, track)` | `scenario_solver.json → in.trains[].entryTrackPart` | **Only set for `arrival = 0` trains.** Inbound trains with `arrival > 0` are absent from init. |
| `has_arrived(train)` | Set to `true` for standing trains and `arrival = 0` inbound trains. Absent (false) for inbound trains with `arrival > 0`. | v0.7 |
| `entry_track_of(train, track)` | `scenario_solver.json → in.trains[].entryTrackPart` | Set only for inbound trains with `arrival > 0`; designates which track the `arrive` action must place them on. | v0.7 |
| `connected_aside(a, b)` | `location_solver.json → trackParts[].aSide` | Set for each a-side neighbour pair |
| `connected_bside(a, b)` | `location_solver.json → trackParts[].bSide` | Set for each b-side neighbour pair |
| `departure_exit_a/b(track)` | `scenario_solver.json → out.trainRequests[].leaveTrackPart` | Set for tracks that are yard exits; a/b determined by which side has neighbours |
| `entry_distance(track)` | Computed — BFS from departure exits, normalised to 1-based rank | Only set for `parking_allowed` tracks; defaults to 0 |
| `track_length(track)` | `location_solver.json → trackParts[].length` | Parking tracks: actual length. Non-parking tracks: 10⁹ (infinite). |
| `astack_distance(track)` | Derived from initial train placements | Starts at 0; increases as trains leave from the a-side end |
| `bstack_distance(track)` | Derived from initial train placements | Sum of lengths of all trains initially on the track |
| `number_of_trains_on_track(track)` | Derived from initial train placements | Count of trains initially on each track |
| `aside_distance(train)` | Derived from initial placement order | Position of train's a-side from track origin |
| `parking_allowed(track)` | `location_solver.json → trackParts[].parkingAllowed` | |
| `coupling_allowed(track)` | Same as `parkingAllowed` (baseline proxy) | |
| `available(unit)` | `scenario_solver.json → in.trains / inStanding.trains` | True for units not locked inside an explicit-uncoupling composition |
| `compatible(unit, slot)` | Computed from unit and request unit type | Set when display name, carriage count, and length match |
| `total_cost` | — | Initialised to 0 |

---

## Costs
> Plan metric: `(:metric minimize (total_cost))` — total elapsed time in seconds

| Action | Cost (seconds) | Rationale |
|--------|---------------|-----------|
| `move_aside_empty` | +300 | 5 minutes per movement (thesis estimate) |
| `move_aside_occupied` | +300 | |
| `move_bside_empty` | +300 | |
| `move_bside_occupied` | +300 | |
| `wait` | +300 | Idle time costs the same as active time |
| `arrive` | 0 | Mandatory; timing already enforced by `total_cost >= arrival(t)` precondition |
| `park` | 0 | Goal-completing action |
| `depart_aside` / `depart_bside` | 0 | Goal-completing action |
| `start_move` / `end_move` | 0 | Administrative bracket |
| `match` | 0 | Administrative |
| `uncouple` | +120 | From NS `splitDuration` (2 minutes) |
| `couple_two_units` | +180 | From NS `combineDuration` (3 minutes) |
| `couple_two_units_same_train` | +180 | From NS `combineDuration` (3 minutes) |

---

## Unified Shunting-Unit Converter (v0.10)
> Applies to `src/convert/explicit_no_switch` and `src/convert/explicit_switch`. All movement, parking, coupling, splitting, servicing and departure happen on the shunting-unit (`shuntingunit`) layer. The legacy arrival-train movement layer is retired (see flags). The `explicit_switch` variant keeps switch track parts; `explicit_no_switch` removes them and reconnects boundaries.

### Feature flags
> Each is a module constant with a matching CLI override on `convert.py` (default in parentheses). Per-combination folders set these directly.

| Flag | CLI | Effect |
|------|-----|--------|
| `ENABLE_CORRIDOR` (True) | `--corridor` / `--no-corridor` | Restrict movement connectivity to a per-scenario corridor of relevant routes; off = full graph |
| `CORRIDOR_EXPAND_HOPS` (2) | `--corridor-hops N` | Hops of maneuvering room kept around the corridor's core routes |
| `CORRIDOR_EDGE_LEVEL` (False, `explicit_switch` only) | `--corridor-edges` | Keep only route edges plus maneuver edges, not every edge between corridor nodes |
| `ENFORCE_COUPLING_ORDER` (False) | `--coupling-order` / `--no-coupling-order` | Require exact-adjacent physical coupling order; off = composition-only |
| `ENABLE_SERVICING` (False) | `--servicing` / `--no-servicing` | Require trains with service tasks to be serviced before departing/parking |
| `ENABLE_ARRIVAL_PRECEDENCE` (False) | `--arrival-precedence` / `--no-arrival-precedence` | Inbound trains move only in scheduled arrival order |
| `ENABLE_ARRIVAL_LAYER` (False, locked off) | — | The redundant arrival-train movement layer is not emitted (bug fix; not a flag) |

### Per-combination folders
`src/convert/` contains a generated folder per combination of corridor / coupling-order / servicing / arrival-precedence across `no_switch` and `switch` (32 folders, e.g. `corridor_no_switch_explicit_servicing_timing`), plus a `baseline` folder (no_switch with corridor + servicing + arrival-precedence on, coupling-order off). Each folder is a copy of the base converter with the flags hardcoded; `run.py` auto-discovers them.

### Shunting-unit fluents
| Fluent | Signature | Type | Description |
|--------|-----------|------|-------------|
| `active_su` | `(shuntingunit)` | Bool | Whether a shunting unit currently exists physically |
| `at_su` | `(shuntingunit, trackpart)` | Bool | Physical location of a shunting unit |
| `su_length` | `(shuntingunit)` | Real | Total length of the shunting unit |
| `su_aside_distance` | `(shuntingunit)` | Real | Stack position of the SU's a-side |
| `allowed_to_move_su` | `(shuntingunit)` | Bool | Movement-session token (set by `start_move_su`, cleared by `end_move_su`/depart) |
| `su_may_move` | `(shuntingunit)` | Bool | Whether the SU is permitted to start moving (set after split/couple; inStanding single units movable) |
| `parked_su` | `(shuntingunit)` | Bool | SU has been parked (SU-layer parking goal); cannot move again |
| `must_depart_su` | `(shuntingunit)` | Bool | Assembled request SU: may only depart, never re-park |
| `contains_su` | `(shuntingunit, trainunit)` | Bool | Membership of units in a shunting unit |
| `single_unit_su` | `(shuntingunit, trainunit)` | Bool | Marks shunting units containing exactly one unit |
| `request_su_for_request` | `(shuntingunit, departurerequest)` | Bool | Links an assembled request SU to its outgoing request |
| `departed_su` | `(shuntingunit)` | Bool | Marks that the SU has left the yard |
| `coupled_to_request` | `(trainunit, departurerequest)` | Bool | Records a unit assembled into a request |
| `serviced` | `(shuntingunit)` | Bool | Service complete (default true; false for units with tasks). `ENABLE_SERVICING` |
| `service_allowed` | `(trackpart)` | Bool | Track has a service facility. `ENABLE_SERVICING` |
| `facility_type` | `(trackpart, facilitytype)` | Bool | Which facility type a track provides. `ENABLE_SERVICING` |
| `requires_facility` | `(shuntingunit, facilitytype)` | Bool | Which facility type a unit needs. `ENABLE_SERVICING` |
| `su_has_arrived` | `(shuntingunit)` | Bool | Whether the SU's train has arrived (default true; false for in-trains). `ENABLE_ARRIVAL_PRECEDENCE` |
| `su_previous_arrived` | `(shuntingunit)` | Bool | Whether the previous train in the arrival order has arrived. `ENABLE_ARRIVAL_PRECEDENCE` |
| `su_arrival_immediately_before` | `(shuntingunit, shuntingunit)` | Bool | Arrival-order chain between consecutive in-trains. `ENABLE_ARRIVAL_PRECEDENCE` |

### Shunting-unit actions
- `start_move_su` / `end_move_su` — bracket a movement session (mirror of arrival-layer brackets).
- `move_aside_empty_su` / `move_aside_occupied_su` / `move_bside_empty_su` / `move_bside_occupied_su` — SU movement.
- `depart_aside_su` / `depart_bside_su` — depart an assembled SU; both increment `num_of_departed_trains` (v0.10 fix).
- `depart_aside_su_for_request` / `depart_bside_su_for_request` — depart a single-unit SU for a one-unit request.
- `park_su` — settle an SU on a parking-allowed track; counts toward `number_of_parked_trains`; blocked for `must_depart_su`.
- `split_two_unit_su` / `split_three_unit_su` — split a multi-unit SU into single-unit SUs.
- `couple_two_sus` — couple two single-unit SUs into a request SU; sets `must_depart_su`; physical order gated by `ENFORCE_COUPLING_ORDER`.
- `service_su` — service an SU at a matching facility (`ENABLE_SERVICING`); `serviced` gates depart/park/couple/split.
- `arrive_su` — admit an in-train in arrival order, unlocking the next (`ENABLE_ARRIVAL_PRECEDENCE`).
- `match`, `uncouple` — unchanged.

### Removed / retired
- `couple_two_units` and `couple_two_units_same_train` removed: they set `request_assembled` but never activated the request SU, leaving `departed_su(request_su)` unreachable (dead-end search traps).
- Arrival-train movement actions (`start_move`, `move_*`, `park`, `turn_*`, `depart_*`) are no longer emitted (`ENABLE_ARRIVAL_LAYER` locked off); their definitions remain as dead code for reference.
- `outStanding` requests are parking goals only — no longer counted as departures.

---

## Known Gaps / TODOs

### Gap 1 — `free` fluent is declared but never maintained
**What's missing:** `free(trackpart)` is referenced in documentation but not present in the current implementation. Capacity is instead tracked via `astack_distance`, `bstack_distance`, and `number_of_trains_on_track`.
**Impact:** No impact — capacity is correctly enforced by the stack-distance model.
**Fix:** Remove any remaining references to `free` from documentation.

---

### Gap 2 — Track capacity enforcement (resolved)
**Status:** Resolved via `astack_distance` / `bstack_distance` / `track_length`. Move actions check available space before allowing a train onto a track.

---

### ~~Gap 3~~ — Arrival timing (resolved in v0.7)
**Status:** Resolved. Inbound trains are absent from the initial state. The `arrive(t, l)` action has precondition `total_cost >= arrival(t)`, enforcing exact arrival timing. The `wait` action advances the clock when no other work is available. Standing trains are initialised with `has_arrived = true` and placed directly.

---

### ~~Gap 4~~ — Service subproblem (resolved in v0.10, opt-in)
**Status:** Resolved on the shunting-unit layer behind `ENABLE_SERVICING` (`--servicing`). `service_allowed`, `facility_type`, `requires_facility`, and `serviced` fluents plus a `service_su(su, l, f)` action are added; `serviced` gates `depart_*_su`, `park_su`, `couple_two_sus`, and `split_*_su`. Facilities are read from `location_solver.json → facilities[]` (Reinigingsperron / Wasmachine / Monteur); a unit needs service when any `members[].tasks` is present.
**Remaining:** Facility capacity (`simultaneousUsageCount`) is not yet enforced — a facility can service unlimited units at once.

---

### Gap 5 — Matching subproblem (partial)
**Status:** Partially resolved. Request slots, `match`, `compatible`, and coupling actions are implemented. Train-type predicates in parking (enforcing that a parked train satisfies a specific outbound request type) are not yet linked.

---

### Gap 6 — `park` uses no departure ordering
**What's missing:** The current `park` action only checks `at(t, l)` and `parking_allowed(l)`. The `departure_rank = entry_distance` ordering constraint from earlier versions has been removed.
**Impact:** The planner may park trains in configurations where an earlier-departing train is blocked by a later-departing one.
**Fix:** Re-add `departure_rank(t) <= entry_distance(l)` precondition to `park` and restore the `departure_rank` fluent initialisation in `convert.py`.

---

### Gap 7 — Multi-unit coupling duration (partially resolved in v0.8)
**Status:** Coupling costs are now modelled: `uncouple` = 120s, `couple_two_units` / `couple_two_units_same_train` = 180s (hardcoded from NS scenario data). Driver/staff resources and temporal overlap are still not modelled.
**Remaining:** Treat staff resources and per-type duration variation as a later temporal/resource-planning variant.

---

### Gap 8 — Concurrency limit is classical only
**What's missing:** `max_concurrent_movements = 1` enforces sequential movement in classical planning. For a temporal planner (TFD), durative actions would be needed to properly limit concurrent movements.
**Impact:** No impact for classical planning. A switch to temporal planning would require refactoring move actions to durative-actions.

---

### ~~Gap 9~~ — No plan-cost metric (resolved in v0.7)
**Status:** Resolved. `total_cost` fluent added (seconds). Each move and each `wait` adds 300 seconds. `(:metric minimize (total_cost))` is emitted in the problem file.

---

### Gap 10 — Entry tracks not exposed as PDDL facts
**What's missing:** `entry_track_of(train, track)` now exists for inbound trains (v0.7), but there is no generic `is_entry_track(trackpart)` predicate marking which tracks are yard entry points for arbitrary queries or future routing checks.
**Impact:** Low — `entry_track_of` is sufficient for the `arrive` action. A generic entry-track predicate would help if routing subproblem needs to distinguish entry tracks from others.
**Fix:** Add `is_entry_track(trackpart)` populated from `entryTrackPart` fields in the scenario JSON if needed.