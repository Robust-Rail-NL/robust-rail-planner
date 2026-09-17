"""Demonstrates a physical-validity gap in the PDDL movement model.

The scenario's yard has the 906b stub served by a single throat (its A-side,
the only connection back to the network). Two shunting units can therefore end
up sharing 906b, with the *second* unit parked between the *first* unit and
the exit. Physically the first unit cannot then leave 906b without driving
through the second one.

This test builds the real `compiled_matching` PDDL problem for the
KleineBinckhorst scenario and simulates exactly that situation with
Unified-Planning's sequential simulator: su_train2 joins su_train1 on 906b via
the throat, and su_train1 is then still allowed to move 906b -> 906a.

This is a canary test for the exclusive-path-clearance fix. While the bug is
present it asserts that the model allows the illegal pass-through; once the
model enforces that only the outermost unit at a track's throat may leave it,
flip the final assertion to `assert not applicable`.
"""

import os
from pathlib import Path

from unified_planning.engines.sequential_simulator import UPSequentialSimulator
from unified_planning.io import PDDLReader
from unified_planning.plans import ActionInstance

from convert_to_pddl.corridor_no_switch_unlimited_order_servicing_discrete_compiled_matching import convert

REPO = Path(__file__).resolve().parent.parent


def _general_inputs():
    candidates = [
        os.environ.get("RRN_INPUTS_DIR"),
        REPO / "robust-rail-general",           # CI checkout beside the planner
        REPO.parent / "robust-rail-general",    # the usual local layout
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_dir():
            return candidate
    raise RuntimeError(
        "cannot find robust-rail-general. Set RRN_INPUTS_DIR, or clone it "
        "beside this repo."
    )


def _build_problem(tmp_path):
    root = Path(_general_inputs()) / "Location_KleineBinckhorst"
    convert.create_instance_from_scenario(
        location_file=str(root / "location.json"),
        scenario_file=str(root / "fixtures" / "feasible" / "scenario_feasible_small_s01.json"),
        domain_file=str(tmp_path / "domain.pddl"),
        output_file=str(tmp_path / "problem.pddl"),
    )
    return PDDLReader().parse_problem(
        str(tmp_path / "domain.pddl"), str(tmp_path / "problem.pddl")
    )


def _apply(problem, sim, state, plan, action_name, *params):
    objs = {o.name: o for o in problem.all_objects}
    action_instance = ActionInstance(problem.action(action_name), [objs[p] for p in params])
    assert sim.is_applicable(state, action_instance), action_name + " not applicable"
    plan.append(action_instance)
    return sim.apply(state, action_instance)


def test_pddl_allows_a_train_to_pass_through_another_on_906b(tmp_path):
    problem = _build_problem(tmp_path)
    sim = UPSequentialSimulator(problem)
    state = sim.get_initial_state()
    plan = []

    # su_train2 (arrival 0) arrives, enters the yard on 906b, then moves aside
    # to o_52 so 906b is free again.
    state = _apply(problem, sim, state, plan, "arrive_su", "su_train2", "sein70")
    state = _apply(problem, sim, state, plan, "enter_yard_su", "su_train2", "sein70", "o_906b")
    state = _apply(problem, sim, state, plan, "start_move_su", "su_train2")
    state = _apply(problem, sim, state, plan, "move_aside_empty_su", "su_train2", "o_906b", "o_906a")
    state = _apply(problem, sim, state, plan, "move_bside_empty_su", "su_train2", "o_906a", "o_52")
    state = _apply(problem, sim, state, plan, "end_move_su", "su_train2", "o_52")

    # su_train1 (arrival 900) arrives and occupies 906b.
    state = _apply(problem, sim, state, plan, "arrive_su", "su_train1", "sein70")
    state = _apply(problem, sim, state, plan, "enter_yard_su", "su_train1", "sein70", "o_906b")

    # su_train2 moves back and joins su_train1 on 906b through the throat,
    # ending up between su_train1 and the 906b -> 906a exit.
    state = _apply(problem, sim, state, plan, "start_move_su", "su_train2")
    state = _apply(problem, sim, state, plan, "move_aside_empty_su", "su_train2", "o_52", "o_906a")
    state = _apply(problem, sim, state, plan, "move_bside_occupied_su", "su_train2", "o_906a", "o_906b")
    state = _apply(problem, sim, state, plan, "end_move_su", "su_train2", "o_906b")

    # su_train1 tries to leave 906b through su_train2. Physically impossible;
    # the model (incorrectly) allows it.
    state = _apply(problem, sim, state, plan, "start_move_su", "su_train1")
    pass_through = ActionInstance(
        problem.action("move_aside_empty_su"),
        [{o.name: o for o in problem.all_objects}[name] for name in ("su_train1", "o_906b", "o_906a")],
    )
    applicable = sim.is_applicable(state, pass_through)

    # Print the plan the PDDL model made.
    print("\n=== plan made by the PDDL model ===")
    for i, action_instance in enumerate(plan, 1):
        print(f"{i:>2}. {action_instance}")
    print(f"    -> pass-through move_aside_empty_su(su_train1 o_906b o_906a)"
          f" applicable: {applicable}")

    if applicable:
        state = sim.apply(state, pass_through)
        at_su = problem.fluent("at_su")
        em = problem.environment.expression_manager
        objs = {o.name: o for o in problem.all_objects}
        su1_on_906a = state.get_value(em.FluentExp(at_su, (objs["su_train1"], objs["o_906a"])))
        su2_on_906b = state.get_value(em.FluentExp(at_su, (objs["su_train2"], objs["o_906b"])))
        print(f"    -> after: su_train1 on o_906a: {su1_on_906a};"
              f" su_train2 still on o_906b: {su2_on_906b}")

    assert not applicable, (
        "the model still allows su_train1 to drive 906b -> 906a through "
        "su_train2; the exclusive-path-clearance fix is not in place"
    )