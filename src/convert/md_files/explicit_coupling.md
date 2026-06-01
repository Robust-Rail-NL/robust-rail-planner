# Explicit Coupling Variant

## Purpose
This converter variant models matching and coupling as a physical shunting-unit flow. It is intended for experiments where the planner must prove that matched units can be assembled, moved, and departed as one outgoing composition.

## Subproblems
- [x] Matching
- [x] Coupling / uncoupling
- [x] Shunting-unit movement after split and coupling
- [x] Routing interaction through shared track occupancy

## Main Modelling Choices
- Incoming train units remain atomic `trainunit` objects for matching.
- Movable physical compositions are represented as `shuntingunit` objects.
- Multi-unit arrivals can be split into active single-unit shunting units.
- Two matched shunting units can be coupled only when they are adjacent, on the same request-specific coupling track, and ordered like the departure request slots.
- A two-unit request is complete only after the assembled request shunting unit departs.

## Key Fluents
| Fluent | Purpose |
|--------|---------|
| `active_su(shuntingunit)` | Whether a shunting unit currently exists physically |
| `contains_su(shuntingunit, trainunit)` | Membership of train units inside a shunting unit |
| `at_su(shuntingunit, trackpart)` | Physical location of a shunting unit |
| `single_unit_su(shuntingunit, trainunit)` | Marks shunting units that contain exactly one unit |
| `request_su_for_request(shuntingunit, departurerequest)` | Links an assembled request shunting unit to its outgoing request |
| `departed_su(shuntingunit)` | Marks that the assembled shunting unit has left the yard |
| `slot_before(requestslot, requestslot)` | Required order of two request slots |
| `coupling_allowed(trackpart)` | Track parts where physical coupling is allowed |
| `coupling_track_for_request(departurerequest, trackpart)` | Small request-specific set of allowed coupling tracks |

## Key Actions
| Action | Purpose |
|--------|---------|
| `match` | Assigns a compatible available train unit to a request slot |
| `split_two_unit_su` | Splits one two-unit shunting composition into two movable single-unit shunting units |
| `couple_two_sus` | Couples two active shunting units into one outgoing request shunting unit |
| `move_*_su` | Moves active shunting units through the yard |
| `depart_*_su` | Departs an assembled shunting unit |

## Notes
- The current explicit coupling implementation supports exactly two-unit outgoing coupling requests.
- Duration, staff resources, and temporal overlap are not modelled in this converter.
- In explicit coupling mode, original `arrivaltrain` movement is locked so the shunting-unit layer owns the physical movement state.
- `coupling_track_for_request` is used to prevent the planner from trying to assemble a request on every coupling-allowed track. The converter first prefers a request's `lastParkingTrackPart` or `leaveTrackPart` if it is a valid coupling track; otherwise it picks the nearest reachable coupling track.

## Known Performance Failure
- `scenario_solver_example3.json` with explicit coupling and no switches was run with ENHSP after the routing/coupling merge. It did not produce a plan after roughly 104 minutes.
- The last observed search status was about 1.7 million expanded nodes and 4.6 million evaluated states.
- This failure suggests that fully free physical coupling plus routing is too unconstrained for ENHSP on realistic multi-unit cases.
- The request-specific coupling-track restriction was added as the first corrective modelling choice: it keeps physical coupling, but removes the choice of arbitrary assembly location from the planner.
