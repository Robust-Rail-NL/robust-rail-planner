# TUSP-SS PDDL Domain Model

## Version History
| Version | Date | Summary of Changes |
|---------|------|--------------------|
| v0.1 | 2026-04-28 | Base model |
| v0.2 | 2026-05-12 | Added `park`, `parking_allowed`, and `parked` |
| v0.3 | 2026-05-12 | Added routing connectivity and entry-distance bookkeeping |
| v0.4 | 2026-05-18 | Added routing departure actions, capacity tracking, and movement concurrency |
| v0.5 | 2026-05-31 | Current routing-only no-switch variant: parking plus directional departure routing |

---

## Subproblems
> Which subproblems are currently modelled

- [x] Subproblem 1 — Parking
- [x] Subproblem 2 — Routing
- [ ] Subproblem 3 — Service Scheduling
- [ ] Subproblem 4 — Matching / Arrivals / Departures
- [ ] Subproblem 5 — Combining & Splitting

---

## Branch Additions
- `convert_no_switches.py` emits the parking/routing-only domain variant.
- The model uses `start_move`, `end_move`, `move_aside_*`, `move_bside_*`, `depart_aside`, `depart_bside`, and `park`.
- `run.py` now asks for subproblem or coupling settings only when a selected converter declares those flags.

---

## Types
| Type | Description | Introduced |
|------|-------------|------------|
| `trackpart` | A piece of track on the yard | v0.1 |
| `arrivaltrain` | An arriving train | v0.1 |

---

## Fluents
| Fluent | Signature | Type | Description | Introduced |
|--------|-----------|------|-------------|------------|
| `arrival` | `(arrivaltrain)` | Int | Arrival timestamp of a train | v0.1 |
| `at` | `(arrivaltrain, trackpart)` | Bool | Whether a train is at a given track part. Default: false | v0.1 |
| `parking_allowed` | `(trackpart)` | Bool | Whether a track part permits parking. Default: false | v0.2 |
| `parked` | `(arrivaltrain)` | Bool | Whether a train has been parked. Default: false | v0.2 |
| `departed` | `(arrivaltrain)` | Bool | Whether a train has left the yard after parking. Default: false | v0.4 |
| `connected_aside` | `(trackpart, trackpart)` | Bool | Directed adjacency for aSide connections | v0.4 |
| `connected_bside` | `(trackpart, trackpart)` | Bool | Directed adjacency for bSide connections | v0.4 |
| `departure_exit_a` | `(trackpart)` | Bool | Whether a track part can be used as an aside departure exit | v0.4 |
| `departure_exit_b` | `(trackpart)` | Bool | Whether a track part can be used as a bside departure exit | v0.4 |
| `entry_distance` | `(trackpart)` | Int | Normalised hop-distance from the exit root | v0.3 |
| `number_of_parked_trains` | `(trackpart)` | Int | Number of parked trains on a track part | v0.5 |
| `number_of_trains_on_track` | `(trackpart)` | Int | Number of trains currently on a track part | v0.4 |
| `num_of_departed_trains` | `()` | Int | Counter for departed trains | v0.4 |
| `track_length` | `(trackpart)` | Real | Total length on a track part | v0.2 |
| `train_length` | `(arrivaltrain)` | Real | Total length of a train | v0.2 |
| `aside_distance` | `(arrivaltrain)` | Real | Distance from the aside edge for the train | v0.4 |
| `astack_distance` | `(trackpart)` | Real | Occupied length on the aside stack of a track part | v0.4 |
| `bstack_distance` | `(trackpart)` | Real | Occupied length on the bside stack of a track part | v0.4 |
| `allowed_to_move` | `(arrivaltrain)` | Bool | Whether a train is currently allowed to move | v0.4 |
| `concurrent_movements` | `()` | Int | Counter for currently active movements | v0.5 |

---

## Actions
### `start_move`
- **Parameters:** `t - arrivaltrain`
- **Description:** Starts a movement for a train and increments the movement counter.

### `end_move`
- **Parameters:** `t - arrivaltrain`, `l - trackpart`
- **Description:** Ends a movement once the train is on a parking-allowed track part.

### `move_aside_empty`
- **Parameters:** `t - arrivaltrain`, `l_from - trackpart`, `l_to - trackpart`
- **Description:** Moves a train from one track part to another over an empty aside connection.

### `move_aside_occupied`
- **Parameters:** `t - arrivaltrain`, `l_from - trackpart`, `l_to - trackpart`
- **Description:** Moves a train over an aside connection onto an occupied track part.

### `move_bside_empty`
- **Parameters:** `t - arrivaltrain`, `l_from - trackpart`, `l_to - trackpart`
- **Description:** Moves a train from one track part to another over an empty bside connection.

### `move_bside_occupied`
- **Parameters:** `t - arrivaltrain`, `l_from - trackpart`, `l_to - trackpart`
- **Description:** Moves a train over a bside connection onto an occupied track part.

### `depart_aside`
- **Parameters:** `t - arrivaltrain`, `l - trackpart`
- **Description:** Removes a parked train through an aside exit track part.

### `depart_bside`
- **Parameters:** `t - arrivaltrain`, `l - trackpart`
- **Description:** Removes a parked train through a bside exit track part.

### `park`
- **Parameters:** `t - arrivaltrain`, `l - trackpart`
- **Description:** Marks a train as parked on a parking-allowed track part.

---

## Initial State
> What gets populated from the JSON inputs

| Entity | Source | What is set |
|--------|--------|-------------|
| `trackpart` objects | `location.json -> trackParts` | One object per track part, named by `name` |
| `arrivaltrain` objects | `scenario.json -> in.trains` and `inStanding.trains` | One object per train, named `train{id}` or `train_in_standing_{index}` |
| `arrival(train)` | `scenario.json -> in.trains[].arrival` | Set to the arrival timestamp |
| `at(train, track)` | `scenario.json -> in.trains[].entryTrackPart` or `firstParkingTrackPart` | Set to true for the initial position |
| `connected_aside(a, b)` | `location_solver.json -> trackParts[].aSide` | Set bidirectionally for aside adjacency |
| `connected_bside(a, b)` | `location_solver.json -> trackParts[].bSide` | Set bidirectionally for bside adjacency |
| `departure_exit_a(track)` | `scenario.json -> out.trainRequests[].leaveTrackPart` | Set when the track part can act as an aside exit |
| `departure_exit_b(track)` | `scenario.json -> out.trainRequests[].leaveTrackPart` | Set when the track part can act as a bside exit |
| `parking_allowed(track)` | `location_solver.json -> trackParts[].parkingAllowed` | Set from the location data |
| `entry_distance(track)` | Computed BFS rank | Set for parking-allowed tracks only |
| `track_length(track)` | `location_solver.json -> trackParts[].length` | Set to the track length, or a large value for non-parking tracks |
| `train_length(train)` | `scenario.json -> in.trains[].members[].trainUnit.type.length` | Sum of all unit lengths |
| `aside_distance(train)` | Derived from initial train placement | Set to the occupied aside distance on the track |
| `astack_distance(track)` | Derived from initial occupancy | Set to the occupied aside stack length |
| `bstack_distance(track)` | Derived from initial occupancy | Set to the occupied bside stack length |
| `number_of_trains_on_track(track)` | Derived from initial occupancy | Set to the number of trains initially on the track |
| `num_of_departed_trains()` | Constant counter | Starts at 0 and is incremented by `depart_aside` / `depart_bside` |

---

## Notes
- This variant models parking and routing only.
- Matching and coupling are intentionally not part of this specification.
