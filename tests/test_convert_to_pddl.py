"""Unit tests for the scenario+location -> PDDL conversion stage, using the
simple-service fixture (tests/fixtures/simple_service/)."""


def test_produces_domain_and_problem_files(pddl_files):
    import os

    assert os.path.exists(pddl_files["domain"])
    assert os.path.exists(pddl_files["problem"])


def test_problem_declares_all_non_switch_track_parts(pddl_files):
    with open(pddl_files["problem"]) as f:
        problem_pddl = f.read()

    for track_name in ("bumper_in", "rail_transit", "rail_service", "rail_park", "bumper_out"):
        assert track_name in problem_pddl


def test_problem_wires_up_the_track_chain(pddl_files):
    with open(pddl_files["problem"]) as f:
        problem_pddl = f.read()

    # bumper_in -[b]-> rail_transit -[b]-> rail_service -[b]-> rail_park -[b]-> bumper_out
    assert "(connected_bside bumper_in rail_transit)" in problem_pddl
    assert "(connected_bside rail_transit rail_service)" in problem_pddl
    assert "(connected_bside rail_service rail_park)" in problem_pddl
    assert "(connected_bside rail_park bumper_out)" in problem_pddl


def test_problem_marks_the_service_track_and_exit(pddl_files):
    with open(pddl_files["problem"]) as f:
        problem_pddl = f.read()

    assert "(service_allowed rail_service)" in problem_pddl
    assert "(facility_type rail_service cleaning)" in problem_pddl
    assert "(departure_exit_b bumper_out)" in problem_pddl


def test_problem_goal_requires_the_single_departure(pddl_files):
    with open(pddl_files["problem"]) as f:
        problem_pddl = f.read()

    assert "(= (num_of_departed_trains) 1)" in problem_pddl
    assert "(request_departed request1)" in problem_pddl
