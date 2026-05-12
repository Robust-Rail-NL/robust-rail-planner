# Coupling/Matching Study Notes

## Scope

These runs isolate the matching and coupling/uncoupling part of the train shunting problem. They do not model full routing, parking, servicing, driver assignment, exact track positions, or evaluator-compatible physical plans.

The goal is to compare how much extra planner effort is introduced when coupling/uncoupling is made more explicit in PDDL.

## Variants

- `implicit_free_uncoupling`: train units are directly matched to departure slots. Coupling is assumed from the final assignment, and units in arriving compositions are immediately available.
- `implicit_explicit_uncoupling`: units from a multi-unit arriving composition must first be released using `uncouple(unit, composition)` before they can be matched.
- `explicit_coupling`: includes explicit uncoupling and additionally requires `couple_to_request(unit, slot, request)` after matching. Goals require coupled slots rather than only filled slots.

## Initial Results

Small examples `scenario_solver_example1.json`, `scenario_solver_example2.json`, and `scenario_solver_example3.json` solved in all three variants. This confirms that the modelling ladder is executable and behaves as expected on simple cases.

The larger `scenario_solver_random1.json` case shows the real modelling cost:

| Variant | Solved | Plan length | Planning time (ms) | Ground actions | Expanded nodes | States evaluated |
|---|---:|---:|---:|---:|---:|---:|
| `implicit_free_uncoupling` | yes | 21 | 111 | 245 | 41 | 3,885 |
| `implicit_explicit_uncoupling` | yes | 38 | 3,978 | 262 | 19,383 | 432,542 |
| `explicit_coupling` | yes | 59 | 152,292 | 507 | 257,082 | 5,686,950 |

Main takeaway: the modelling ladder behaves as expected. Small cases all solve easily, but the larger case shows the real cost. Explicit uncoupling greatly increases search effort, and explicit coupling creates a major blow-up in runtime, grounded actions, expanded nodes, and evaluated states.

## Difference From Prior Work

The actions in this study are abstract matching/coupling actions. They are designed to test modelling choices, not to produce a complete physical shunting plan.

In Lonyuk's thesis model, coupling and uncoupling are physical PDDL actions. They require trains to be on the same parking track, adjacent in the correct order, and operated by a driver. The thesis model also updates train length, train type, activity status, and numeric position fluents such as `aside_distance` and `train_length`. This makes the thesis actions much closer to a full shunting plan, but also much more complex.

In the local-search paper by van den Broek et al., matching assigns arriving train units to positions in departing trains, while splitting and combining are part of constructing a feasible shunting and service schedule. The local-search approach reasons over a richer schedule representation and uses heuristic search, not PDDL action grounding.

Our current actions sit between these two levels. They are more explicit than a pure type-count matching model because they use individual train units, request slots, uncoupling actions, and coupling actions. However, they are less physical than the thesis actions because they do not yet check track location, adjacency, timing, driver availability, routing conflicts, or parking feasibility.

This distinction is intentional: the current experiment measures the cost of adding coupling/uncoupling structure to the matching model before mixing in routing and parking complexity.
