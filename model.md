# TUSP-SS PDDL Domain Model

## Version History
| Version | Date  | Summary of Changes |
|---------|------|---------------|--------------------|
| v0.1 | 2026-04-28 | Summary of changes |
| v0.2 | 2026-05-12 | Added `park` action, `parking_allowed` and `parked` fluents |
| v0.3 | 2026-05-12 | Parking subproblem: `connected` on `move`; `entry_distance` + `departure_rank` on `park` |

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
| `connected` | `(trackpart, trackpart)` | Bool | Whether two track parts are directly adjacent. Default: false. Set bidirectionally from `aSide`/`bSide` in `location_solver.json` | v0.3 |
| `entry_distance` | `(trackpart)` | Int | Normalised hop-distance from the yard's departure track (BFS). Rank 1 = closest to exit. Default: 0 (non-parking tracks). | v0.3 |
| `departure_rank` | `(arrivaltrain)` | Int | Rank of the train's departure time among all inbound trains (1 = first to depart). Ties get the same rank (lenient). | v0.3 |

---

## Actions
### `move`
- **Parameters:** `t - arrivaltrain`, `l_from - trackpart`, `l_to - trackpart`
- **Preconditions:**
  - `at(t, l_from)`
  - `connected(l_from, l_to)`
- **Effects:**
  - `at(t, l_to) = true`
  - `at(t, l_from) = false`
- **Introduced:** v0.1 (connectivity precondition added v0.3)
- **Notes:** 

### `park`
- **Parameters:** `t - arrivaltrain`, `l - trackpart`
- **Preconditions:**
  - `at(t, l)`
  - `parking_allowed(l)`
  - `departure_rank(t) = entry_distance(l)`
- **Effects:**
  - `parked(t) = true`
- **Introduced:** v0.2 (departure ordering precondition added v0.3)
- **Notes:** Every inbound train has `parked(t)` as a goal. The `departure_rank = entry_distance` constraint enforces that earlier-departing trains park closer to the yard exit, preventing blocking. Lenient: multiple tracks can share the same `entry_distance`, giving the planner a choice.

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

---

## Known Gaps / TODOs
