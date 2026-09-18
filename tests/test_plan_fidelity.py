"""Fidelity tests: the converter must place every rest/Wait and Exit on exactly
the track the plan parked the train, and the Exit on the request's
lastParkingTrackPart — never on the PDDL depart action's separate track (a
zero-length signal a train cannot occupy)."""

import os

from convert_plan_to_tors.convert_to_tors import convert_plan

FIXTURES_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "simple_service"
)
LOCATION_FILE = os.path.join(FIXTURES_DIR, "location.json")
SCENARIO_FILE = os.path.join(FIXTURES_DIR, "scenarios", "scenario_simple.json")

# Fixture track ids: 0=bumper_in, 1=rail_transit, 2=rail_service (parkable),
# 3=rail_park (parkable), 4=bumper_out. Request 1 leaves via bumper_out but its
# lastParkingTrackPart is 3, so the Exit must sit on 3.
RAIL_SERVICE = 2
RAIL_PARK = 3
BUMPER_OUT = 4

PARK_THEN_DEPART_PLAN = [
    "(arrive_su su_train9001 rail_park)",
    "(start_move_su su_train9001)",
    "(move_aside_occupied_su su_train9001 rail_park rail_service)",
    "(service_su su_train9001 rail_service cleaning)",
    "(start_move_su su_train9001)",
    "(move_aside_occupied_su su_train9001 rail_service rail_park)",
    "(park_su su_train9001 rail_park)",
    "(depart_bside_su_for_request su_train9001 unit9101 request1_slot0 request1 bumper_out)",
]


def _run_plan(tmp_path, lines):
    plan_file = tmp_path / "plan.plan"
    plan_file.write_text("\n".join(lines) + "\n")
    return convert_plan(str(plan_file), SCENARIO_FILE, LOCATION_FILE)


def _actions_of_kind(plan, *kinds):
    return [
        a for a in plan["actions"]
        if a["taskType"].get("predefined") in kinds
    ]


def test_wait_and_exit_stay_on_the_planned_park_track(tmp_path):
    # The depart action's track is bumper_out (4), but the train parks on and
    # leaves from rail_park (3). The converter must not move the train.
    plan = _run_plan(tmp_path, PARK_THEN_DEPART_PLAN)

    waits = _actions_of_kind(plan, "Wait")
    exits = _actions_of_kind(plan, "Exit")

    assert len(waits) == 1
    assert waits[0]["location"] == RAIL_PARK, waits

    assert len(exits) == 1
    assert exits[0]["location"] == RAIL_PARK, exits
    assert exits[0]["location"] != BUMPER_OUT


def test_exit_happens_at_the_request_deadline(tmp_path):
    plan = _run_plan(tmp_path, PARK_THEN_DEPART_PLAN)
    exits = _actions_of_kind(plan, "Exit")

    assert len(exits) == 1
    # Request 1's departure time (scenario "arrival") is 1000.
    assert exits[0]["startTime"] == 1000
    assert exits[0]["endTime"] == 1000


def test_rested_track_is_never_redecided(tmp_path):
    # No action may place the train on a track it was not planned to occupy:
    # in particular nothing may ever sit on the zero-length bumper_out signal.
    plan = _run_plan(tmp_path, PARK_THEN_DEPART_PLAN)

    for a in plan["actions"]:
        assert a["location"] != BUMPER_OUT, a

    # Every resting action ends on rail_park; the train's last position is the
    # plan's park track.
    last_by_su = {}
    for a in plan["actions"]:
        if a["taskType"].get("predefined") == "Move":
            loc = a["resources"][-1]["id"] if a["resources"] else a["location"]
        else:
            loc = a["location"]
        last_by_su[a["shuntingUnit"]["id"]] = loc
    for su_id, loc in last_by_su.items():
        assert loc == RAIL_PARK, (su_id, loc)


def test_schedule_is_feasible_for_fixture_plan(tmp_path):
    plan = _run_plan(tmp_path, PARK_THEN_DEPART_PLAN)
    # A feasible plan: the departing train is ready long before its deadline,
    # and no fidelity violation is reported.
    assert len(_actions_of_kind(plan, "Arrive")) == 1

    times = []
    for a in plan["actions"]:
        times.append(a["startTime"])
        times.append(a["endTime"])
    assert max(times) <= 1000


# The 4-train KleineBinckhorst plan is the golden pipeline output the user
# reported against. The converter keeps the PDDL plan's own order but lets
# trains run concurrently: Waits are anchored to the unit's own last Move and
# may overlap other trains, while a move line guarantees no two Moves overlap.
# Arrivals stay at their scenario times and Exits land on the request's
# departure time.
KLEINEBINCKHORST_PLAN = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "fixtures", "kleinebinckhorst", "plan.plan",
)


def _kleinebinckhorst_inputs():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.environ.get("RRN_INPUTS_DIR"),
        os.path.join(repo_root, "robust-rail-general"),  # CI checkout beside this repo
        os.path.abspath(os.path.join(repo_root, "..", "robust-rail-general")),  # local layout
    ]
    for candidate in candidates:
        if not candidate:
            continue
        scenario = os.path.join(
            candidate, "Location_KleineBinckhorst", "scenarios",
            "scenario_KleineBinckhorst_4t_random_1s_feasible_small.json",
        )
        location = os.path.join(candidate, "Location_KleineBinckhorst", "location.json")
        if os.path.isfile(scenario) and os.path.isfile(location):
            return scenario, location
    raise RuntimeError(
        "cannot find the KleineBinckhorst inputs (robust-rail-general). "
        "Set RRN_INPUTS_DIR, or clone the repo beside this one."
    )


def test_list_order_waits_and_moves_follow_the_concurrent_plan(tmp_path):
    """The action list appears in the PDDL plan's own order (not by start
    time), arrivals are forced at their scenario times, and each train's Exit
    lands on its request's departure time. Trains run concurrently — a train
    may rest while another moves. The one cross-train constraint is the
    railway: no two Move actions may overlap. Each departure Wait is anchored
    to the unit's last arrival or Move and runs until it drives to the exit."""
    scenario_file, location_file = _kleinebinckhorst_inputs()
    plan = convert_plan(KLEINEBINCKHORST_PLAN, scenario_file, location_file)

    actions = plan["actions"]
    assert actions, "expected a non-empty plan"
    assert actions[0]["taskType"]["predefined"] == "Arrive"

    def members(a):
        return frozenset(a["shuntingUnit"]["memberIDs"])

    def predefined(a):
        return a["taskType"].get("predefined")

    # No two Moves may overlap (the shared railway).
    moves = [a for a in actions if predefined(a) == "Move"]
    for i, a in enumerate(moves):
        for b in moves[i + 1:]:
            if members(a) == members(b):
                continue
            a_end = int(a["endTime"])
            b_start = int(b["startTime"])
            b_end = int(b["endTime"])
            assert not (b_start < a_end and b_end > int(a["startTime"])), (
                "overlapping Moves:", a, b
            )

    arrivals = [a for a in actions if predefined(a) == "Arrive"]
    exits = [a for a in actions if predefined(a) == "Exit"]
    assert len(arrivals) == len(exits) == 4

    # First appearance in the PDDL: train2, train1, train3, train0; each
    # Arrive forced at its scenario arrival time.
    arrival_order = [
        (frozenset({3, 2}), 0),     # train2
        (frozenset({1}), 900),      # train1
        (frozenset({4}), 3600),     # train3
        (frozenset({0}), 4500),     # train0
    ]
    got_arrivals = [(members(a), int(a["startTime"])) for a in arrivals]
    assert got_arrivals == arrival_order, got_arrivals

    # The JSON list order mirrors the plan step by step: each train's first
    # action appears before the next train's first action.
    first_index = {}
    for i, a in enumerate(actions):
        m = members(a)
        if m not in first_index:
            first_index[m] = i
    assert [first_index[m] for m, _ in arrival_order] == sorted(
        first_index[m] for m, _ in arrival_order
    )

    # Exits follow the plan's departure order and land exactly on their
    # request's departure time (the deadline pin; moves never collide here).
    exit_deadlines = {
        frozenset({0}): 5400,     # request4 (train0)
        frozenset({4}): 7200,     # request7 (train3)
        frozenset({3, 2}): 9000,  # request6 (train2)
        frozenset({1}): 9900,     # request5 (train1)
    }
    got_exits = [(members(a), int(a["startTime"])) for a in exits]
    assert got_exits == list(exit_deadlines.items()), got_exits

    # Each departure Wait sits directly after the unit's last position-setting
    # action and runs until that unit's approach Move starts.
    waits = [a for a in actions if predefined(a) == "Wait"]
    assert len(waits) == 4
    for i, wait in enumerate(actions):
        if predefined(wait) != "Wait":
            continue
        cluster = members(wait)
        anchor = actions[i - 1]
        assert members(anchor) == cluster and predefined(anchor) in ("Arrive", "Move"), (
            "Wait must follow the unit's own arrival or last Move", wait, anchor
        )
        expected_start = int(anchor["endTime"]) + (1 if predefined(anchor) == "Move" else 0)
        assert int(wait["startTime"]) == expected_start, wait
        approach = next(
            a for a in actions[i + 1:] if predefined(a) == "Move" and members(a) == cluster
        )
        assert int(wait["endTime"]) == int(approach["startTime"]), wait


# ── combine-split chaining regression ──────────────────────────────────────
# The s07 feasible_small plan exercises combine/split with member trains that
# must not be left moving when the combine fires.  The fixture lives next to
# this file; the scenario lives in the sibling robust-rail-general repo.

COMBINE_SPLIT_PLAN = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "fixtures", "combine_split", "plan.plan",
)


def _resolve_feasible_scenario(stem="scenario_feasible_small_s07"):
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.environ.get("RRN_INPUTS_DIR"),
        os.path.join(repo_root, "robust-rail-general"),
        os.path.abspath(os.path.join(repo_root, "..", "robust-rail-general")),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        scn = os.path.join(
            candidate, "Location_KleineBinckhorst", "fixtures", "feasible",
            stem + ".json",
        )
        loc = os.path.join(candidate, "Location_KleineBinckhorst", "location.json")
        if os.path.isfile(scn) and os.path.isfile(loc):
            return scn, loc
    raise RuntimeError("cannot find feasible_small scenario; set RRN_INPUTS_DIR")


def _members(a):
    return frozenset(a["shuntingUnit"].get("memberIDs", []))


def _predefined(a):
    return a["taskType"].get("predefined")


def test_combine_split_does_not_overlap_member_moves(tmp_path):
    """After the combine-split chaining pass, a Combine/Split event may not
    overlap any Move (or other occupancy) that touches one of its member
    wagons.  This was the root cause of 'SU already active' in TORS."""
    scenario_file, location_file = _resolve_feasible_scenario()
    plan = convert_plan(COMBINE_SPLIT_PLAN, scenario_file, location_file)
    actions = plan["actions"]

    # Collect Combine/Split groups: events sharing identical
    # (predefined, location, startTime, endTime).
    _groups = {}
    for a in actions:
        k = _predefined(a)
        if k in ("Combine", "Split"):
            key = (k, a.get("location"), a["startTime"], a["endTime"])
            _groups.setdefault(key, set()).update(_members(a))

    for a in actions:
        k = _predefined(a)
        if k in ("Combine", "Split"):
            continue
        # Skip non-occupancy actions.
        if k in ("Arrive", "Exit", "StandOut", "Wait"):
            continue
        wagon_set = _members(a)
        for key, group_wagons in _groups.items():
            if not wagon_set & group_wagons:
                continue
            start, end = a["startTime"], a["endTime"]
            gs, ge = key[2], key[3]
            # Temporal overlap: s1 < e2 and s2 < e1.
            assert not (gs < end and start < ge), (
                f"{k} {sorted(wagon_set)} [{start},{end}] overlaps "
                f"{key[0]} group {sorted(group_wagons)} [{gs},{ge}]",
            )


def test_no_two_moves_overlap(tmp_path):
    scenario_file, location_file = _resolve_feasible_scenario()
    plan = convert_plan(COMBINE_SPLIT_PLAN, scenario_file, location_file)
    actions = plan["actions"]

    moves = [a for a in actions if _predefined(a) == "Move"]
    for i, a in enumerate(moves):
        for b in moves[i + 1:]:
            if _members(a) == _members(b):
                continue
            assert not (b["startTime"] < a["endTime"]
                        and b["endTime"] > a["startTime"]), (
                "overlapping Moves:", a, b,
            )
