### Version 0
- Get an `end_move_su` after `arrive_su`
  - removed `allowed_to_move_su` from `arrive_su` effects
  - required for uncoupling
- `arrive_su` is not first to arrive
  - removed two lines:
    ```
    for su in ordered_arrival_sus:
    problem.set_initial_value(su_previous_arrived(su), True)
    ```
  - increase lenghts of rail2/3/4/5 in LocationSimpleService to 420 to fit longer trains

### Version 1
- `concurrent_movements` is increased after arrive, not allowing `start_move_su` to activate
  - Removing this effect now allows the arrive of the next train straight away...
- from `rail_4` move only to `rail_1` in the middle, then an extra action to the each of the other tracks from there
  - after moving to aside `rail_1` can then move to either aside on rail 2 or 3 or Bside of rail 4 or 5
- expected uncouple action after arriving, requires `allowed_to_move`
    - changed precondition to `su_may_move`

- `end_move` vs `park_su`
- `su_may_move` vs `allowed_to_move`
  - `su_may_move`
    - all arriving and instanding trains start as a shunting unit that may move
    - required for `start_move_su`
    - effect of `complete_request_composition` and `compiled_adopt_composition`
    - `compiled_uncouple_front/back` makes true for both parent and child, and set `active_su` for only child
      - `active_su` not set to false on parent
  - `allowed_to_move_su`
    - required false for `start_move` - set to true after
    - required true for move a/b sides
    - required true for `park` - set to false after
    - required true for `end_move` set to false after
    - required true for `depart` - set to false after
    - used in when effect `compiled_adopt_composition`
      - `(when (allowed_to_move_su ?source_su) (allowed_to_move_su ?request_su)) (when (allowed_to_move_su ?source_su) (not (allowed_to_move_su ?source_su)))`
    - required true for parent in uncouple - set to false after on parent, not to true on child

### Version 2
- uncouple can only happen after service
  - remove precondition
- uncouple front and back have `(<= 2 (su_unit_count ?parent_su))`
  - back must have >2 otherwise always uncouple the front
- should change to consistent use of `(decrease (concurrent_movements) 1)` and `(increase (concurrent_movements) 1)`
- uncoupling requires `(not (allowed_to_move_su))`
- servicing requires `(not (allowed_to_move_su))`
- require servicing should be put on the trainunit or only on the shunting unit if all units require servicing

### Friday
- arrival of 22222 requires departure of 11113 - which is wrong
- coupling of units in 11113 is not possible