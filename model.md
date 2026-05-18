# TUSP-SS PDDL Domain Model

## Version History
| Version | Date | Summary of Changes |
|---------|------|--------------------|
| v0.1 | 2026-04-28 | Summary of changes |
| v0.2 | 2026-05-12 | Added `park` action, `parking_allowed` and `parked` fluents |
| v0.3 | 2026-05-12 | Parking subproblem: `connected` on `move`; `entry_distance` + `departure_rank` on `park` |
| v0.4 | 2026-05-18 | Added the routing subproblem: `depart` action, `departure_exit` fluent, capacity tracking with `track_capacity`, `train_length`, `occupied_length`, and `track_is_parked_at` |

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
| `parking_allowed` | `(trackpart)` | Bool | Whether a track part permits parking. Default: false. Set from `parkingAllowed` in `location_solver.json` | v0.2 |
| `parked` | `(arrivaltrain)` | Bool | Whether a train has been parked. Default: false | v0.2 |
| `departed` | `(arrivaltrain)` | Bool | Whether a train has left the yard after parking. Default: false | v0.4 |
| `connected` | `(trackpart, trackpart)` | Bool | Whether two track parts are directly adjacent. Default: false. Set bidirectionally from `aSide`/`bSide` in `location_solver.json` | v0.3 |
| `departure_exit` | `(trackpart)` | Bool | Whether a track part is a yard exit where a train may depart. Default: false | v0.4 |
| `entry_distance` | `(trackpart)` | Int | Normalised hop-distance from the yard's departure track (BFS). Rank 1 = closest to exit. Default: 0 (non-parking tracks). | v0.3 |
| `departure_rank` | `(arrivaltrain)` | Int | Rank of the train's departure time among all inbound trains (1 = first to depart). Ties get the same rank (lenient). | v0.3 |
| `track_capacity` | `(trackpart)` | Real | Maximum length that can be parked on a track. Set from the track length in the location data. Default: 0 | v0.4 |
| `train_length` | `(arrivaltrain)` | Real | Total length of a train, computed from its units. Default: 0 | v0.4 |
| `occupied_length` | `(trackpart)` | Real | Current occupied length of a track. Increases when a train parks and decreases when it departs. Default: 0 | v0.4 |
| `track_is_parked_at` | `(trackpart)` | Bool | Whether a parked train currently occupies the track. Default: false | v0.4 |
| `num_of_departed_trains` | `()` | Int | Counter for how many trains have departed. Default: 0 | v0.4 |

---

## Actions
### `move`
- **Parameters:** `t - arrivaltrain`, `l_from - trackpart`, `l_to - trackpart`
- **Preconditions:**
  - `at(t, l_from)`
  - `connected(l_from, l_to)`
  - `free(l_to)`
- **Effects:**
  - `at(t, l_to) = true`
  - `at(t, l_from) = false`
  - `free(l_to) = false`
  - `free(l_from) = true`
  - `parked(t) = false`
  - `track_is_parked_at(l_from) = false`
- **Introduced:** v0.1 (connectivity precondition added v0.3)
- **Notes:** 

### `park`
- **Parameters:** `t - arrivaltrain`, `l - trackpart`
- **Preconditions:**
  - `at(t, l)`
  - `parking_allowed(l)`
  - `departure_rank(t) = entry_distance(l)`
  - `occupied_length(l) + train_length(t) <= track_capacity(l)`
- **Effects:**
  - `parked(t) = true`
  - `track_is_parked_at(l) = true`
  - `occupied_length(l) = occupied_length(l) + train_length(t)`
- **Introduced:** v0.2 (departure ordering precondition added v0.3)
- **Notes:** Every inbound train has `parked(t)` as a goal. The `departure_rank = entry_distance` constraint enforces that earlier-departing trains park closer to the yard exit, preventing blocking. Lenient: multiple tracks can share the same `entry_distance`, giving the planner a choice.

### `depart`
- **Parameters:** `t - arrivaltrain`, `l - trackpart`
- **Preconditions:**
  - `at(t, l)`
  - `parked(t)`
  - `departure_exit(l)`
- **Effects:**
  - `at(t, l) = false`
  - `parked(t) = false`
  - `departed(t) = true`
  - `free(l) = true`
  - `occupied_length(l)` decreases by `train_length(t)`
  - `num_of_departed_trains` increases by 1
- **Introduced:** v0.4
- **Notes:** Routing is still handled by `move`; `depart` only removes a parked train once it reaches an exit track. The final goal is now `departed(t)`.

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
| `connected(a, b)` | `location_solver.json → trackParts[].aSide / bSide` | Set bidirectionally for each adjacent pair |
| `entry_distance(track)` | Computed — BFS from `scenario.json → out.trainRequests[].leaveTrackPart`, normalised to 1-based rank | Only set for `parking_allowed` tracks; defaults to 0 |
| `departure_rank(train)` | Computed — rank of `scenario.json → in.trains[].departure` sorted ascending (ties share a rank) | |
| `track_capacity(track)` | `location_solver.json → trackParts[].length` | Set to the track length |
| `train_length(train)` | `scenario.json → in.trains[].members[].trainUnit.type.length` | Sum of all unit lengths |
| `occupied_length(track)` | Derived from initial inbound train placements | Accumulates the total length already occupying each track |
| `track_is_parked_at(track)` | Action effects | Initially false; set true when a train parks |
| `num_of_departed_trains()` | Constant counter | Starts at 0 and is incremented by `depart` |

---

## Known Gaps / TODOs
