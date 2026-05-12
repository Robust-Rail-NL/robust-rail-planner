# TUSP-SS PDDL Domain Model

## Version History
| Version | Date  | Summary of Changes |
|---------|------|---------------|--------------------|
| v0.1 | 2026-04-28 | Summary of changes |
| v0.2 | 2026-05-12 | Added `park` action, `parking_allowed` and `parked` fluents |

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

### `park`
- **Parameters:** `t - arrivaltrain`, `l - trackpart`
- **Preconditions:**
  - `at(t, l)`
  - `parking_allowed(l)`
- **Effects:**
  - `parked(t) = true`
- **Introduced:** v0.2
- **Notes:** Every inbound train has `parked(t)` as a goal. A train must already be at the target track part and that track part must permit parking.

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
