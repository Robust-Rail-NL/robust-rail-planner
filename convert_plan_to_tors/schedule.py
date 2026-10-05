"""Decide when a converted plan's actions happen, with a CP model.

convert_to_tors.py translates a PDDL plan action by action and times each one
from a clock it keeps per shunting unit. That clock knows nothing about the
other units, so two routes can be booked onto one trackPart at overlapping
times and TORS rejects the plan as "Track <x> is reserved" -- not because the
plan is wrong, but because nothing coordinated the two.

This replaces that accumulation with a constraint model over the whole plan:
every action's start is a variable, and the solver places them all at once
subject to

  * the order the converter gave each unit's actions, and their durations,
  * one unit at a time on any trackPart,
  * a unit only left standing where parking is allowed,
  * arrivals and departures on the instants the scenario fixes,

minimising the makespan. The actions themselves -- which they are, what they
reserve, how long they take -- are left exactly as the converter built them;
only the times change.

Structurally this follows LonyuNaz/tusp-pddl-post-processing, which schedules a
PDDL plan the same way in MiniZinc. CP-SAT is used instead so the image needs
no solver toolchain beyond a pip install.
"""

import collections

try:
    from ortools.sat.python import cp_model
except ImportError:  # pragma: no cover - exercised only in a broken install
    cp_model = None

# How long to let the solver look before taking the converter's own times. Hit
# only on plans far larger than a yard scenario produces; a 10-train plan solves
# in well under a second.
SOLVE_SECONDS = 20.0

# An Arrive, Exit or Reverse can be instantaneous. A zero-width interval
# excludes nothing from a resource, but TORS does hold the track for it, so give
# it the smallest footprint that still contends.
MIN_FOOTPRINT = 1

# The converter leaves a second between a unit's consecutive actions
# (su_clock = end + 1). Kept here so re-timing a plan does not quietly change
# that convention along with everything else.
GAP = 1

# Actions whose time the scenario dictates rather than the plan.
ARRIVAL_KINDS = ("Arrive", "StandIn")
DEPARTURE_KINDS = ("Exit", "StandOut")


def _kind(action):
    return (action.get("taskType") or {}).get("predefined")


def occupied(action):
    """Every trackPart the action holds: the track it is on, plus its route."""
    ids = [action["location"]] if action.get("location") is not None else []
    return ids + [r["id"] for r in (action.get("resources") or [])]


def ends_at(action):
    """Where the unit is left standing once the action finishes.

    An action's "location" is where it starts -- for a Move, its origin -- so
    the track it dwells on afterwards is the last trackPart of its route.
    """
    resources = action.get("resources") or []
    return resources[-1]["id"] if resources else action.get("location")


def _as_cm(value):
    """A length in whole centimetres: CP-SAT needs integers, metres are fractional."""
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return 0


def _unit_lengths(actions, scenario):
    """action index -> the length in centimetres of the unit performing it.

    Taken from the scenario's own trainUnitTypes, summed over the unit's
    members, so it is the same number TORS compares against a track's length.
    """
    by_type = {}
    for unit_type in scenario.get("trainUnitTypes") or []:
        by_type[(unit_type.get("typePrefix"), unit_type.get("carriages"))] = \
            _as_cm(unit_type.get("length"))
    per_member = {}
    for group in ("in", "inStanding"):
        for train in scenario.get(group) or []:
            for member in train.get("members") or []:
                per_member[member["id"]] = by_type.get(
                    (member.get("typePrefix"), member.get("carriages")), 0
                )
    lengths = {}
    for i, action in enumerate(actions):
        members = (action.get("shuntingUnit") or {}).get("memberIDs") or []
        lengths[i] = sum(per_member.get(m, 0) for m in members)
    return lengths


def _bookings(actions):
    """trackPart -> the indices of the actions that hold it."""
    booked = collections.defaultdict(set)
    for i, action in enumerate(actions):
        for track in occupied(action):
            booked[track].add(i)
    return booked


def conflicts(actions):
    """Moves by different units driving over one trackPart at the same time.

    Only Moves: two units standing on one track is allowed if they fit along
    it, so counting that as a conflict would overstate the problem. This is the
    thing TORS rejects as "Track <x> is reserved".
    """
    found = []
    driving = collections.defaultdict(list)
    for i, action in enumerate(actions):
        if _kind(action) != "Move":
            continue
        for track in occupied(action):
            driving[track].append(i)
    for track, indices in driving.items():
        for n, i in enumerate(indices):
            for j in indices[n + 1:]:
                a, b = actions[i], actions[j]
                if a["shuntingUnit"]["id"] == b["shuntingUnit"]["id"]:
                    continue
                if a["startTime"] < b["endTime"] and b["startTime"] < a["endTime"]:
                    found.append((track, i, j))
    return found


def _member_chains(actions):
    """member train -> its actions, in the order the converter emitted them.

    A train keeps its identity across Combine and Split even as the shunting
    unit around it changes, so this is what orders a combined unit's Move
    against the Combine that formed it.
    """
    chains = collections.defaultdict(list)
    for i, action in enumerate(actions):
        for member in (action.get("shuntingUnit") or {}).get("memberIDs") or []:
            chains[member].append(i)
    for indices in chains.values():
        indices.sort(key=lambda i: (actions[i]["startTime"], actions[i]["endTime"]))
    return chains


def _unit_chains(actions):
    """Each unit's actions, in the order the converter emitted them."""
    chains = collections.defaultdict(list)
    for i, action in enumerate(actions):
        chains[action["shuntingUnit"]["id"]].append(i)
    for indices in chains.values():
        indices.sort(key=lambda i: (actions[i]["startTime"], actions[i]["endTime"]))
    return chains


def assign_times(actions, scenario, location, keep_departures=True):
    """Re-time `actions` in place. Returns a dict describing what was done.

    Leaves the times untouched and reports `status` when there is no solver
    available, nothing to schedule, or no assignment satisfying the
    constraints -- the converter's own times are then still a faithful, if
    uncoordinated, rendering of the plan.
    """
    if cp_model is None:
        return {"status": "no-solver", "changed": False}
    if not actions:
        return {"status": "empty", "changed": False}

    no_parking = {tp["id"] for tp in location.get("trackParts", [])
                  if not tp.get("parkingAllowed")}
    arrivals = {int(t["arrival"]) for t in (scenario.get("in") or [])}
    departures = {int(t["departure"]) for t in (scenario.get("out") or [])}
    horizon = max(a["endTime"] for a in actions) * 4 + 1

    model = cp_model.CpModel()
    start, size = {}, {}
    for i, action in enumerate(actions):
        size[i] = action["endTime"] - action["startTime"]
        start[i] = model.NewIntVar(0, horizon, f"start{i}")

    # An arrival happens when the scenario says a train arrives; that instant is
    # not the plan's to move, and the same holds for a departure while the
    # deadline is being respected.
    pinned = 0
    for i, action in enumerate(actions):
        kind = _kind(action)
        if kind in ARRIVAL_KINDS and action["startTime"] in arrivals:
            model.Add(start[i] == action["startTime"]); pinned += 1
        elif kind in DEPARTURE_KINDS and keep_departures and action["startTime"] in departures:
            model.Add(start[i] == action["startTime"]); pinned += 1

    # A physical train does one thing at a time, in the order the converter gave
    # it. Chained on member trains rather than on shunting-unit ids, because a
    # Combine or Split changes which unit a train belongs to: keying on the unit
    # would leave a Move of the combined unit unordered against the Combine that
    # produced it.
    for indices in _member_chains(actions).values():
        for first, second in zip(indices, indices[1:]):
            model.Add(start[second] >= start[first] + size[first] + GAP)

    # A unit may only be left standing where parking is allowed: between two of
    # its own actions it sits on the track the first one ended on, so either that
    # track is parkable or the two follow on immediately.
    chains = _unit_chains(actions)
    for indices in chains.values():
        for first, second in zip(indices, indices[1:]):
            model.Add(start[second] >= start[first] + size[first] + GAP)
            if ends_at(actions[first]) in no_parking:
                model.Add(start[second] == start[first] + size[first] + GAP)

    # Driving over a track and standing on it are different kinds of use, and
    # TORS treats them differently. A train driving through needs the route to
    # itself, so a Move's tracks are exclusive. Standing is shared: two units
    # may sit on one track as long as they fit along it, which is the rule TORS
    # enforces as "Adding ShuntingUnit-N to Track X exceeds the maximum length".
    # Modelling standing as exclusive too would reject plans TORS accepts.
    capacity = {tp["id"]: _as_cm(tp.get("length")) for tp in location.get("trackParts", [])}
    lengths = _unit_lengths(actions, scenario)

    # Every use of a track, as (start, width, end, unit, driving?, length).
    uses = collections.defaultdict(list)

    def add_use(track, at, width, unit, is_driving, demand, name):
        if track is None:
            return
        end_var = model.NewIntVar(0, horizon, f"end_{name}")
        model.Add(end_var == at + width)
        uses[track].append({
            "start": at, "width": width, "end": end_var,
            "unit": unit, "driving": is_driving, "demand": demand, "name": name,
        })

    for i, action in enumerate(actions):
        unit = action["shuntingUnit"]["id"]
        demand = lengths.get(i, 0)
        width = max(size[i], MIN_FOOTPRINT)
        if _kind(action) == "Move":
            for track in occupied(action):
                add_use(track, start[i], width, unit, True, demand, f"drive{i}_{track}")
        else:
            add_use(action.get("location"), start[i], width, unit, False, demand, f"hold{i}")

    # Between two of a unit's own actions it stands on the track the first ended
    # on, for however long the gap turns out to be.
    for indices in chains.values():
        for first, second in zip(indices, indices[1:]):
            gap = model.NewIntVar(0, horizon, f"gapw{first}")
            model.Add(gap == start[second] - (start[first] + size[first]))
            add_use(ends_at(actions[first]), start[first] + size[first], gap,
                    actions[first]["shuntingUnit"]["id"], False,
                    lengths.get(first, 0), f"gap{first}")

    for track, entries in uses.items():
        limit = capacity.get(track, 0)
        # A unit driving over a track needs it to itself: nothing else of
        # another unit may be on it, standing or driving.
        for n, a in enumerate(entries):
            for b in entries[n + 1:]:
                if a["unit"] == b["unit"]:
                    continue
                if not (a["driving"] or b["driving"]):
                    continue
                a_first = model.NewBoolVar(f"ord_{track}_{a['name']}_{b['name']}")
                model.Add(a["end"] <= b["start"]).OnlyEnforceIf(a_first)
                model.Add(b["end"] <= a["start"]).OnlyEnforceIf(a_first.Not())
        # Standing together is allowed while the trains fit along the track.
        # A zero-length connector has no capacity to share, and is covered by
        # the driving rule above.
        if limit <= 0:
            continue
        held = [e for e in entries if not e["driving"] and e["demand"] > 0]
        if len(held) > 1:
            model.AddCumulative(
                [model.NewIntervalVar(e["start"], e["width"], e["end"], f"iv_{e['name']}")
                 for e in held],
                [e["demand"] for e in held],
                limit,
            )

    makespan = model.NewIntVar(0, horizon, "makespan")
    model.AddMaxEquality(makespan, [start[i] + size[i] for i in range(len(actions))])
    model.Minimize(makespan)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = SOLVE_SECONDS
    solver.parameters.num_search_workers = 4
    status = solver.Solve(model)
    name = solver.StatusName(status)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {"status": name, "changed": False, "pinned": pinned}

    before = len(conflicts(actions))
    for i, action in enumerate(actions):
        action["startTime"] = solver.Value(start[i])
        action["endTime"] = action["startTime"] + size[i]
    actions.sort(key=lambda a: (a["startTime"], a["endTime"]))
    return {
        "status": name,
        "changed": True,
        "pinned": pinned,
        "conflicts_before": before,
        "conflicts_after": len(conflicts(actions)),
        "makespan": solver.Value(makespan),
    }
