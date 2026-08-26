# TUSP-SS PDDL Domain Model — `baseline_no_parameters`

## Version History
| Version | Date | Summary of Changes |
|---------|------|--------------------|
| v1.0 | 2026-07-07 | Full model: arrivals, routing, parking, splitting, coupling, matching, servicing, departure |

---

## Subproblems
> Which subproblems are currently modelled

- [x] Subproblem 1 — Parking
- [x] Subproblem 2 — Routing
- [x] Subproblem 3 — Service Scheduling
- [x] Subproblem 4 — Matching / Arrivals / Departures
- [x] Subproblem 5 — Combining & Splitting
- [x] Subproblem 6 — Parking Requests (outStanding)

---

## Branch Additions
- `baseline_no_parameters/convert.py` emits the full PDDL model with all subproblems enabled.
- The model uses a single shunting-unit layer (no separate arrival-train layer) for all movement, parking, coupling and departure.
- Switch-like zero-length connector nodes are collapsed; the track graph is reconnected via direct boundary-to-boundary links.
- A per-scenario corridor filter restricts movement connectivity to relevant routes.
- Inbound trains are handled in scheduled arrival order via `arrive_su` and arrival-precedence fluents.
- Servicing tasks (cleaning, washing, inspection) require the shunting unit to visit the matching facility track before departure or parking.
- Multi-unit inbound compositions are split into individual shunting units via `split_two_unit_su` / `split_three_unit_su`.
- Outbound requests are fulfilled by matching train units to slots, coupling them via `couple_two_sus`, and departing the assembled shunting unit.
- Standing-out parking requests define slots on specific tracks; compatible units must park there and be marked as used.

---

## Types
| Type | Description | Introduced |
|------|-------------|------------|
| `trackpart` | A piece of track on the yard | v1.0 |
| `trainunit` | An atomic train unit | v1.0 |
| `departurerequest` | An outbound departure request | v1.0 |
| `requestslot` | A slot in a departure request for one train unit | v1.0 |
| `arrivalcomposition` | A multi-unit composition arriving together | v1.0 |
| `shuntingunit` | A movable shunting unit (can be split, coupled, moved) | v1.0 |
| `parkingrequest` | A standing-out parking request | v1.0 |
| `parkingslot` | A slot in a parking request for one train unit | v1.0 |
| `facilitytype` | A service facility type (cleaning, washing, inspection) | v1.0 |

---

## Objects
| Object | Type | Description |
|--------|------|-------------|
| `phantom` | `trackpart` | A synthetic track part used as a holding location for arriving trains before they enter the yard |

---

## Fluents

### Track / Routing
| Fluent | Signature | Type | Description | Default |
|--------|-----------|------|-------------|---------|
| `parking_allowed` | `(trackpart)` | Bool | Whether a track part permits parking | false |
| `turning_allowed` | `(trackpart)` | Bool | Whether a track part allows turning (saw movement) | false |
| `connected_aside` | `(trackpart, trackpart)` | Bool | Directed adjacency for aSide connections | false |
| `connected_bside` | `(trackpart, trackpart)` | Bool | Directed adjacency for bSide connections | false |
| `departure_exit_a` | `(trackpart)` | Bool | Whether a track part can be used as an aside departure exit | false |
| `departure_exit_b` | `(trackpart)` | Bool | Whether a track part can be used as a bside departure exit | false |
| `entry_distance` | `(trackpart)` | Int | Normalised hop-distance from the exit root | 0 |
| `number_of_parked_trains` | `(trackpart)` | Int | Number of parked trains on a track part | 0 |
| `number_of_trains_on_track` | `(trackpart)` | Int | Number of trains currently on a track part | 0 |
| `num_of_departed_trains` | `()` | Int | Counter for departed trains | 0 |
| `track_length` | `(trackpart)` | Real | Total length of a track part | 0 |
| `astack_distance` | `(trackpart)` | Real | Occupied length on the aside stack of a track part | 0 |
| `bstack_distance` | `(trackpart)` | Real | Occupied length on the bside stack of a track part | 0 |
| `concurrent_movements` | `()` | Int | Counter for currently active movements | 0 |
| `coupling_allowed` | `(trackpart)` | Bool | Whether coupling is permitted on this track part | false |

### Matching
| Fluent | Signature | Type | Description | Default |
|--------|-----------|------|-------------|---------|
| `available` | `(trainunit)` | Bool | Whether a train unit is available for matching | false |
| `request_open` | `(departurerequest)` | Bool | Whether a departure request is still open | false |
| `slot_open` | `(requestslot)` | Bool | Whether a request slot is open for matching | false |
| `slot_filled` | `(requestslot)` | Bool | Whether a request slot has been filled by a match | false |
| `compatible` | `(trainunit, requestslot)` | Bool | Whether a train unit is type-compatible with a slot | false |
| `matched` | `(trainunit, requestslot)` | Bool | Whether a train unit has been matched to a slot | false |
| `slot_for_request` | `(requestslot, departurerequest)` | Bool | Which request a slot belongs to | false |
| `slot_before` | `(requestslot, requestslot)` | Bool | Ordering constraint between slots of the same request | false |
| `unit_before` | `(trainunit, trainunit)` | Bool | Ordering constraint between units in a composition | false |
| `coupling_track_for_request` | `(departurerequest, trackpart)` | Bool | Which track a request prefers for coupling | false |

### Shunting Unit
| Fluent | Signature | Type | Description | Default |
|--------|-----------|------|-------------|---------|
| `active_su` | `(shuntingunit)` | Bool | Whether a shunting unit exists (is active) | false |
| `contains_su` | `(shuntingunit, trainunit)` | Bool | Whether a shunting unit contains a given train unit | false |
| `at_su` | `(shuntingunit, trackpart)` | Bool | Whether a shunting unit is at a given track part | false |
| `departed_su` | `(shuntingunit)` | Bool | Whether a shunting unit has departed | false |
| `single_unit_su` | `(shuntingunit, trainunit)` | Bool | Whether a shunting unit represents exactly one given train unit | false |
| `request_su_for_request` | `(shuntingunit, departurerequest)` | Bool | Which assembled SU is allocated to which request | false |
| `request_departed` | `(departurerequest)` | Bool | Whether a request has been fulfilled by a departure | false |
| `su_length` | `(shuntingunit)` | Real | Total physical length of a shunting unit | 0 |
| `su_aside_distance` | `(shuntingunit)` | Real | Distance from the aside edge for a shunting unit | 0 |
| `allowed_to_move_su` | `(shuntingunit)` | Bool | Whether a shunting unit is currently allowed to move | false |
| `su_may_move` | `(shuntingunit)` | Bool | Whether a shunting unit is permitted to initiate movement | false |
| `must_depart_su` | `(shuntingunit)` | Bool | Whether an assembled shunting unit must depart and cannot park | false |
| `parked_su` | `(shuntingunit)` | Bool | Whether a shunting unit has been parked | false |
| `su_has_arrived` | `(shuntingunit)` | Bool | Whether an inbound shunting unit has been processed by `arrive_su` | true |
| `su_previous_arrived` | `(shuntingunit)` | Bool | Whether the previous inbound train in arrival order has arrived | false |
| `su_arrival_immediately_before` | `(shuntingunit, shuntingunit)` | Bool | Arrival ordering chain between inbound SUs | false |

### Arrival
| Fluent | Signature | Type | Description | Default |
|--------|-----------|------|-------------|---------|
| `su_arrival_track` | `(shuntingunit, trackpart)` | Bool | Which track an arriving train should be placed on | false |

### Parking Request (outStanding)
| Fluent | Signature | Type | Description | Default |
|--------|-----------|------|-------------|---------|
| `parking_slot_for_request` | `(parkingslot, parkingrequest)` | Bool | Which request a parking slot belongs to | false |
| `parking_slot_track` | `(parkingslot, trackpart)` | Bool | Which track a parking slot targets | false |
| `parking_compatible` | `(trainunit, parkingslot)` | Bool | Whether a train unit is type-compatible with a parking slot | false |
| `parking_slot_fulfilled` | `(parkingslot)` | Bool | Whether a parking slot has been fulfilled | false |
| `parked_unit_used` | `(trainunit)` | Bool | Whether a parked unit has been claimed for a parking slot | false |

### Service
| Fluent | Signature | Type | Description | Default |
|--------|-----------|------|-------------|---------|
| `service_allowed` | `(trackpart)` | Bool | Whether a track part permits servicing | false |
| `facility_type` | `(trackpart, facilitytype)` | Bool | The facility type of a service track | false |
| `requires_facility` | `(shuntingunit, facilitytype)` | Bool | Whether a shunting unit requires a given facility type | false |
| `serviced` | `(shuntingunit)` | Bool | Whether a shunting unit has been serviced | true |

### Coupling / Assembly
| Fluent | Signature | Type | Description | Default |
|--------|-----------|------|-------------|---------|
| `slot_coupled` | `(requestslot)` | Bool | Whether a slot has been physically coupled | false |
| `coupled_to_request` | `(trainunit, departurerequest)` | Bool | Whether a train unit has been coupled to a request | false |
| `physically_coupled` | `(trainunit, trainunit)` | Bool | Whether two train units are physically coupled | false |
| `request_assembled` | `(departurerequest)` | Bool | Whether a two-unit request has been fully assembled | false |

### Composition
| Fluent | Signature | Type | Description | Default |
|--------|-----------|------|-------------|---------|
| `part_of_composition` | `(trainunit, arrivalcomposition)` | Bool | Whether a train unit belongs to an arriving composition | false |
| `composition_needs_uncoupling` | `(arrivalcomposition)` | Bool | Whether a composition needs to be uncoupled before units can be used | false |

---

## Actions

### `start_move_su`
- **Parameters:** `su - shuntingunit`
- **Description:** Starts a movement for a shunting unit and increments the movement counter. The SU must be active, not parked, not already moving, and must have arrived.

### `arrive_su`
- **Parameters:** `su - shuntingunit`, `l - trackpart`
- **Description:** Processes an inbound train's arrival: moves it from the phantom track onto its arrival track, sets its aside distance to the track's bstack, and marks it as arrived. Only fires when the previous inbound has already arrived.

### `park_su`
- **Parameters:** `su - shuntingunit`, `l - trackpart`
- **Description:** Marks a shunting unit as parked on a parking-allowed track part. The SU must not have `must_depart_su` set (assembled request units cannot park).

### `end_move_su`
- **Parameters:** `su - shuntingunit`, `l - trackpart`
- **Description:** Ends a movement on a parking-allowed track, decrementing the movement counter. The SU must not have `must_depart_su` set.

### `move_aside_empty_su`
- **Parameters:** `su - shuntingunit`, `l_from - trackpart`, `l_to - trackpart`
- **Description:** Moves a shunting unit over an aside connection onto an empty track part. The SU must be at the aside stack of the source track.

### `move_aside_occupied_su`
- **Parameters:** `su - shuntingunit`, `l_from - trackpart`, `l_to - trackpart`
- **Description:** Moves a shunting unit over an aside connection onto an occupied track part, stacking it behind existing content.

### `move_bside_empty_su`
- **Parameters:** `su - shuntingunit`, `l_from - trackpart`, `l_to - trackpart`
- **Description:** Moves a shunting unit over a bside connection onto an empty track part.

### `move_bside_occupied_su`
- **Parameters:** `su - shuntingunit`, `l_from - trackpart`, `l_to - trackpart`
- **Description:** Moves a shunting unit over a bside connection onto an occupied track part.

### `depart_aside_su`
- **Parameters:** `su - shuntingunit`, `l - trackpart`
- **Description:** Removes a shunting unit through an aside exit. The SU must be at the aside stack of the exit track.

### `depart_bside_su`
- **Parameters:** `su - shuntingunit`, `l - trackpart`
- **Description:** Removes a shunting unit through a bside exit. The SU must be at the bside stack of the exit track.

### `depart_aside_su_for_request`
- **Parameters:** `su - shuntingunit`, `unit - trainunit`, `slot - requestslot`, `request - departurerequest`, `l - trackpart`
- **Description:** Departs a single-unit shunting unit through an aside exit, marking the specific request as departed.

### `depart_bside_su_for_request`
- **Parameters:** `su - shuntingunit`, `unit - trainunit`, `slot - requestslot`, `request - departurerequest`, `l - trackpart`
- **Description:** Departs a single-unit shunting unit through a bside exit, marking the specific request as departed.

### `uncouple`
- **Parameters:** `unit - trainunit`, `composition - arrivalcomposition`
- **Description:** Makes a single train unit available by uncoupling it from its arriving composition.

### `split_two_unit_su`
- **Parameters:** `parent_su - shuntingunit`, `left_su - shuntingunit`, `right_su - shuntingunit`, `unit_a - trainunit`, `unit_b - trainunit`, `composition - arrivalcomposition`, `track - trackpart`
- **Description:** Splits a two-unit composition into two separate shunting units. The parent SU is deactivated and both child SUs become active and movable.

### `split_three_unit_su`
- **Parameters:** `parent_su - shuntingunit`, `first_su - shuntingunit`, `second_su - shuntingunit`, `third_su - shuntingunit`, `unit_a - trainunit`, `unit_b - trainunit`, `unit_c - trainunit`, `composition - arrivalcomposition`, `track - trackpart`
- **Description:** Splits a three-unit composition into three separate shunting units.

### `couple_two_sus`
- **Parameters:** `su_a - shuntingunit`, `su_b - shuntingunit`, `su_result - shuntingunit`, `unit_a - trainunit`, `unit_b - trainunit`, `track - trackpart`, `slot_a - requestslot`, `slot_b - requestslot`, `request - departurerequest`
- **Description:** Physically couples two single-unit SUs on a coupling-allowed track into one assembled SU. The assembled SU is marked `must_depart_su` and can only depart, never park.

### `service_su`
- **Parameters:** `su - shuntingunit`, `l - trackpart`, `f - facilitytype`
- **Description:** Services a shunting unit at a facility track of the required type.

### `match`
- **Parameters:** `unit - trainunit`, `slot - requestslot`
- **Description:** Matches an available train unit to an open compatible slot, filling the slot and consuming the unit.

### `parking_fulfill`
- **Parameters:** `su - shuntingunit`, `unit - trainunit`, `slot - parkingslot`, `l - trackpart`
- **Description:** Fulfils a parking slot by claiming a parked unit that is type-compatible and on the correct track.

---

## Initial State
> What gets populated from the JSON inputs

| Entity | Source | What is set |
|--------|--------|-------------|
| `trackpart` objects | `location.json -> trackParts` | One object per non-switch track part in the corridor, named by `name` |
| `shuntingunit` objects | `scenario.json -> in.trains` and `inStanding.trains` | One SU per train, plus single-unit SUs for multi-unit compositions |
| `trainunit` objects | `scenario.json -> in/trains[].members[].trainUnit` | One object per train unit |
| `departurerequest` objects | `scenario.json -> out.trainRequests` | One object per request |
| `parkingrequest` objects | `scenario.json -> outStanding.trainRequests` | One object per standing-out request |
| `facilitytype` objects | `location.json -> facilities[].type` | One object per unique facility type |
| `active_su(su)` | All inbound and standing trains | Set to true for the initial SU |
| `at_su(su, track)` | `entryTrackPart` / `firstParkingTrackPart` | Set for standing trains; arriving trains start on `phantom` |
| `su_arrival_track(su, track)` | `entryTrackPart` / `firstParkingTrackPart` | Set for inbound trains to define their arrival placement |
| `su_has_arrived(su)` | Inbound trains | Set to false (must go through `arrive_su`) |
| `su_previous_arrived(su)` | First inbound train by arrival time | Set to true to activate the arrival chain |
| `su_arrival_immediately_before(a, b)` | Sorted inbound trains | Chained in arrival-time order |
| `su_may_move(su)` | All trains | Set to true (standing single-unit trains start as movable) |
| `contains_su(su, unit)` | Train members | Set for each unit inside its parent SU |
| `single_unit_su(su, unit)` | Pre-allocated single-unit SUs | Set for single-unit compositions and split-target SUs |
| `su_length(su)` | `members[].trainUnit.type.length` | Sum of all unit lengths |
| `su_aside_distance(su)` | Derived from initial track occupancy | Set for standing trains based on existing occupancies |
| `available(unit)` | Units not in multi-unit compositions | Set to true (compositions require uncouple first) |
| `part_of_composition(unit, comp)` | Multi-unit compositions | Set for units that arrive as part of a multi-unit train |
| `composition_needs_uncoupling(comp)` | Multi-unit trains | Set to true |
| `unit_before(a, b)` | Train member order | Set for consecutive units in a composition |
| `parking_allowed(track)` | `trackParts[].parkingAllowed` | Set from the location data |
| `turning_allowed(track)` | `trackParts[].sawMovementAllowed` | Set for turnable track parts |
| `coupling_allowed(track)` | `trackParts[].parkingAllowed` | Set on the same tracks as parking |
| `connected_aside(a, b)` / `connected_bside(a, b)` | `trackParts[].aSide` / `bSide` | Set bidirectionally for corridor-track pairs |
| `departure_exit_a / departure_exit_b(track)` | `out.trainRequests[].leaveTrackPart` | Set when the track part can act as an exit |
| `entry_distance(track)` | Computed BFS rank from exit | Set for parking-allowed tracks in the corridor |
| `track_length(track)` | `trackParts[].length` | Set to the track length, or a large value for non-parking tracks |
| `astack_distance(track)` | Derived from initial occupancy | Starts at 0 |
| `bstack_distance(track)` | Derived from initial occupancy | Set to the total occupied length from standing trains |
| `number_of_trains_on_track(track)` | Derived from initial occupancy | Set to the count of standing trains |
| `service_allowed(track)` | `facilities[].relatedTrackParts` | Set for tracks linked to a facility |
| `facility_type(track, ftype)` | `facilities[].type` | Set for tracks linked to a facility |
| `requires_facility(su, ftype)` | `members[].tasks[].type.other` | Set for SUs whose members have service tasks |
| `serviced(su)` | Trains with service tasks | Set to false for SUs that need servicing (default true otherwise) |
| `slot_open(slot)` | `out.trainRequests[].trainUnits[]` | Set for each request slot |
| `slot_for_request(slot, request)` | Request slot membership | Set for each slot |
| `compatible(unit, slot)` | Type key matching | Set when a unit's type matches the slot's requirement |
| `coupling_track_for_request(request, track)` | Computed from scenario | Set for the preferred coupling track per request |
| `request_su_for_request(su, request)` | Two-unit requests | Pre-allocated SU that must be assembled and departed |
| `slot_before(a, b)` | Two-unit request slot order | Set for the two slots of a two-unit request |
| `parking_slot_for_request(slot, request)` | `outStanding.trainRequests[].trainUnits[]` | Set for each parking slot |
| `parking_slot_track(slot, track)` | Request's `lastParkingTrackPart` | Set for each parking slot |
| `parking_compatible(unit, slot)` | Type key matching | Set when a unit's type matches the slot's requirement |
| `num_of_departed_trains()` | Constant counter | Starts at 0 |
| `concurrent_movements()` | Constant counter | Starts at 0 |

---

## Goals
| Goal | Condition |
|------|-----------|
| Departure count | `num_of_departed_trains == len(out.trainRequests)` |
| Departure (single-unit request) | `request_departed(request)` |
| Assembly + departure (two-unit request) | `request_assembled(request)` and `departed_su(request_su)` |
| Parking slot fulfilment | `parking_slot_fulfilled(slot)` for each standing-out slot |

---

## Notes
- This variant models the complete yard shunting problem: arrivals, routing, parking, splitting, coupling, matching, servicing, and departure.
- All movement uses the shunting-unit layer only — there is no separate arrival-train layer.
- Switch-like zero-length track parts are collapsed; boundary track parts are reconnected directly.
- The corridor filter can be adjusted via `CORRIDOR_EXPAND_HOPS` (default 3).
- Only one concurrent movement is allowed (`max_concurrent_movements = 1`).
