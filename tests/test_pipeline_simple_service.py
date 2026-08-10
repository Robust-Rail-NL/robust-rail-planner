"""End-to-end tests of the PDDL -> plan -> TORS stages, driven by the real
Julia/SymbolicPlanners.jl backend, against the simple-service fixture."""
from conftest import requires_julia


@requires_julia
def test_planner_solves_the_fixture(raw_plan_file):
    with open(raw_plan_file) as f:
        lines = [line.strip() for line in f if line.strip()]

    assert lines, "planner produced an empty plan"


@requires_julia
def test_plan_visits_arrive_service_and_departs(raw_plan_file):
    with open(raw_plan_file) as f:
        plan_text = f.read()

    assert "arrive_su(su_train9001, bumper_in)" in plan_text
    assert "service_su(su_train9001, rail_service, cleaning)" in plan_text
    assert "depart_bside_su_for_request(su_train9001, unit9101, request1_slot0, request1, bumper_out)" in plan_text


@requires_julia
def test_service_action_happens_before_departure(raw_plan_file):
    with open(raw_plan_file) as f:
        lines = [line.strip() for line in f if line.strip()]

    service_index = next(i for i, line in enumerate(lines) if line.startswith("service_su"))
    depart_index = next(i for i, line in enumerate(lines) if line.startswith("depart_"))
    assert service_index < depart_index


@requires_julia
def test_validate_plan_accepts_the_planner_output(pddl_files, raw_plan_file):
    from plan.validate_plan import validate_plan

    assert validate_plan(pddl_files["domain"], pddl_files["problem"], raw_plan_file) is True


@requires_julia
def test_tors_plan_has_expected_action_shape(tors_plan):
    assert set(tors_plan.keys()) == {"actions", "trackParts"}
    actions = tors_plan["actions"]
    assert len(actions) > 0

    predefined_types = [a["taskType"].get("predefined") for a in actions]
    assert predefined_types[0] == "Arrive"
    assert predefined_types[-1] == "Exit"

    for action in actions:
        assert "startTime" in action and "endTime" in action
        assert int(action["endTime"]) >= int(action["startTime"])


@requires_julia
def test_tors_plan_includes_the_cleaning_service_task(tors_plan):
    service_actions = [
        a for a in tors_plan["actions"]
        if a["taskType"].get("other") == "Cleaning"
    ]
    assert len(service_actions) == 1
    assert service_actions[0]["location"] == "2"  # rail_service track id


@requires_julia
def test_tors_plan_carries_the_train_unit_through(tors_plan):
    for action in tors_plan["actions"]:
        member_ids = {m["id"] for m in action["shuntingUnit"]["members"]}
        assert member_ids == {"9101"}
