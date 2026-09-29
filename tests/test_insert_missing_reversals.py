"""Unit tests for insert_missing_reversals (issue #48).

A reversal spanning a real stop (a Wait separating two independently closed
runs) is invisible to find_reversal_indices/compute_reversals on either
side of it: the track where it happens is the last track of the closing
run and the first track of the reopened one, an endpoint of each path,
never an interior one. insert_missing_reversals covers the narrow, always-
safe case: the reversal's own duration fits inside the Wait between the two
runs, so it can be carved out of that dwell time with no other timestamp in
the plan moving.
"""

from convert_plan_to_tors import convert_to_tors as C


def _action(su, kind, start, end, location=0, resources=()):
    return {
        "startTime": C._as_time(start),
        "endTime": C._as_time(end),
        "taskType": {"predefined": kind},
        "shuntingUnit": {"id": su, "memberIDs": [su], "parentIDs": [], "childIDs": []},
        "location": location,
        "resources": [{"kind": "trackPart", "id": r} for r in resources],
    }


# Track 2's 'a' side reaches both neighbours 1 and 3 (a switch fan on one
# side) -- entering from 1 and leaving toward 3 (or vice versa) is a
# same-side turnaround: a genuine reversal.
_REVERSAL_A_ADJ = {2: [1, 3]}
_REVERSAL_B_ADJ = {}

# Track 2 connects to 1 on its 'a' side and to 3 on its 'b' side: an
# ordinary pass-through, ordinary in both directions, never a reversal.
_STRAIGHT_A_ADJ = {2: [1]}
_STRAIGHT_B_ADJ = {2: [3]}

_TRAIN_LOOKUP = {
    0: {
        "members": ["u1"],
        "member_types": [{"backNormTime": 100, "backAdditionTime": 0, "carriages": 0}],
    },
}


def test_inserts_a_reverse_when_the_wait_has_enough_slack():
    move1 = _action(0, "Move", 0, 60, location=1, resources=[2])
    wait = _action(0, "Wait", 61, 1000, location=2)
    move2 = _action(0, "Move", 1001, 1061, location=2, resources=[3])

    out = C.insert_missing_reversals(
        [move1, wait, move2], _REVERSAL_A_ADJ, _REVERSAL_B_ADJ, _TRAIN_LOOKUP, {}, {})

    kinds = [a["taskType"]["predefined"] for a in out]
    assert kinds == ["Move", "Wait", "Reverse", "Move"], kinds

    new_wait, reverse = out[1], out[2]
    # The Wait shrinks by exactly reversal_duration from its end.
    assert new_wait["startTime"] == 61
    assert new_wait["endTime"] == 1000 - 100
    # The Reverse fills the reclaimed slice exactly, so the next Move's own
    # start (never touched) still lines up with no gap or overlap.
    assert reverse["startTime"] == 1000 - 100
    assert reverse["endTime"] == 1000
    assert reverse["location"] == 2
    assert reverse["resources"] == []
    assert out[3]["startTime"] == 1001  # untouched


def test_leaves_it_alone_when_the_wait_is_too_short():
    move1 = _action(0, "Move", 0, 60, location=1, resources=[2])
    wait = _action(0, "Wait", 61, 100, location=2)  # only 39s, less than reversal_duration=100
    move2 = _action(0, "Move", 101, 161, location=2, resources=[3])

    out = C.insert_missing_reversals(
        [move1, wait, move2], _REVERSAL_A_ADJ, _REVERSAL_B_ADJ, _TRAIN_LOOKUP, {}, {})

    assert out == [move1, wait, move2]


def test_leaves_an_ordinary_straight_wait_alone():
    move1 = _action(0, "Move", 0, 60, location=1, resources=[2])
    wait = _action(0, "Wait", 61, 1000, location=2)
    move2 = _action(0, "Move", 1001, 1061, location=2, resources=[3])

    out = C.insert_missing_reversals(
        [move1, wait, move2], _STRAIGHT_A_ADJ, _STRAIGHT_B_ADJ, _TRAIN_LOOKUP, {}, {})

    assert out == [move1, wait, move2]


def test_leaves_adjacent_runs_with_no_wait_alone():
    # No Wait at all between the two runs (e.g. a Service-only gap, or a
    # bookkeeping close-and-reopen with zero dwell) -- out of scope for the
    # easy case, even though it is a genuine reversal.
    move1 = _action(0, "Move", 0, 60, location=1, resources=[2])
    move2 = _action(0, "Move", 60, 120, location=2, resources=[3])

    out = C.insert_missing_reversals(
        [move1, move2], _REVERSAL_A_ADJ, _REVERSAL_B_ADJ, _TRAIN_LOOKUP, {}, {})

    assert out == [move1, move2]


def test_does_not_confuse_two_different_shunting_units():
    su0_move1 = _action(0, "Move", 0, 60, location=1, resources=[2])
    su0_wait = _action(0, "Wait", 61, 1000, location=2)
    su0_move2 = _action(0, "Move", 1001, 1061, location=2, resources=[3])
    # SU 1 has no reversal at all -- an ordinary Wait between two Moves that
    # don't touch track 2's switch fan.
    su1_move1 = _action(1, "Move", 0, 60, location=10, resources=[11])
    su1_wait = _action(1, "Wait", 61, 1000, location=11)
    su1_move2 = _action(1, "Move", 1001, 1061, location=11, resources=[12])

    out = C.insert_missing_reversals(
        [su0_move1, su0_wait, su0_move2, su1_move1, su1_wait, su1_move2],
        _REVERSAL_A_ADJ, _REVERSAL_B_ADJ, _TRAIN_LOOKUP, {}, {})

    kinds = [(a["shuntingUnit"]["id"], a["taskType"]["predefined"]) for a in out]
    assert kinds == [
        (0, "Move"), (0, "Wait"), (0, "Reverse"), (0, "Move"),
        (1, "Move"), (1, "Wait"), (1, "Move"),
    ], kinds
