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
        [a1, a2], {}, {}, set(), {}, {}, {}, set())
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
        [a1, a2, a3], {}, {}, set(), {}, {}, {}, set())
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
        [a1, a2], {}, {}, set(), {}, {}, {}, set())
    moves = [a for a in out if a["taskType"]["predefined"] == "Move"]
    assert moves == []


def test_move_separated_by_wait_is_not_merged():
    # A Move, then a Wait, then a final Move (the departure approach) must keep
    # both Moves: non-consecutive Moves are never merged.
    m1 = _action(0, "Move", 0, 100, location=906, resources=[59, 15])
    w = _action(0, "Wait", 100, 8850, location=906)
    m2 = _action(0, "Move", 8850, 9000, location=906, resources=[59, 15])
    out = C.consolidate_loops(
        [m1, w, m2], {}, {}, set(), {}, {}, {}, set())
    kinds = [a["taskType"]["predefined"] for a in out]
    assert kinds == ["Move", "Wait", "Move"]
    assert out[0] != out[2]