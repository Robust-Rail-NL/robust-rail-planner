# Implicit Coupling Variant

## Purpose
This converter variant keeps matching lightweight by treating coupling as a logical consequence of filling the required request slots. It is useful as the baseline comparison against explicit physical coupling.

## Subproblems
- [x] Matching
- [ ] Physical coupling
- [ ] Shunting-unit assembly

## Main Modelling Choices
- Train units are matched to outgoing request slots using compatibility facts.
- A request slot is satisfied once a compatible unit is assigned by `match`.
- Coupling is implicit: if all slots of an outgoing request are filled, the model assumes the departure composition can be formed.
- No physical same-track, adjacency, movement-after-coupling, duration, or staff-resource checks are required.

## Key Fluents
| Fluent | Purpose |
|--------|---------|
| `available(trainunit)` | Whether a unit can still be assigned |
| `slot_open(requestslot)` | Whether a request slot is unfilled |
| `slot_filled(requestslot)` | Whether a request slot has been matched |
| `compatible(trainunit, requestslot)` | Whether a unit type fits the requested slot |
| `matched(trainunit, requestslot)` | Records the assignment chosen by the planner |

## Key Actions
| Action | Purpose |
|--------|---------|
| `match` | Assigns one compatible available unit to one open request slot |
| `uncouple` | Optional in explicit-uncoupling mode; releases a unit from an incoming composition before matching |

## Notes
- This variant is expected to be faster than explicit coupling because it does not require shunting-unit movement or physical assembly.
- It is less realistic because it does not prove that matched units can physically meet and couple inside the yard.
