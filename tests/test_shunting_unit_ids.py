"""In-standing trains are named by index (train_in_standing_0), incoming trains
by scenario id (train0); the converter must never give the two the same id."""

from convert_plan_to_tors.convert_to_tors import _as_id
from convert_to_pddl.convert import _tracks_reachable_through_connectors


def test_in_standing_train_does_not_share_an_id_with_incoming_train0():
    assert _as_id("su_train_in_standing_0") != _as_id("su_train0")
    assert _as_id("train_in_standing_0") == _as_id("su_train_in_standing_0")


def test_incoming_trains_and_units_keep_their_scenario_ids():
    assert _as_id("su_train3") == 3
    assert _as_id("unit2801") == 2801


def test_arrival_options_stop_at_the_first_track_with_length():
    # arrival -> switch -> parking_a -> switch -> parking_b
    adjacency = {
        "arrival": {"sw1"},
        "sw1": {"arrival", "parking_a"},
        "parking_a": {"sw1", "sw2"},
        "sw2": {"parking_a", "parking_b"},
        "parking_b": {"sw2"},
    }
    lengths = {"arrival": 300, "sw1": 0, "parking_a": 400, "sw2": 0, "parking_b": 400}
    track_part_by_id = {tid: {"length": length} for tid, length in lengths.items()}

    reached = _tracks_reachable_through_connectors(adjacency, track_part_by_id, "arrival")

    assert reached == {"parking_a"}
