import sys
from fractions import Fraction

from unified_planning.io import PDDLReader
from unified_planning.shortcuts import SequentialSimulator

from validate_plan import parse_plan_file


def _bool(state, fluent, *objects):
    return state.get_value(fluent(*objects)).bool_constant_value()


def _number(state, fluent, *objects):
    return Fraction(state.get_value(fluent(*objects)).constant_value())


def _audit_state(problem, state, step):
    track_type = problem.user_type("trackpart")
    su_type = problem.user_type("shuntingunit")
    tracks = list(problem.objects(track_type))
    shunting_units = list(problem.objects(su_type))

    active_su = problem.fluent("active_su")
    at_su = problem.fluent("at_su")
    frontmost_a = problem.fluent("frontmost_a_su")
    frontmost_b = problem.fluent("frontmost_b_su")
    behind = problem.fluent("behind_su")
    occupied_length = problem.fluent("occupied_length")
    track_length = problem.fluent("track_length")
    su_length = problem.fluent("su_length")
    track_count = problem.fluent("number_of_trains_on_track")

    locations = {}
    for su in shunting_units:
        if not _bool(state, active_su, su):
            continue
        matches = [track for track in tracks if _bool(state, at_su, su, track)]
        if len(matches) != 1:
            raise AssertionError(
                f"step {step}: active {su.name} has {len(matches)} locations"
            )
        locations[su] = matches[0]

    for back in shunting_units:
        for front in shunting_units:
            if not _bool(state, behind, back, front):
                continue
            if back not in locations or front not in locations:
                raise AssertionError(
                    f"step {step}: stale order edge behind_su({back.name}, {front.name})"
                )
            if locations[back] != locations[front]:
                raise AssertionError(
                    f"step {step}: cross-track order edge behind_su({back.name}, {front.name})"
                )

    for track in tracks:
        if track.name == "phantom":
            continue
        occupants = [su for su, location in locations.items() if location == track]
        expected_length = sum((_number(state, su_length, su) for su in occupants), Fraction(0))
        actual_length = _number(state, occupied_length, track)
        capacity = _number(state, track_length, track)
        count = _number(state, track_count, track)

        if count != len(occupants):
            raise AssertionError(
                f"step {step}: {track.name} count {count} != {len(occupants)} active SUs"
            )
        if actual_length != expected_length:
            raise AssertionError(
                f"step {step}: {track.name} occupied length {actual_length} != {expected_length}"
            )
        if actual_length > capacity:
            raise AssertionError(
                f"step {step}: {track.name} exceeds capacity {actual_length} > {capacity}"
            )
        if not occupants:
            continue

        a_ends = [su for su in occupants if _bool(state, frontmost_a, su)]
        b_ends = [su for su in occupants if _bool(state, frontmost_b, su)]
        if len(a_ends) != 1 or len(b_ends) != 1:
            raise AssertionError(
                f"step {step}: {track.name} has {len(a_ends)} A-ends and {len(b_ends)} B-ends"
            )

        visited = [a_ends[0]]
        while True:
            next_units = [
                candidate
                for candidate in occupants
                if _bool(state, behind, candidate, visited[-1])
            ]
            if not next_units:
                break
            if len(next_units) != 1 or next_units[0] in visited:
                raise AssertionError(f"step {step}: {track.name} order is branched or cyclic")
            visited.append(next_units[0])

        if set(visited) != set(occupants) or visited[-1] != b_ends[0]:
            raise AssertionError(f"step {step}: {track.name} order chain is incomplete")


def audit_plan(domain_file, problem_file, plan_file):
    problem = PDDLReader().parse_problem(domain_file, problem_file)
    plan = parse_plan_file(problem, plan_file)
    simulator = SequentialSimulator(problem)
    state = simulator.get_initial_state()
    _audit_state(problem, state, 0)

    for step, action in enumerate(plan.actions, start=1):
        if not simulator.is_applicable(state, action):
            raise AssertionError(f"step {step}: inapplicable action {action}")
        state = simulator.apply(state, action)
        _audit_state(problem, state, step)

    if not simulator.is_goal(state):
        raise AssertionError("final state does not satisfy all goals")
    print(f"DISCRETE PLAN AUDIT PASSED: {len(plan.actions)} actions")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: audit_discrete_plan.py DOMAIN PROBLEM PLAN")
    audit_plan(*sys.argv[1:])
