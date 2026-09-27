"""Unit tests for consolidate_loops.

consolidate_loops merges each unit's *consecutive* Move actions into a single
Move over the run's net non-backtracking path. It must not merge a Move that is
separated from the next by any other action type, so every departing unit's
final exit-approach Move survives.
"""

import pytest

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


def test_collapse_loops_removes_excursion():
    assert C._collapse_loops([906, 52, 906]) == [906]
    assert C._collapse_loops([906, 59, 15, 15, 59, 41]) == [906, 59, 41]
    assert C._collapse_loops([1, 2, 3, 2, 5]) == [1, 2, 5]


def test_forward_only_run_merged_into_single_move():
    # Two consecutive forward Moves (906 -> 59 -> 15, then 15 -> 59 -> 41) must
    # collapse into a single Move, preserving startTime and recomputing endTime.
    a1 = _action(0, "Move", 0, 100, location=906, resources=[59, 15])
    a2 = _action(0, "Move", 101, 200, location=15, resources=[59, 41])
    out = C.consolidate_loops(
        [a1, a2], {}, {}, set(), {}, {}, {}, set(), {}, {})
    moves = [a for a in out if a["taskType"]["predefined"] == "Move"]
    assert len(moves) == 1
    assert moves[0]["startTime"] == 0
    assert moves[0]["location"] == 906
    assert [r["id"] for r in moves[0]["resources"]] == [59, 41]
    # endTime is recomputed from the net path's duration.
    assert moves[0]["endTime"] > moves[0]["startTime"]


def test_detour_run_collapses_to_net_transit():
    # entry -> 906, then 906 -> 52, then 52 -> 906 leaves the unit at 906; the
    # whole run reduces to a single Move ending at 906 (the net transit).
    a1 = _action(0, "Move", 0, 100, location=42, resources=[15, 59, 906])
    a2 = _action(0, "Move", 101, 200, location=906, resources=[59, 52])
    a3 = _action(0, "Move", 201, 300, location=52, resources=[59, 906])
    out = C.consolidate_loops(
        [a1, a2, a3], {}, {}, set(), {}, {}, {}, set(), {}, {})
    moves = [a for a in out if a["taskType"]["predefined"] == "Move"]
    # Net path is entry(42) -> 15 -> 59 -> 906.
    assert len(moves) == 1
    assert moves[0]["location"] == 42
    assert moves[0]["resources"][-1]["id"] == 906


def test_cancelling_detour_is_dropped():
    # A run that returns exactly to its start cancels out and is dropped
    # entirely (the unit is considered to remain where it stood).
    a1 = _action(0, "Move", 0, 100, location=906, resources=[59, 52])
    a2 = _action(0, "Move", 101, 200, location=52, resources=[59, 906])
    out = C.consolidate_loops(
        [a1, a2], {}, {}, set(), {}, {}, {}, set(), {}, {})
    moves = [a for a in out if a["taskType"]["predefined"] == "Move"]
    assert moves == []


def test_move_separated_by_wait_is_not_merged():
    # A Move, then a Wait, then a final Move (the departure approach) must keep
    # both Moves: non-consecutive Moves are never merged.
    m1 = _action(0, "Move", 0, 100, location=906, resources=[59, 15])
    w = _action(0, "Wait", 100, 8850, location=906)
    m2 = _action(0, "Move", 8850, 9000, location=906, resources=[59, 15])
    out = C.consolidate_loops(
        [m1, w, m2], {}, {}, set(), {}, {}, {}, set(), {}, {})
    kinds = [a["taskType"]["predefined"] for a in out]
    assert kinds == ["Move", "Wait", "Move"]
    assert out[0] != out[2]


# --- Regression: a reversal (saw) sitting exactly on a run-boundary split ---
#
# compute_reversals only inspects *interior* tracks of the path it is given
# (range(1, len(path) - 1)): a track that is the last element of one leg and
# the first element of the next is an endpoint in both, so a same-side
# turnaround sitting exactly on that seam is invisible to either half. This is
# the concrete mechanism behind the bug Tycho reported when he removed
# consolidate_loops: "allowing a train to move from a track to another track
# without sawing." consolidate_loops is what puts the two legs back together
# so the reversal is seen at all.
#
# Track 2's 'a' side reaches both neighbours 1 and 3 (a switch fan on one
# side) -- entering from 1 and leaving toward 3 is a same-side turnaround.
_REVERSAL_A_ADJ = {2: [1, 3]}
_REVERSAL_B_ADJ = {}


def test_reversal_on_a_run_boundary_is_invisible_to_each_leg_alone():
    full_path = [1, 2, 3]
    assert C.compute_reversals(full_path, _REVERSAL_A_ADJ, _REVERSAL_B_ADJ) == 1
    # Split at the reversal track itself: neither half sees it.
    assert C.compute_reversals([1, 2], _REVERSAL_A_ADJ, _REVERSAL_B_ADJ) == 0
    assert C.compute_reversals([2, 3], _REVERSAL_A_ADJ, _REVERSAL_B_ADJ) == 0


def test_consolidate_loops_recovers_a_reversal_lost_at_a_run_boundary():
    # Two separately-closed Move actions for the same run: 1 -> 2, then
    # 2 -> 3. Emitted this way (as _close_run would if the run got split for
    # bookkeeping reasons right at the reversal), each leg's own resource path
    # looks like ordinary travel -- neither shows a reversal.
    a1 = _action(0, "Move", 0, 60, location=1, resources=[2])
    a2 = _action(0, "Move", 61, 121, location=2, resources=[3])
    out = C.consolidate_loops(
        [a1, a2], _REVERSAL_A_ADJ, _REVERSAL_B_ADJ, set(), {}, {}, {}, set(), {}, {})
    kinds = [a["taskType"]["predefined"] for a in out]
    # Merging must not fold the reversal back into a single Move whose
    # resources happen to double back (invisible to compute_reversals, and
    # rejected outright under schemaVersion 2) -- it comes out as an explicit
    # Move/Reverse/Move, same as a reversal discovered within a single run.
    assert kinds == ["Move", "Reverse", "Move"], kinds
    assert out[0]["location"] == 1
    assert [r["id"] for r in out[0]["resources"]] == [2]
    assert out[1]["location"] == 2
    assert out[1]["resources"] == []
    assert out[2]["location"] == 2
    assert [r["id"] for r in out[2]["resources"]] == [3]


# --- Regression: an ordinary (non-reversal) boundary split inflates duration ---
#
# Splitting a continuous straight-through drive into two Move actions double-
# counts the shared boundary track in each leg's own track-crossing time, so
# their *summed* duration overshoots the correct, single-Move duration.
# Track 2 connects to 1 on its 'a' side and to 3 on its 'b' side here: an
# ordinary pass-through, not a reversal.
_STRAIGHT_A_ADJ = {2: [1]}
_STRAIGHT_B_ADJ = {2: [3]}


def test_consolidate_loops_avoids_double_counting_a_split_straight_run():
    leg1_dur = C.compute_move_duration([1, 2], _STRAIGHT_A_ADJ, _STRAIGHT_B_ADJ, {}, 0, None)
    leg2_dur = C.compute_move_duration([2, 3], _STRAIGHT_A_ADJ, _STRAIGHT_B_ADJ, {}, 0, None)
    naive_sum = leg1_dur + leg2_dur

    a1 = _action(0, "Move", 0, leg1_dur, location=1, resources=[2])
    a2 = _action(0, "Move", leg1_dur + 1, leg1_dur + 1 + leg2_dur, location=2, resources=[3])
    out = C.consolidate_loops(
        [a1, a2], _STRAIGHT_A_ADJ, _STRAIGHT_B_ADJ, set(), {}, {}, {}, set(), {}, {})
    moves = [a for a in out if a["taskType"]["predefined"] == "Move"]
    assert len(moves) == 1
    merged_dur = moves[0]["endTime"] - moves[0]["startTime"]

    correct_dur = C.compute_move_duration([1, 2, 3], _STRAIGHT_A_ADJ, _STRAIGHT_B_ADJ, {}, 0, None)
    assert merged_dur == correct_dur
    # The un-merged legs' own sum overshoots by exactly one double-counted
    # boundary track's crossing time.
    assert naive_sum == correct_dur + C.TRACK_CROSSING_TIME