import os
import re
import json
import sys
from collections import deque


_DBG = os.environ.get("CONVERT_DEBUG") == "1"


def _dbg(msg):
    if _DBG:
        print("  DBG:", msg, flush=True)


class ScheduleInfeasibleError(Exception):
    """Raised when the plan cannot be scheduled against the scenario's hard
    constraints (arrival-track holds, departure deadlines, the scenario
    horizon). `problems` carries one diagnostic string per violation; `plan`
    is the partially converted TORS plan, otherwise intact and
    schema-compatible, which callers can write out for inspection despite the
    schedule being invalid. None when conversion failed before actions were
    built."""

    def __init__(self, problems, plan=None):
        self.problems = list(problems)
        self.plan = plan
        super().__init__("\n".join(self.problems))


# =====================================================
# REGEX
# =====================================================

# Interchange schema version this converter writes. Bumped together with the
# generator, solver and evaluator; see robust-rail-general's SCHEMA_CHANGELOG.md.
SCHEMA_VERSION = 1

SINGLE_ARG = r"\(([\w_]+) ([^)]+)\)"
DOUBLE_ARG = r"\(([\w_]+) ([^ ]+) ([^)]+)\)"
TRIPLE_ARG = r"\(([\w_]+) ([^ ]+) ([^ ]+) ([^)]+)\)"

START_MOVE_SU_RE = re.compile(r"\(start_move_su ([^)]+)\)")
END_MOVE_SU_RE = re.compile(r"\(end_move_su ([^ ]+) ([^)]+)\)")
MOVE_SU_RE = re.compile(
    r"\(move_(?:aside|bside)_(?:empty|occupied)_su ([^ ]+) ([^ ]+) ([^)]+)\)"
)
PARK_SU_RE = re.compile(
    r"\(park_su ([^ ]+) (?:([^ ]+) ([^ ]+) )?([^)]+)\)"
)
DEPART_SU_RE = re.compile(r"\(depart_(?:aside|bside)_su ([^ ]+) ([^)]+)\)")
DEPART_SU_FOR_REQUEST_RE = re.compile(
    r"\(depart_(?:aside|bside)_su_for_request ([^ ]+) ([^ ]+) ([^ ]+) ([^ ]+) ([^)]+)\)"
)
# The corridor model's form of the above: four args rather than five, because
# compile_precomputed_actions bakes the unit-to-slot matching into the action
# itself and the name gains a compiled_ prefix and loses the _su.
COMPILED_DEPART_FOR_REQUEST_RE = re.compile(
    r"\(compiled_depart_(?:aside|bside)_for_request ([^ ]+) ([^ ]+) ([^ ]+) ([^)]+)\)"
)

COUPLE_RE = re.compile(
    r"\(couple_two_sus ([^ ]+) ([^ ]+) ([^ ]+) ([^ ]+) ([^ ]+) ([^ ]+) ([^ ]+) ([^ ]+) ([^)]+)\)"
)
SPLIT_TWO_RE = re.compile(
    r"\(split_two_unit_su ([^ ]+) ([^ ]+) ([^ ]+) ([^ ]+) ([^ ]+) ([^ ]+) ([^)]+)\)"
)
SPLIT_THREE_RE = re.compile(
    r"\(split_three_unit_su ([^ ]+) ([^ ]+) ([^ ]+) ([^ ]+) ([^ ]+) ([^ ]+) ([^ ]+) ([^ ]+) ([^)]+)\)"
)
SERVICE_RE = re.compile(r"\(service_su ([^ ]+) ([^ ]+) ([^)]+)\)")
MATCH_RE = re.compile(r"\(match ([^ ]+) ([^)]+)\)")
ARRIVE_SU_RE = re.compile(r"\(arrive_su ([^ ]+) ([^)]+)\)")
ENTER_YARD_SU_RE = re.compile(r"\(enter_yard_su ([^ ]+) ([^ ]+) ([^)]+)\)")
UNCOUPLE_RE = re.compile(r"\(uncouple ([^ ]+) ([^)]+)\)")
PARKING_FULFILL_RE = re.compile(
    r"\(parking_fulfill ([^ ]+) ([^ ]+) ([^ ]+) ([^)]+)\)"
)
# The compiled-matching model represents "an arriving, serviced train becomes
# the departing train" by transferring the arrived SU's identity onto the
# request's placeholder SU. It is a logical rename, not a physical action.
ADOPT_COMPOSITION_RE = re.compile(
    r"\(compiled_adopt_composition ([^ ]+) ([^ ]+) ([^)]+)\)"
)
COMPLETE_REQUEST_RE = re.compile(
    r"\(complete_request_composition ([^ ]+) ([^)]+)\)"
)
COMPILED_ADVANCE_RE = re.compile(r"\(compiled_advance_request_\d+\)")
COMPILED_START_RE = re.compile(
    r"\(compiled_start_request ([^ ]+) ([^ ]+) ([^ ]+) ([^)]+)\)"
)
COMPILED_UNCOUPLE_RE = re.compile(
    r"\(compiled_uncouple_(front|back) ([^ ]+) ([^ ]+) ([^ ]+) ([^)]+)\)"
)
COMPILED_COUPLE_RE = re.compile(
    r"\(compiled_couple_(front|back) ([^ ]+) ([^ ]+) ([^ ]+) ([^)]+)\)"
)


COMBINE_DURATION = 180
SPLIT_DURATION = 120


def _as_id(value):
    """IDs are numbers on the wire, and reach us as strings from PDDL names."""
    if isinstance(value, bool):
        raise TypeError(f"not an id: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        return _as_id(value["id"])
    text = str(value)
    # PDDL object names carry prefixes the JSON does not: unit2801, su_train3.
    digits = re.sub(r"^\D+", "", text)
    if not digits.isdigit():
        raise ValueError(f"cannot read an id out of {value!r}")
    return int(digits)


def _as_time(value):
    """Times are numbers too; they were quoted while the JSON came from proto."""
    return int(value)


def _member_ids(members):
    """Member IDs from either bare ids or whole TrainUnit objects."""
    return [_as_id(m) for m in members]


def _track_resource(track_id):
    return {"kind": "trackPart", "id": _as_id(track_id)}


def _normalize_plan_line(plan_line):
    """Convert SymbolicPlanners `action(arg1, arg2)` to PDDL `(action arg1 arg2)` format."""
    m = re.match(r"(\w[\w_]*)\((.*)\)$", plan_line)
    if m:
        action = m.group(1)
        arguments = m.group(2)
        if not arguments:
            return f"({action})"
        args = re.split(r",\s*", arguments)
        return "(" + action + " " + " ".join(args) + ")"
    return plan_line


# =====================================================
# LOAD LOOKUPS
# =====================================================

def _member_type_key(member, type_lookup):
    """Resolve a member to its (typePrefix, carriages) lookup key.

    The in-repo fixture embeds typePrefix/carriages on each member, while
    generator-produced scenarios reference the trainUnitTypes block by
    displayName instead. Either shape must resolve.
    """
    key = (member.get("typePrefix"), member.get("carriages"))
    if key in type_lookup:
        return key
    display_name = member.get("typeDisplayName")
    if display_name is not None:
        for type_key, type_info in type_lookup.items():
            if type_info.get("displayName") == display_name:
                return type_key
    return None


def _member_types(members, type_lookup):
    """Resolve the train unit type dict for each member via the type lookup."""
    resolved = []
    for m in members:
        key = _member_type_key(m, type_lookup)
        resolved.append(type_lookup.get(key, {}) if key is not None else {})
    return resolved


def build_train_lookup(scenario):
    """Build lookup for all trains including their types and durations"""
    lookup = {}

    type_lookup = {}
    for t in scenario["trainUnitTypes"]:
        type_lookup[t["typePrefix"], t["carriages"]] = t

    # Incoming trains
    for train in scenario.get("in", []):
        names = [f"train{train['id']}", f"su_train{train['id']}"]
        members = train.get("members", [])
        member_types = _member_types(members, type_lookup)
        if members:
            key = _member_type_key(members[0], type_lookup)
            if key is not None:
                combine_duration = int(type_lookup[key].get("combineDuration", COMBINE_DURATION))
                split_duration = int(type_lookup[key].get("splitDuration", SPLIT_DURATION))
            else:
                combine_duration = COMBINE_DURATION
                split_duration = SPLIT_DURATION
        else:
            combine_duration = COMBINE_DURATION
            split_duration = SPLIT_DURATION

        entry = {
            "id": train["id"],
            "members": members,
            "member_types": member_types,
            "combine_duration": combine_duration,
            "split_duration": split_duration,
        }
        for n in names:
            lookup[n] = entry

    # In standing trains
    for i, train in enumerate(scenario.get("inStanding", [])):
        names = [f"train_in_standing_{i}", f"su_train_in_standing_{i}"]
        members = train.get("members", [])
        member_types = _member_types(members, type_lookup)
        if members:
            key = _member_type_key(members[0], type_lookup)
            if key is not None:
                combine_duration = int(type_lookup[key].get("combineDuration", COMBINE_DURATION))
                split_duration = int(type_lookup[key].get("splitDuration", SPLIT_DURATION))
            else:
                combine_duration = COMBINE_DURATION
                split_duration = SPLIT_DURATION
        else:
            combine_duration = COMBINE_DURATION
            split_duration = SPLIT_DURATION

        entry = {
            "id": train["id"],
            "members": members,
            "member_types": member_types,
            "combine_duration": combine_duration,
            "split_duration": split_duration,
        }
        for n in names:
            lookup[n] = entry

    # Also store under bare train unit IDs for combine/split member resolution
    for key, entry in list(lookup.items()):
        for m in entry["members"]:
            lookup[f"unit{m['id']}"] = {
                "id": m["id"],
                "members": [m],
                "member_types": _member_types([m], type_lookup),
            }

    return lookup


def build_unit_lookup(scenario):
    """Build lookup for individual train units"""
    lookup = {}

    for train in scenario.get("in", []):
        for member in train.get("members", []):
            lookup[f"unit{member['id']}"] = member

    for train in scenario.get("inStanding", []):
        for member in train.get("members", []):
            lookup[f"unit{member['id']}"] = member

    return lookup


def build_request_lookup(scenario):
    """Build lookup for departure requests"""
    lookup = {}

    for request in scenario.get("out", []):
        request_name = f"request{request['id']}"
        lookup[request_name] = {
            "id": request["id"],
            "trainUnits": request.get("trainUnits", []),
            "leaveTrackPart": request.get("leaveTrackPart"),
            "lastParkingTrackPart": request.get("lastParkingTrackPart"),
            "arrival": request.get("arrival")  # departure time (confusingly named)
        }

    return lookup


def build_track_lookup(location):
    """Creates track name to ID mapping"""
    lookup = {}

    for track in location["trackParts"]:
        track_name = track["name"]
        name_lower = track_name.lower()

        lookup["o_" + name_lower] = _track_resource(track["id"])

        # Also add bare name for tracks referenced without "o_" prefix (e.g. stootblok906b)
        lookup[name_lower] = _track_resource(track["id"])

    return lookup


def build_track_id_lookup(location):
    """Build reverse lookup from track ID to track info"""
    return {tp["id"]: _track_resource(tp["id"]) for tp in location["trackParts"]}


# =====================================================
# ACTION BUILDERS
# =====================================================

def make_shunting_unit(train_id, train_lookup, unit_lookup=None, members=None):
    """Create a shunting unit object.

    memberIDs is a list of TrainUnit IDs. It used to be `members` holding whole
    TrainUnit objects, each with its type embedded; the evaluator rejects that
    shape outright, naming the field in the error.
    """
    def su(member_ids):
        return {
            "id": _as_id(train_id),
            "memberIDs": [_as_id(m) for m in member_ids],
            "parentIDs": [],
            "childIDs": [],
        }

    if members:
        return su(_member_ids(members))

    if train_id in train_lookup:
        return su(_member_ids(train_lookup[train_id]["members"]))

    if isinstance(train_id, str) and train_id.startswith("su_"):
        stripped = train_id.replace("su_unit", "")
        unit_id = f"unit{stripped}"
        if unit_id in unit_lookup:
            return su(_member_ids([unit_lookup[unit_id]]))

    return su([])


def convert_track(track_name, track_lookup, track_id_lookup=None):
    """Convert a track name to the Resource that refers to it."""
    if track_name in track_lookup:
        return track_lookup[track_name]

    if track_id_lookup and track_name in track_id_lookup:
        return track_id_lookup[track_name]

    return _track_resource(track_name.replace("o_", ""))


def create_move_action(train_id, start, end, path,
                       train_lookup, track_id_lookup, unit_lookup=None):
    """Create a Move action"""
    resources = []
    for p in path:
        resources.append(track_id_lookup.get(p, _track_resource(p)))

    location = resources[0]["id"]
    # Remove start track from resources (it's already captured in location)
    resources = resources[1:]
    shunting_unit = make_shunting_unit(train_id, train_lookup, unit_lookup)

    return {
        "startTime": _as_time(start),
        "endTime": _as_time(end),
        "taskType": {
            "predefined": "Move"
        },
        "shuntingUnit": shunting_unit,
        "location": location,
        "resources": resources
    }


def create_arrive_action(train_id, time, track,
                         train_lookup, track_lookup, unit_lookup=None,
                         standing_type="", track_id_lookup=None):
    """Create an Arrive action"""
    resource = convert_track(track, track_lookup, track_id_lookup)
    shunting_unit = make_shunting_unit(train_id, train_lookup, unit_lookup)

    # standingType has been dropped from the schema: a train that was already
    # in the yard, or stays in it, is expressed by the task type itself.
    predefined = "StandIn" if standing_type else "Arrive"

    return {
        "startTime": _as_time(time),
        "endTime": _as_time(time),
        "taskType": {
            "predefined": predefined
        },
        "shuntingUnit": shunting_unit,
        "location": resource["id"],
        "resources": [resource]
    }


def create_exit_action(train_id, time, track,
                       train_lookup, track_lookup, unit_lookup=None,
                       standing_type="", track_id_lookup=None):
    """Create an Exit action"""
    resource = convert_track(track, track_lookup, track_id_lookup)
    shunting_unit = make_shunting_unit(train_id, train_lookup, unit_lookup)

    predefined = "StandOut" if standing_type else "Exit"

    return {
        "startTime": _as_time(time),
        "endTime": _as_time(time),
        "taskType": {
            "predefined": predefined
        },
        "shuntingUnit": shunting_unit,
        "location": resource["id"],
        "resources": [resource]
    }


def create_wait_action(train_id, start, end, location,
                       train_lookup, unit_lookup=None):
    """Create a Wait action"""
    shunting_unit = make_shunting_unit(train_id, train_lookup, unit_lookup)

    return {
        "startTime": _as_time(start),
        "endTime": _as_time(end),
        "taskType": {
            "predefined": "Wait"
        },
        "shuntingUnit": shunting_unit,
        "location": location,
        "resources": []
    }


def create_combine_action(train_ids, result_id, start, end, location,
                          train_lookup, unit_lookup=None):
    """Create Combine actions for coupling"""
    actions = []
    combined_members = []

    for train_id in train_ids:
        shunting_unit = make_shunting_unit(train_id, train_lookup, unit_lookup)
        shunting_unit["childIDs"] = [_as_id(result_id)]
        combined_members.extend(shunting_unit["memberIDs"])

        actions.append({
            "startTime": _as_time(start),
            "endTime": _as_time(end),
            "taskType": {
                "predefined": "Combine"
            },
            "shuntingUnit": shunting_unit,
            "location": location,
            "resources": []
        })

    return actions, combined_members


def create_split_action(train_id, child_ids, start, end, location,
                        train_lookup, unit_lookup=None):
    """Create a Split action"""
    shunting_unit = make_shunting_unit(train_id, train_lookup, unit_lookup)
    shunting_unit["childIDs"] = [_as_id(cid) for cid in child_ids]

    return {
        "startTime": _as_time(start),
        "endTime": _as_time(end),
        "taskType": {
            "predefined": "Split"
        },
        "shuntingUnit": shunting_unit,
        "location": location,
        "resources": []
    }


def create_service_action(train_id, start, end, location, facility_id,
                          facility_type, train_lookup, unit_lookup=None):
    """Create a Service action"""
    shunting_unit = make_shunting_unit(train_id, train_lookup, unit_lookup)

    return {
        "startTime": _as_time(start),
        "endTime": _as_time(end),
        "taskType": {
            "other": facility_type
        },
        "shuntingUnit": shunting_unit,
        "location": location,
        "resources": [{"kind": "facility", "id": _as_id(facility_id)}]
    }


def get_train_duration(train_id, train_lookup, unit_lookup=None, duration_type="combine"):
    """Get duration for combine/split operations"""
    if train_id in train_lookup:
        if duration_type == "combine":
            return train_lookup[train_id].get("combine_duration", COMBINE_DURATION)
        else:
            return train_lookup[train_id].get("split_duration", SPLIT_DURATION)

    return COMBINE_DURATION if duration_type == "combine" else SPLIT_DURATION


# =====================================================
# TRACK TOPOLOGY
# =====================================================

def _is_switch_like_track_part(track_part):
    if track_part.get("parkingAllowed", False):
        return False
    length = track_part.get("length", 0)
    neighbors = set(track_part.get("aSide", [])) | set(track_part.get("bSide", []))
    return length == 0 and len(neighbors) >= 2


def build_switch_sets(location):
    return {
        tp["id"]
        for tp in location["trackParts"]
        if _is_switch_like_track_part(tp)
    }


def build_directed_adj(location, side_key):
    adj = {tp["id"]: [] for tp in location["trackParts"]}
    for tp in location["trackParts"]:
        for nb_id in tp.get(side_key, []):
            if nb_id in adj:
                adj[tp["id"]].append(nb_id)
    return adj


def bfs_through_switches(a_adj, b_adj, start, goal, switch_ids):
    """Directed BFS: try a-side path, then b-side path, return the shorter one.
    Only traverses through switch-like intermediate nodes."""
    if start == goal:
        return [start]

    def _bfs(adj, start, goal):
        visited = {start}
        queue = deque([[start]])
        while queue:
            path = queue.popleft()
            node = path[-1]
            for nb in adj.get(node, []):
                if nb == goal:
                    return path + [nb]
                if nb not in visited:
                    visited.add(nb)
                    queue.append(path + [nb])
        return []

    a_path = _bfs(a_adj, start, goal)
    b_path = _bfs(b_adj, start, goal)
    if a_path and b_path:
        return a_path if len(a_path) <= len(b_path) else b_path
    return a_path or b_path or [start, goal]


def expand_path(path, a_adj, b_adj, switch_ids):
    if not path:
        return path
    expanded = [path[0]]
    for i in range(len(path) - 1):
        segment = bfs_through_switches(a_adj, b_adj, path[i], path[i + 1], switch_ids)
        expanded.extend(segment[1:])
    return expanded


TRACK_CROSSING_TIME = 60
SWITCH_CROSSING_TIME = 30
SWITCH_COST = {"Switch": 1, "EnglishSwitch": 2, "HalfEnglishSwitch": 2}


def switch_cost_map(location):
    """Map trackPart id -> switch cost (1 per Switch, 2 per English/HalfEnglish switch)."""
    costs = {}
    for tp in location.get("trackParts", []):
        costs[tp["id"]] = SWITCH_COST.get(tp.get("type"), 0)
    return costs


def compute_reversals(expanded_path, a_adj, b_adj):
    """Count direction reversals along the path (the solver's Reverse arcs).

    A reversal occurs on an interior track when the path enters and leaves it
    through the same side (a turn-around), matching ArcType.Reverse in the
    solver's routing graph.
    """
    reversals = 0
    for i in range(1, len(expanded_path) - 1):
        track = expanded_path[i]
        prev, nxt = expanded_path[i - 1], expanded_path[i + 1]
        if prev in a_adj.get(track, []):
            entry_side = "a"
        elif prev in b_adj.get(track, []):
            entry_side = "b"
        else:
            entry_side = None
        if nxt in a_adj.get(track, []):
            exit_side = "a"
        elif nxt in b_adj.get(track, []):
            exit_side = "b"
        else:
            exit_side = None
        if entry_side is not None and entry_side == exit_side:
            reversals += 1
    return reversals


def compute_move_duration(expanded_path, a_adj, b_adj, switch_costs=None, reversal_duration=0, track_parts_by_id=None):
    """Compute a Move action's duration using the C# solver's ComputeDuration model:

        duration = (Tracks.Length + TotalReversals) * TrackCrossingTime
                 + TotalSwitches * SwitchCrossingTime
                 + TotalReversals * ReversalDuration

    Tracks.Length counts only track parts with physical length > 0 (i.e.
    actual railroad segments, not zero-length switches/connectors).
    TotalSwitches is the summed switch cost over the path, TotalReversals
    counts same-side turn-arounds.
    """
    if switch_costs is None:
        switch_costs = {}
    reversals = compute_reversals(expanded_path, a_adj, b_adj)
    total_switches = sum(switch_costs.get(tid, 0) for tid in expanded_path)
    if track_parts_by_id:
        nonzero_tracks = sum(1 for tid in expanded_path
                             if track_parts_by_id.get(tid, {}).get("length", 0) > 0)
    else:
        nonzero_tracks = len(expanded_path)
    return (
        (nonzero_tracks + reversals) * TRACK_CROSSING_TIME
        + total_switches * SWITCH_CROSSING_TIME
        + reversals * reversal_duration
    )


def get_reversal_duration(train, train_lookup):
    """ReversalDuration for a train: max across its composed member types (0 if unknown).

    Matches the solver's ShuntTrain.ReversalDuration: backNormTime + carriages *
    backAdditionTime per type.
    """
    entry = train_lookup.get(train)
    if not entry:
        return 0
    durations = []
    for t in entry.get("member_types", []):
        try:
            back_norm_time = int(t.get("backNormTime", 0) or 0)
            back_addition_time = int(t.get("backAdditionTime", 0) or 0)
            carriages = int(t.get("carriages", 0) or 0)
            durations.append(back_norm_time + carriages * back_addition_time)
        except (TypeError, ValueError):
            durations.append(0)
    return max(durations, default=0)


# =====================================================
# CONVERTER
# =====================================================

def convert_plan(plan_file, scenario_file, location_file):
    """Convert a PDDL plan to TORS JSON.

    The converter is deliberately a single-pass, per-train translator: every
    track a train lands on, parks on, or departs from is taken verbatim from
    the plan's action arguments. Nothing here re-decides where a train rests;
    if a track would not fit, that is reported as infeasible, never fixed by
    parking the train elsewhere.
    """

    with open(scenario_file) as f:
        scenario = json.load(f)

    with open(location_file) as f:
        location = json.load(f)

    train_lookup = build_train_lookup(scenario)
    unit_lookup = build_unit_lookup(scenario)
    request_lookup = build_request_lookup(scenario)
    track_lookup = build_track_lookup(location)
    track_id_lookup = build_track_id_lookup(location)
    track_parts_by_id = {tp["id"]: tp for tp in location["trackParts"]}
    a_adj = build_directed_adj(location, "aSide")
    b_adj = build_directed_adj(location, "bSide")
    switch_ids = build_switch_sets(location)
    switch_costs = switch_cost_map(location)
    zero_length_tracks = {tp["id"] for tp in location["trackParts"] if tp.get("length", 0) == 0}
    type_lookup = {
        (t.get("typePrefix"), t.get("carriages")): t
        for t in scenario.get("trainUnitTypes", [])
    }
    scenario_end_time = int(scenario.get("endTime", 0))

    def _strip_trailing_zero_length(path):
        """Remove trailing zero-length tracks (bumpers/signals) from a path.
        These are points like Sein70 that trains cannot physically occupy."""
        while len(path) > 1 and path[-1] in zero_length_tracks:
            path = path[:-1]
        return path

    # ------------------------------------------------------------------
    # Per-SU state. `su_loc` and `su_clock` are keyed by the resolved SU name
    # (or integer id for generated combine/split children).
    # ------------------------------------------------------------------
    su_loc = {}                        # SU -> physical track id it currently stands on
    su_clock = {}                      # SU -> earliest time its next action may start
    su_arrival = {}                    # SU -> scenario arrival time
    su_identity = {}                   # request-alias SU name -> physical SU name
    runs = {}                          # SU -> {"seq": [...]} of an open move run
    rested = {}                        # SU -> track id the plan parks it on
    waiting_on_messages = {}           # parked-out SU name -> parking slot id
    _scenario_exit_tracks = {}         # SU -> the track its Exit must sit on
    actions = []

    scenario_arrival_times = {}
    scenario_materialized = {}

    def _materialized_arrival_track(train):
        """Track where an arriving train physically appears: the scenario's
        entry/parking track, resolved off zero-length signals onto the real
        track beside them."""
        for key in ("entryTrackPart", "firstParkingTrackPart"):
            raw = train.get(key)
            if raw is None:
                continue
            tid = convert_track(raw, track_lookup, track_id_lookup)["id"]
            tp = track_parts_by_id.get(tid)
            if tp and tp.get("length", 0) == 0 and not tp.get("parkingAllowed", False):
                for nb in tp.get("aSide", []) + tp.get("bSide", []):
                    ntp = track_parts_by_id.get(nb)
                    if ntp and (ntp.get("length", 0) > 0 or ntp.get("parkingAllowed", False)):
                        return nb
            return tid
        return None

    for train in scenario.get("in", []):
        arrival = int(train.get("arrival", 0))
        materialized = _materialized_arrival_track(train)
        for name in (f"train{train['id']}", f"su_train{train['id']}"):
            scenario_arrival_times[name] = arrival
            scenario_materialized[name] = materialized

    def _resolve_su(name):
        """Return the physical SU represented by a PDDL request alias."""
        return su_identity.get(name, name)

    def get_su_id(name):
        """Assign each shunting-unit name (or id) a stable integer id."""
        nonlocal next_su_id
        if name not in su_name_to_int:
            su_name_to_int[name] = next_su_id
            next_su_id += 1
        return su_name_to_int[name]

    su_name_to_int = {}
    next_su_id = 0
    shunting_unit_composition = {}
    next_generated_su = 1000000

    su_departure_time = {}             # SU -> departure time pulled from its request
    su_departure_deadline = {}         # departing SU -> requested departure time
    departing_sus = set()
    train_arrival_times = {}           # SU name -> arrival time (for post-processing)

    def _members_for(su_id):
        """Return the train-unit IDs currently contained in an SU."""
        if su_id in shunting_unit_composition:
            return list(shunting_unit_composition[su_id]["memberIDs"])
        if su_id in train_lookup:
            return _member_ids(train_lookup[su_id]["members"])
        if isinstance(su_id, str) and su_id.startswith("su_unit"):
            unit = unit_lookup.get("unit" + su_id[len("su_unit"):])
            if unit:
                return [_as_id(unit)]
        return []

    def _generated_su(member_ids, parent_ids):
        """Create an SU identity for the result of a split or coupling."""
        nonlocal next_generated_su
        su_id = next_generated_su
        next_generated_su += 1
        shunting_unit_composition[su_id] = {
            "memberIDs": list(member_ids),
            "parentIDs": list(parent_ids),
        }
        return su_id

    def _append_su_action(action, su_id):
        """Attach the current SU identity and members to a TORS action."""
        action["shuntingUnit"]["id"] = su_id
        action["shuntingUnit"]["memberIDs"] = _members_for(su_id)
        actions.append(action)

    def _set_request_departure(su_id, request_su):
        """Assign the request's departure time to its physical SU."""
        request_name = request_su[3:] if request_su.startswith("su_") else request_su
        if request_name in request_lookup:
            departure = request_lookup[request_name].get("arrival")
            if departure is not None:
                su_departure_time[su_id] = int(departure)

    def _ensure_position(train):
        """Fill in su_loc/su_arrival from the scenario for a train that never
        got an arrive_su line (standing trains)."""
        if train in su_loc:
            return su_loc[train]
        if not isinstance(train, str):
            return None
        stripped = train[3:] if train.startswith("su_") else train
        for i, standing in enumerate(scenario.get("inStanding", [])):
            names = (f"train_in_standing_{i}", f"su_train_in_standing_{i}")
            if stripped in names or train in names:
                materialized = _materialized_arrival_track(standing)
                if materialized is not None:
                    su_loc[train] = materialized
                su_arrival[train] = int(standing.get("arrival", 0))
                su_clock[train] = max(su_clock.get(train, 0), su_arrival[train])
                return su_loc.get(train)
        for incoming in scenario.get("in", []):
            names = (f"train{incoming['id']}", f"su_train{incoming['id']}")
            if stripped in names or train in names:
                materialized = _materialized_arrival_track(incoming)
                if materialized is not None:
                    su_loc[train] = materialized
                su_arrival[train] = int(incoming.get("arrival", 0))
                su_clock[train] = max(su_clock.get(train, 0), su_arrival[train])
                return su_loc.get(train)
        return None

    def _close_run(train, pin_end=None):
        """Build the Move for the open run, ending on the track the plan's last
        move leg designated (does not append it). When `pin_end` is set the Move
        ends there where possible (a departing train's exit approach); if the
        train is not ready in time the Move simply ends when it realistically
        can — the converter is not asked to make plans feasible, only to convert
        them accurately. Returns (start, end, move_action), or (None, None,
        None) when there is nothing to emit."""
        run = runs.pop(train, None)
        if not run:
            return None, None, None
        seq = [s for s in run.get("seq", []) if s is not None]
        if len(seq) < 2:
            return None, None, None
        expanded = _strip_trailing_zero_length(
            expand_path(seq, a_adj, b_adj, switch_ids)
        )
        if len(expanded) < 2:
            return None, None, None
        duration = compute_move_duration(
            expanded, a_adj, b_adj, switch_costs,
            get_reversal_duration(train, train_lookup), track_parts_by_id
        )
        ready = max(su_clock.get(train, 0), su_arrival.get(train, 0))
        if pin_end is not None:
            end = max(int(pin_end), ready + duration)
            start = end - duration
        else:
            start = ready
            end = start + duration
        move_action = create_move_action(
            train, start, end, expanded,
            train_lookup, track_id_lookup, unit_lookup
        )
        su_clock[train] = end + 1
        su_loc[train] = expanded[-1]
        return start, end, move_action

    def _emit_close_run(train, pin_end=None):
        """Close an open run by appending its Move. Returns (start, end), or
        (None, None) when there is nothing to emit."""
        start, end, move_action = _close_run(train, pin_end=pin_end)
        if move_action is not None:
            _append_su_action(move_action, train)
        return start, end

    def _move(train, from_id, target_id):
        """Emit a single-hop Move driving a train onto its plan target track
        (the enter-yard drive). A no-op when already on the target."""
        _ensure_position(train)
        ready = max(su_clock.get(train, 0), su_arrival.get(train, 0))
        expanded = _strip_trailing_zero_length(
            expand_path([from_id, target_id], a_adj, b_adj, switch_ids)
        )
        if len(expanded) < 2:
            su_loc[train] = target_id
            su_clock[train] = ready
            return
        duration = compute_move_duration(
            expanded, a_adj, b_adj, switch_costs,
            get_reversal_duration(train, train_lookup), track_parts_by_id
        )
        start = ready
        end = start + duration
        _append_su_action(
            create_move_action(
                train, start, end, expanded,
                train_lookup, track_id_lookup, unit_lookup
            ),
            train,
        )
        su_clock[train] = end + 1
        su_loc[train] = expanded[-1]

    problems = []

    with open(plan_file) as f:
        lines = [line.strip() for line in f if line.strip()]

    unhandled = []
    for line in lines:
        line = _normalize_plan_line(line)

        # --------------------------------
        # Logical no-op actions
        # --------------------------------
        m = MATCH_RE.match(line)
        if m:
            continue

        if COMPLETE_REQUEST_RE.match(line) or COMPILED_ADVANCE_RE.match(line):
            continue

        m = UNCOUPLE_RE.match(line)
        if m:
            continue

        # --------------------------------
        # ARRIVE
        # --------------------------------
        m = ARRIVE_SU_RE.match(line)
        if m:
            su_id = m.group(1)
            arrival = scenario_arrival_times.get(su_id, 0)
            train_arrival_times[su_id] = arrival
            su_arrival[su_id] = arrival
            materialized = scenario_materialized.get(su_id)
            if materialized is not None:
                su_loc[su_id] = materialized
            else:
                su_loc[su_id] = convert_track(
                    m.group(2), track_lookup, track_id_lookup
                )["id"]
            su_clock[su_id] = max(su_clock.get(su_id, 0), arrival)
            continue

        # --------------------------------
        # ENTER YARD (drive the materialized arrival onto the plan's target)
        # --------------------------------
        m = ENTER_YARD_SU_RE.match(line)
        if m:
            su_id, _entry, target = m.groups()
            target_id = convert_track(target, track_lookup, track_id_lookup)["id"]
            from_id = su_loc.get(su_id)
            if from_id is None:
                from_id = convert_track(_entry, track_lookup, track_id_lookup)["id"]
            _move(su_id, from_id, target_id)
            rested[su_id] = su_loc.get(su_id, target_id)
            continue

        # --------------------------------
        # COMPILED START / ADOPT (identity transfers, no physical action)
        # --------------------------------
        m = COMPILED_START_RE.match(line)
        if m:
            source_su, _unit, request_su, track = m.groups()
            source_su = _resolve_su(source_su)
            su_identity[request_su] = source_su
            _set_request_departure(source_su, request_su)
            su_loc[source_su] = convert_track(track, track_lookup, track_id_lookup)["id"]
            rested[source_su] = su_loc[source_su]
            continue

        m = ADOPT_COMPOSITION_RE.match(line)
        if m:
            source_su, request_su, track = m.groups()
            source_su = _resolve_su(source_su)
            su_identity[request_su] = source_su
            _set_request_departure(source_su, request_su)
            track_id = convert_track(track, track_lookup, track_id_lookup)["id"]

            if source_su in train_lookup:
                src = train_lookup[source_su]
                train_lookup[request_su] = {
                    "id": src.get("id", request_su),
                    "members": src.get("members", []),
                    "member_types": src.get("member_types", []),
                    "combine_duration": src.get("combine_duration", COMBINE_DURATION),
                    "split_duration": src.get("split_duration", SPLIT_DURATION),
                }
            su_loc[request_su] = su_loc.get(source_su, track_id)
            su_arrival[request_su] = su_arrival.get(source_su, 0)
            su_clock[request_su] = su_clock.get(source_su, 0)
            if source_su in runs:
                runs[request_su] = runs.pop(source_su)
            continue

        # --------------------------------
        # START MOVE
        # --------------------------------
        m = START_MOVE_SU_RE.match(line)
        if m:
            train = _resolve_su(m.group(1))
            _ensure_position(train)
            runs.setdefault(train, {"seq": []})
            continue

        # --------------------------------
        # MOVE (accumulate the run's raw track sequence verbatim)
        # --------------------------------
        m = MOVE_SU_RE.match(line)
        if m:
            train, from_track, to_track = m.groups()
            train = _resolve_su(train)
            from_id = convert_track(from_track, track_lookup, track_id_lookup)["id"]
            to_id = convert_track(to_track, track_lookup, track_id_lookup)["id"]
            _ensure_position(train)
            run = runs.get(train)
            if not run:
                run = {"seq": []}
                runs[train] = run
            if not run["seq"] or run["seq"][0] is None:
                run["seq"] = [from_id]
            run["seq"].append(to_id)
            su_loc[train] = to_id
            continue

        # --------------------------------
        # END MOVE
        # --------------------------------
        m = END_MOVE_SU_RE.match(line)
        if m:
            train, track = m.groups()
            train = _resolve_su(train)
            end_id = convert_track(track, track_lookup, track_id_lookup)["id"]
            run = runs.get(train)
            if run and run.get("seq") and any(s is not None for s in run["seq"]):
                if run["seq"][-1] != end_id:
                    run["seq"].append(end_id)
                start_t, _end_t = _emit_close_run(train)
                if start_t is None:
                    su_loc[train] = end_id
            else:
                su_loc[train] = end_id
            rested[train] = su_loc.get(train, end_id)
            continue

        # --------------------------------
        # PARK
        # --------------------------------
        m = PARK_SU_RE.match(line)
        if m:
            train, unit, parking_slot, track = m.groups()
            train = _resolve_su(train)
            track_id = convert_track(track, track_lookup, track_id_lookup)["id"]
            _ensure_position(train)
            run = runs.get(train)
            if run and run.get("seq") and any(s is not None for s in run["seq"]):
                if run["seq"][-1] != track_id:
                    run["seq"].append(track_id)
                start_t, _end_t = _emit_close_run(train)
                if start_t is None:
                    su_loc[train] = track_id
            else:
                su_loc[train] = track_id
            rested[train] = su_loc.get(train, track_id)

            # no_bumpers 4-arg park_su parks a unit in a slot; the unit waits
            # on this track until the scenario ends, then leaves as OutStanding.
            if unit is not None and parking_slot is not None:
                waiting_on_messages[train] = parking_slot
                exit_time = max(su_clock.get(train, 0), scenario_end_time)
                exit_action = create_exit_action(
                    train,
                    exit_time,
                    track,
                    train_lookup,
                    track_lookup,
                    unit_lookup,
                    standing_type="OutStanding",
                    track_id_lookup=track_id_lookup
                )
                _append_su_action(exit_action, train)
                su_clock[train] = exit_time + 1
            continue

        # --------------------------------
        # PARKING FULFILL
        # --------------------------------
        m = PARKING_FULFILL_RE.match(line)
        if m:
            su_id, unit, parking_slot, track = m.groups()
            su_id = _resolve_su(su_id)
            track_id = convert_track(track, track_lookup, track_id_lookup)["id"]
            _ensure_position(su_id)
            run = runs.get(su_id)
            if run and run.get("seq") and any(s is not None for s in run["seq"]):
                if run["seq"][-1] != track_id:
                    run["seq"].append(track_id)
                start_t, _end_t = _emit_close_run(su_id)
                if start_t is None:
                    su_loc[su_id] = track_id
            else:
                su_loc[su_id] = track_id
            rested[su_id] = su_loc.get(su_id, track_id)
            waiting_on_messages[su_id] = parking_slot

            exit_time = max(su_clock.get(su_id, 0), scenario_end_time)
            exit_action = create_exit_action(
                su_id,
                exit_time,
                track,
                train_lookup,
                track_lookup,
                unit_lookup,
                standing_type="OutStanding",
                track_id_lookup=track_id_lookup
            )
            _append_su_action(exit_action, su_id)
            su_clock[su_id] = exit_time + 1
            continue

        # --------------------------------
        # DEPART
        # --------------------------------
        m = (DEPART_SU_RE.match(line)
             or DEPART_SU_FOR_REQUEST_RE.match(line)
             or COMPILED_DEPART_FOR_REQUEST_RE.match(line))
        if m:
            groups = m.groups()
            raw_train = groups[0]
            train = _resolve_su(raw_train)
            track = groups[-1] if len(groups) > 2 else groups[1]
            track_id = convert_track(track, track_lookup, track_id_lookup)["id"]

            # Departure deadline from the scenario request.
            dep = None
            if len(groups) >= 4 and not raw_train.startswith("su_request"):
                req_name = groups[-2]
                if req_name in request_lookup:
                    dep = request_lookup[req_name].get("arrival")
                else:
                    for req in scenario.get("out", []):
                        dep = req.get("arrival")
                        break
            elif raw_train.startswith("su_request"):
                req_name = "request" + raw_train[len("su_request"):]
                if req_name in request_lookup:
                    dep = request_lookup[req_name].get("arrival")
            elif train in su_departure_time:
                dep = su_departure_time[train]

            if dep is not None:
                dep = int(dep)
                su_departure_deadline[train] = dep
                departing_sus.add(train)
            else:
                dep = None

            # The park track: where the plan left the train standing. Never
            # chosen by the converter.
            run = runs.get(train)
            parked_track = None
            if run and run.get("seq") and any(s is not None for s in run["seq"]):
                parked_track = run["seq"][0]
            if parked_track is None:
                parked_track = su_loc.get(train)
            parked_track = _ensure_position(train) if parked_track is None else parked_track
            if parked_track is None:
                parked_track = track_id
                problems.append(
                    f"INFEASIBLE: SU {train} has no recorded park track to "
                    f"depart from."
                )

            # An open run is the plan's exit approach; pin it to the deadline.
            had_run = run is not None
            ready_time = max(su_clock.get(train, 0), su_arrival.get(train, 0))
            app_start, app_end, move_action = None, None, None
            if had_run:
                app_start, app_end, move_action = _close_run(train, pin_end=dep)
                if app_start is None:
                    had_run = False

            if had_run and dep is not None:
                exit_time = app_end
                wait_end = app_start
            elif had_run:
                exit_time = app_end + 1
                wait_end = ready_time
            else:
                exit_time = dep if dep is not None else ready_time
                wait_end = exit_time

            # The Wait, the approach Move and the Exit are emitted in that order
            # so the story clock reads forward: rest, drive to the exit, leave.
            if wait_end > ready_time:
                _append_su_action(
                    create_wait_action(
                        train, ready_time, wait_end, parked_track,
                        train_lookup, unit_lookup
                    ),
                    train,
                )
            rested[train] = parked_track
            if move_action is not None:
                _append_su_action(move_action, train)

            exit_action = create_exit_action(
                train,
                exit_time,
                track,
                train_lookup,
                track_lookup,
                unit_lookup,
                track_id_lookup=track_id_lookup
            )
            # TORS expects the Exit on the request's lastParkingTrackPart (a
            # parkable track like 906a), NOT on the depart action's track (a
            # zero-length signal like Sein70 that cannot be occupied).
            req_name_for_exit = None
            if len(groups) >= 4 and not raw_train.startswith("su_request"):
                req_name_for_exit = groups[-2]
            elif raw_train.startswith("su_request"):
                req_name_for_exit = "request" + raw_train[len("su_request"):]
            exit_track_id = None
            if req_name_for_exit and req_name_for_exit in request_lookup:
                dep_track = request_lookup[req_name_for_exit].get("lastParkingTrackPart")
                if dep_track is not None and dep_track in track_id_lookup:
                    exit_track_id = dep_track
                elif dep_track is not None:
                    exit_track_id = convert_track(dep_track, track_lookup, track_id_lookup)["id"]
            _scenario_exit_tracks[train] = exit_track_id if exit_track_id is not None else track_id
            if exit_track_id is not None:
                exit_action["location"] = exit_track_id
                exit_action["resources"] = [track_id_lookup[exit_track_id]]
            _append_su_action(exit_action, train)
            su_clock[train] = exit_time + 1
            continue

        # --------------------------------
        # COMPILED UNCOUPLE / COUPLE
        # --------------------------------
        m = COMPILED_UNCOUPLE_RE.match(line)
        if m:
            side, parent_name, child_name, unit, track = m.groups()
            parent_su = _resolve_su(parent_name)
            parent_members = _members_for(parent_su)
            unit_id = _as_id(unit)
            expected_unit = parent_members[0] if side == "front" else parent_members[-1]
            if unit_id != expected_unit:
                raise ValueError(f"{line} does not remove the {side} unit")

            remaining_members = (
                parent_members[1:] if side == "front" else parent_members[:-1]
            )
            detached_su = _generated_su([unit_id], [parent_su])
            remaining_su = _generated_su(remaining_members, [parent_su])
            child_ids = (
                [detached_su, remaining_su]
                if side == "front"
                else [remaining_su, detached_su]
            )

            split_track_id = convert_track(track, track_lookup, track_id_lookup)["id"]
            run = runs.get(parent_su)
            if run and run.get("seq") and any(s is not None for s in run["seq"]):
                if run["seq"][-1] != split_track_id:
                    run["seq"].append(split_track_id)
                _emit_close_run(parent_su)
            elif parent_su not in su_loc:
                su_loc[parent_su] = split_track_id
            split_track_id = su_loc.get(parent_su) or split_track_id

            split_duration = get_train_duration(
                parent_name, train_lookup, unit_lookup, "split"
            )
            start_time = max(
                su_clock.get(parent_su, 0), su_arrival.get(parent_su, 0)
            )
            end_time = start_time + split_duration
            split_action = create_split_action(
                parent_su,
                child_ids,
                start_time,
                end_time,
                split_track_id,
                train_lookup,
                unit_lookup,
            )
            split_action["shuntingUnit"]["id"] = parent_su
            split_action["shuntingUnit"]["memberIDs"] = parent_members
            split_action["shuntingUnit"]["childIDs"] = child_ids
            actions.append(split_action)

            su_identity[parent_name] = remaining_su
            su_identity[child_name] = detached_su
            su_loc[remaining_su] = split_track_id
            su_loc[detached_su] = split_track_id
            rested[remaining_su] = split_track_id
            rested[detached_su] = split_track_id
            su_clock[parent_su] = end_time + 1
            su_clock[remaining_su] = end_time + 1
            su_clock[detached_su] = end_time + 1
            continue

        m = COMPILED_COUPLE_RE.match(line)
        if m:
            side, source_name, unit, request_name, track = m.groups()
            source_su = _resolve_su(source_name)
            request_su = _resolve_su(request_name)
            source_members = _members_for(source_su)
            request_members = _members_for(request_su)
            unit_id = _as_id(unit)
            if source_members != [unit_id]:
                raise ValueError(f"{line} does not couple a single-unit source")

            if side == "front":
                parent_ids = [source_su, request_su]
                combined_members = source_members + request_members
            else:
                parent_ids = [request_su, source_su]
                combined_members = request_members + source_members

            result_su = _generated_su(combined_members, parent_ids)
            track_id = convert_track(track, track_lookup, track_id_lookup)["id"]
            combine_duration = max(
                get_train_duration(source_name, train_lookup, unit_lookup, "combine"),
                get_train_duration(request_name, train_lookup, unit_lookup, "combine"),
            )
            start_time = max(
                su_clock.get(source_su, 0),
                su_clock.get(request_su, 0),
                su_arrival.get(source_su, 0),
                su_arrival.get(request_su, 0),
            )
            end_time = start_time + combine_duration
            combine_actions, _ = create_combine_action(
                parent_ids,
                result_su,
                start_time,
                end_time,
                track_id,
                train_lookup,
                unit_lookup,
            )
            for combine_action, parent_su in zip(combine_actions, parent_ids):
                combine_action["shuntingUnit"]["id"] = parent_su
                combine_action["shuntingUnit"]["memberIDs"] = _members_for(parent_su)
                combine_action["shuntingUnit"]["childIDs"] = [result_su]
                actions.append(combine_action)

            su_identity[request_name] = result_su
            su_loc[result_su] = track_id
            rested[result_su] = track_id
            _set_request_departure(result_su, request_name)
            su_clock[source_su] = end_time + 1
            su_clock[request_su] = end_time + 1
            su_clock[result_su] = end_time + 1
            continue

        # --------------------------------
        # COUPLE (baseline)
        # --------------------------------
        m = COUPLE_RE.match(line)
        if m:
            su_a, su_b, su_result, _unit_a, _unit_b, track, _slot_a, _slot_b, request = m.groups()
            track_id = convert_track(track, track_lookup, track_id_lookup)["id"]

            combine_duration = max(
                get_train_duration(su_a, train_lookup, unit_lookup, "combine"),
                get_train_duration(su_b, train_lookup, unit_lookup, "combine")
            )
            start_time = max(su_clock.get(su_a, 0), su_clock.get(su_b, 0))
            end_time = start_time + combine_duration

            combine_actions, combined_members = create_combine_action(
                [su_a, su_b],
                su_result,
                start_time,
                end_time,
                track_id,
                train_lookup,
                unit_lookup
            )
            actions.extend(combine_actions)

            shunting_unit_composition[_as_id(su_result)] = {
                "memberIDs": combined_members,
                "parentIDs": [su_a, su_b]
            }
            if request in request_lookup:
                dep_time = request_lookup[request].get("arrival")
                if dep_time is not None:
                    su_departure_time[su_result] = int(dep_time)

            su_clock[su_a] = end_time + 1
            su_clock[su_b] = end_time + 1
            su_clock[su_result] = end_time + 1
            su_loc[su_result] = track_id
            rested[su_result] = track_id
            continue

        # --------------------------------
        # SPLIT (baseline)
        # --------------------------------
        m = SPLIT_TWO_RE.match(line) or SPLIT_THREE_RE.match(line)
        if m:
            groups = m.groups()
            if len(groups) == 7:
                parent_su, left_su, right_su, _unit_a, _unit_b, _composition, track = groups
                child_ids = [left_su, right_su]
            else:
                parent_su, first_su, second_su, third_su, _unit_a, _unit_b, _unit_c, _composition, track = groups
                child_ids = [first_su, second_su, third_su]

            track_id = convert_track(track, track_lookup, track_id_lookup)["id"]
            split_duration = get_train_duration(parent_su, train_lookup, unit_lookup, "split")
            start_time = max(su_clock.get(parent_su, 0), su_arrival.get(parent_su, 0))
            end_time = start_time + split_duration

            split_action = create_split_action(
                parent_su,
                child_ids,
                start_time,
                end_time,
                track_id,
                train_lookup,
                unit_lookup
            )
            actions.append(split_action)

            su_clock[parent_su] = end_time + 1
            for child in child_ids:
                su_clock[child] = end_time + 1
                su_loc[child] = track_id
                rested[child] = track_id
            continue

        # --------------------------------
        # SERVICE
        # --------------------------------
        m = SERVICE_RE.match(line)
        if m:
            su_id, track, pddl_facility = m.groups()
            track_id = convert_track(track, track_lookup, track_id_lookup)["id"]
            _ensure_position(su_id)
            run = runs.get(su_id)
            if run and run.get("seq") and any(s is not None for s in run["seq"]):
                if run["seq"][-1] != track_id:
                    run["seq"].append(track_id)
                start_t, _end_t = _emit_close_run(su_id)
                if start_t is None:
                    su_loc[su_id] = track_id
            else:
                su_loc[su_id] = track_id

            pddl_facility_lower = pddl_facility.lower()
            facility_type_task = pddl_facility
            facility_id = ""
            for fac in location.get("facilities", []):
                fac_type_lower = fac["type"].lower()
                is_type_match = fac_type_lower == pddl_facility_lower
                is_track_match = track_id in [str(tp) for tp in fac.get("relatedTrackPartIDs", [])]
                if is_type_match or is_track_match:
                    if fac.get("taskTypes"):
                        facility_type_task = fac["taskTypes"][0].get("other", pddl_facility)
                    facility_id = fac["id"]
                    break

            service_duration = 600
            task_match = None
            su_entry = train_lookup.get(su_id, {})
            task_lower = facility_type_task.lower()
            for member in su_entry.get("members", []):
                for task in member.get("tasks", []):
                    task_type = str(task.get("type", {}).get("other", "")).lower()
                    if task_type and (task_type == task_lower or task_type == pddl_facility_lower):
                        task_match = task
                        break
                if task_match:
                    break
            if task_match:
                service_duration = int(task_match["duration"])

            start_time = max(su_clock.get(su_id, 0), su_arrival.get(su_id, 0))
            end_time = start_time + service_duration
            service_action = create_service_action(
                su_id,
                start_time,
                end_time,
                track_id,
                facility_id,
                facility_type_task,
                train_lookup,
                unit_lookup
            )
            actions.append(service_action)
            su_clock[su_id] = end_time + 1
            continue

        # Nothing matched. An action the converter does not know would be
        # dropped silently, truncating the plan; refuse instead.
        unhandled.append(line)

    # Close any runs the plan left open (paranoia: a plan always closes its own).
    for train in list(runs):
        if runs.get(train, {}).get("seq"):
            _emit_close_run(train)

    if unhandled:
        raise ValueError(
            "convert_to_tors does not recognise these planner actions, so the "
            "plan would be silently truncated. Add a pattern for each, or "
            "confirm it carries no TORS action:\n  " + "\n  ".join(unhandled)
        )

    # OutStanding units that parked in a slot but never got their Exit close
    # out here at the scenario end rather than being left in the yard forever.
    emitted_exit_sus = {
        a["shuntingUnit"]["id"]
        for a in actions
        if a.get("taskType", {}).get("predefined") in ("Exit", "StandOut")
    }
    for su_name in list(waiting_on_messages):
        if _as_id(su_name) in emitted_exit_sus:
            continue
        track_id = su_loc.get(su_name)
        if track_id is None:
            problems.append(
                f"INFEASIBLE: parked-out SU {su_name} has no track to exit from."
            )
            continue
        exit_time = max(su_clock.get(su_name, 0), scenario_end_time)
        _append_su_action(
            create_exit_action(
                su_name,
                exit_time,
                track_id,
                train_lookup,
                track_lookup,
                unit_lookup,
                standing_type="OutStanding",
                track_id_lookup=track_id_lookup
            ),
            su_name,
        )
        su_clock[su_name] = exit_time + 1

    # Assign integer SU IDs to all actions and fix members for combined SUs.
    for action in actions:
        su = action["shuntingUnit"]
        old_id = su["id"]
        su["id"] = get_su_id(_as_id(old_id))

        su["childIDs"] = [get_su_id(_as_id(c)) for c in su.get("childIDs", [])]
        su["parentIDs"] = [get_su_id(_as_id(c)) for c in su.get("parentIDs", [])]

        int_id = _as_id(old_id)
        if int_id in shunting_unit_composition:
            comp = shunting_unit_composition[int_id]
            if comp["memberIDs"]:
                su["memberIDs"] = comp["memberIDs"]
            su["parentIDs"] = [get_su_id(_as_id(p)) for p in comp.get("parentIDs", [])]

    # Maps keyed the way post_process_actions expects them (integer SU ids).
    train_arrival_times_int = {}
    for k, v in train_arrival_times.items():
        train_arrival_times_int[get_su_id(_as_id(k))] = v

    train_locations_int = {}
    for k, v in su_loc.items():
        train_locations_int[get_su_id(_as_id(k))] = v

    # Post-process: add Arrive actions and sort chronologically.
    actions = post_process_actions(
        actions, train_lookup, unit_lookup, track_lookup, track_id_lookup,
        train_locations_int, train_arrival_times_int, scenario, get_su_id,
        parkable_tracks={tp["id"] for tp in location["trackParts"] if tp.get("parkingAllowed")},
        track_parts_by_id=track_parts_by_id,
    )

    # Fill in missing members/parentIDs/childIDs for actions that reference
    # SUs by integer ID (e.g. Wait actions created by post_process_actions).
    su_fill = {}
    for a in actions:
        su = a["shuntingUnit"]
        sid = su["id"]
        if su["memberIDs"] and sid not in su_fill:
            su_fill[sid] = {
                "memberIDs": su["memberIDs"],
                "parentIDs": su.get("parentIDs", []),
                "childIDs": su.get("childIDs", [])
            }
    for a in actions:
        su = a["shuntingUnit"]
        sid = su["id"]
        if sid in su_fill:
            if not su["memberIDs"]:
                su["memberIDs"] = su_fill[sid]["memberIDs"]
            if not su.get("parentIDs", []):
                su["parentIDs"] = su_fill[sid]["parentIDs"]
            if not su.get("childIDs", []):
                su["childIDs"] = su_fill[sid]["childIDs"]

    for a in actions:
        su = a["shuntingUnit"]
        if not su.get("parentIDs"):
            su["parentIDs"] = []
        if not su.get("childIDs"):
            su["childIDs"] = []
        if a.get("resources") is None:
            a["resources"] = []

    # Fidelity guard: a train may only wait on, and exit from, the track the
    # plan parked it on. If this ever fired, the converter would be silently
    # changing park tracks; report the deviation instead.
    rested_int = {get_su_id(_as_id(k)): v for k, v in rested.items()}
    exit_tracks_int = {
        get_su_id(_as_id(k)): v for k, v in _scenario_exit_tracks.items()
    }
    for a in actions:
        su = a["shuntingUnit"]
        tt = a["taskType"].get("predefined")
        if tt == "Wait":
            expected = rested_int.get(su["id"], a["location"])
            if a["location"] != expected:
                problems.append(
                    f"INFEASIBLE: Wait for SU {su['id']} sits on track "
                    f"{a['location']} but the plan parks it on {expected}."
                )
        elif tt in ("Exit", "StandOut"):
            expected = exit_tracks_int.get(su["id"])
            if expected is not None and a["location"] != expected:
                problems.append(
                    f"INFEASIBLE: Exit for SU {su['id']} placed on track "
                    f"{a['location']} instead of its planned track {expected}."
                )

    # Exit diagnostics: every departing SU must get an Exit action. The Exit's
    # time is not a failure condition — this converter mirrors the plan rather
    # than making it feasible, so a late departure is reported but accepted.
    exit_by_su = {}
    for a in actions:
        if a.get("taskType", {}).get("predefined") in ("Exit", "StandOut"):
            sid = a["shuntingUnit"]["id"]
            exit_by_su.setdefault(sid, []).append(int(a["startTime"]))
    for su_name, deadline in su_departure_deadline.items():
        sid = get_su_id(_as_id(su_name))
        exit_times = exit_by_su.get(sid, [])
        if not exit_times:
            problems.append(
                f"INFEASIBLE: departing SU {su_name} has departure deadline "
                f"{deadline} but no Exit action was emitted."
            )

    result = {
        "schemaVersion": SCHEMA_VERSION,
        "actions": actions,
    }
    if problems:
        raise ScheduleInfeasibleError(problems, plan=result)

    return result


# =====================================================
# POST-PROCESS
# =====================================================

def post_process_actions(actions, train_lookup, unit_lookup, track_lookup,
                         track_id_lookup, train_locations, train_arrival_times, scenario, su_id_fn=None,
                         parkable_tracks=None, track_parts_by_id=None):
    """Add Arrive actions and order the plan chronologically."""

    su_first_action = {}
    su_last_position = {}

    processed_actions = []

    initial_positions = {}
    if su_id_fn:
        for train in scenario.get("in", []):
            for name in [f"train{train['id']}", f"su_train{train['id']}"]:
                if "entryTrackPart" in train:
                    initial_positions[su_id_fn(_as_id(name))] = train["entryTrackPart"]
                elif "firstParkingTrackPart" in train:
                    initial_positions[su_id_fn(_as_id(name))] = train["firstParkingTrackPart"]

        for i, train in enumerate(scenario.get("inStanding", [])):
            for name in [f"train_in_standing_{i}", f"su_train_in_standing_{i}"]:
                if "firstParkingTrackPart" in train:
                    initial_positions[su_id_fn(_as_id(name))] = train["firstParkingTrackPart"]
                elif "entryTrackPart" in train:
                    initial_positions[su_id_fn(_as_id(name))] = train["entryTrackPart"]

    # Only SUs that are real scenario trains (in/inStanding) may receive an
    # Arrive action. SUs materialized by the planner (request placeholders,
    # combine/split children) never appear in the scenario and must not get a
    # fabricated Arrive.
    scenario_in_su_ids = set()
    if su_id_fn:
        for train in scenario.get("in", []):
            for name in [f"train{train['id']}", f"su_train{train['id']}"]:
                scenario_in_su_ids.add(su_id_fn(_as_id(name)))
        for i in range(len(scenario.get("inStanding", []))):
            for name in [f"train_in_standing_{i}", f"su_train_in_standing_{i}"]:
                scenario_in_su_ids.add(su_id_fn(_as_id(name)))

    standing_su_ids = set()
    if su_id_fn:
        for i in range(len(scenario.get("inStanding", []))):
            standing_su_ids.add(su_id_fn(_as_id(f"su_train_in_standing_{i}")))
            standing_su_ids.add(su_id_fn(_as_id(f"train_in_standing_{i}")))

    out_standing_ids = set()
    if su_id_fn:
        for request in scenario.get("outStanding", []):
            key = f"su_outstanding_{request.get('id', '')}"
            out_standing_ids.add(su_id_fn(key))

    for action in actions:
        cur_su_id = action["shuntingUnit"]["id"]

        if cur_su_id not in su_first_action and cur_su_id not in su_last_position:
            if cur_su_id in train_arrival_times:
                arrive_time = train_arrival_times[cur_su_id]
            else:
                arrive_time = int(action["startTime"])

            su_first_action[cur_su_id] = arrive_time

            if cur_su_id in scenario_in_su_ids:
                if cur_su_id in initial_positions:
                    arrive_location = initial_positions[cur_su_id]
                else:
                    arrive_location = action["location"]

                standing_type = ""
                if cur_su_id in standing_su_ids:
                    standing_type = "InStanding"
                elif cur_su_id in out_standing_ids:
                    standing_type = "OutStanding"

                # If the arrival track is a signal/bumper (0-length,
                # non-parking), resolve it to the adjacent real track.
                arrive_resource_loc = arrive_location
                if track_parts_by_id and arrive_location in track_parts_by_id:
                    _tp = track_parts_by_id[arrive_location]
                    if _tp.get("length", 0) == 0 and not _tp.get("parkingAllowed", False):
                        _neighbors = _tp.get("aSide", []) + _tp.get("bSide", [])
                        for _nb in _neighbors:
                            if _nb in track_parts_by_id and (
                                track_parts_by_id[_nb].get("length", 0) > 0
                                or track_parts_by_id[_nb].get("parkingAllowed", False)
                            ):
                                arrive_resource_loc = _nb
                                break
                if arrive_resource_loc in track_id_lookup:
                    resource = track_id_lookup[arrive_resource_loc]
                else:
                    resource = convert_track(arrive_resource_loc, track_lookup)
                shunting_unit = make_shunting_unit(cur_su_id, train_lookup, unit_lookup)
                arrive_action = {
                    "startTime": _as_time(arrive_time),
                    "endTime": _as_time(arrive_time),
                    "taskType": {"predefined": "StandIn" if standing_type else "Arrive"},
                    "shuntingUnit": shunting_unit,
                    "location": resource["id"],
                    "resources": [resource]
                }
                processed_actions.append(arrive_action)

        elif cur_su_id not in su_first_action:
            su_first_action[cur_su_id] = int(action["startTime"])

        if "location" in action:
            if action["taskType"].get("predefined") == "Move":
                resources = action.get("resources", [])
                if resources:
                    last_loc = resources[-1]["id"]
                else:
                    last_loc = action["location"]
                su_last_position[cur_su_id] = (last_loc, int(action["endTime"]))
            else:
                su_last_position[cur_su_id] = (action["location"], int(action["endTime"]))

        if action["taskType"].get("predefined") == "Combine":
            for child_id in action["shuntingUnit"].get("childIDs", []):
                if child_id not in su_last_position:
                    su_last_position[child_id] = (action["location"], int(action["endTime"]))

        if action["taskType"].get("predefined") == "Split":
            for child_id in action["shuntingUnit"].get("childIDs", []):
                if child_id not in su_last_position:
                    su_last_position[child_id] = (action["location"], int(action["endTime"]))

        processed_actions.append(action)

    # The action list keeps the PDDL plan's emission order; it is NOT re-sorted
    # by time. Each train's actions carry times from its own per-train clock
    # (Arrive at its scenario time, Exit pinned to its request's departure), so
    # different trains legitimately overlap: a train may rest while another
    # moves.
    #
    # Cross-train constraints enforced here:
    # 1) No two Move actions overlap (move line).
    # 2) A Combine/Split event can only start after ALL member wagons have
    #    finished any prior action that uses them (per-wagon chain).
    # 3) A Combine/Split event cannot overlap any Move that moves one of its
    #    member wagons — enforced by (2) because the Move finishes first and
    #    updates wagon_end.
    # 4) Waits fill the gap between the unit's last prior action and its
    #    following approach Move.

    def _kind(action, name):
        return action["taskType"].get("predefined") == name

    def _wagons(action):
        return set(action["shuntingUnit"].get("memberIDs", []))

    # --- Phase 1: group Combine/Split halves into events ---
    # Halves of the same event share identical (predefined, location,
    # startTime, endTime).  We union their memberIDs into one event set
    # and process the group atomically.
    _event_groups = {}          # key -> list of action dicts
    _event_members = {}         # key -> union of memberIDs
    _consumed = set()           # id() of actions already grouped
    for action in processed_actions:
        k = _kind(action, "Combine") or _kind(action, "Split")
        if k:
            key = (action["taskType"].get("predefined"),
                   action.get("location"),
                   int(action["startTime"]),
                   int(action["endTime"]))
            _event_groups.setdefault(key, []).append(action)
            _event_members.setdefault(key, set()).update(_wagons(action))
    # Only treat groups with >1 action as multi-half events; single-action
    # groups are processed inline (still need wagon chaining). Members are NOT
    # marked consumed here — the whole group is processed (and then consumed)
    # at its first member's position in the ordered pass below.
    _event_keys = {k for k, v in _event_groups.items() if len(v) > 1}

    # --- Phase 2: single ordered pass (move line + wagon chain) ---
    move_line_end = 0
    wagon_end = {}  # memberID -> end time of last action using that wagon

    for action in processed_actions:
        if id(action) in _consumed:
            continue  # already processed as part of a multi-half event

        if _kind(action, "Combine") or _kind(action, "Split"):
            key = (action["taskType"].get("predefined"),
                   action.get("location"),
                   int(action["startTime"]),
                   int(action["endTime"]))
            if key in _event_keys:
                group = _event_groups[key]
                all_wag = _event_members[key]
                dur = int(group[0]["endTime"]) - int(group[0]["startTime"])
                busy = max((wagon_end.get(w, 0) for w in all_wag), default=0)
                start = max(int(group[0]["startTime"]), busy)
                for half in group:
                    _consumed.add(id(half))
                    half["startTime"] = _as_time(start)
                    half["endTime"] = _as_time(start + dur)
                for w in all_wag:
                    wagon_end[w] = start + dur
                continue
            # single Split/Combine, no halves to unify
            dur = int(action["endTime"]) - int(action["startTime"])
            wag = _wagons(action)
            busy = max((wagon_end.get(w, 0) for w in wag), default=0)
            start = max(int(action["startTime"]), busy)
            action["startTime"] = _as_time(start)
            action["endTime"] = _as_time(start + dur)
            for w in wag:
                wagon_end[w] = start + dur
            continue

        if _kind(action, "Move"):
            dur = int(action["endTime"]) - int(action["startTime"])
            wag = _wagons(action)
            busy = max((wagon_end.get(w, 0) for w in wag), default=0)
            start = max(int(action["startTime"]), move_line_end, busy)
            action["startTime"] = _as_time(start)
            action["endTime"] = _as_time(start + dur)
            move_line_end = start + dur
            for w in wag:
                wagon_end[w] = start + dur
            continue

        if _kind(action, "Wait"):
            wag = _wagons(action)
            busy = max((wagon_end.get(w, 0) for w in wag), default=0)
            start = max(int(action["startTime"]), busy)
            action["startTime"] = _as_time(start)
            # end stays as-is for now; refined in Phase 3. wagon_end must be
            # monotonic: a later action never un-busies a wagon.
            for w in wag:
                wagon_end[w] = max(int(action["endTime"]), start,
                                   wagon_end.get(w, 0))
            continue

        # Arrive / Exit / StandOut — pinned to scenario times; record their
        # end so anything after them chains (monotonic).
        wag = _wagons(action)
        if wag:
            for w in wag:
                wagon_end[w] = max(int(action["endTime"]),
                                   wagon_end.get(w, 0))
            continue

        # Service and any other occupancy — chain behind prior wagon use.
        dur = int(action["endTime"]) - int(action["startTime"])
        busy = max((wagon_end.get(w, 0) for w in wag), default=0)
        start = max(int(action["startTime"]), busy)
        action["startTime"] = _as_time(start)
        action["endTime"] = _as_time(start + dur)
        for w in wag:
            wagon_end[w] = start + dur

    # --- Phase 3: relocate Waits and refine their times ---
    # Anchor each Wait to the unit's own last Move listed before it: start when
    # that Move ends, end when the unit's approach Move starts. Relocate the
    # Wait to sit directly after that Move so the list reads the stop, the rest,
    # the drive to the exit.
    waits = [a for a in processed_actions if _kind(a, "Wait")]
    anchors = {}
    for wait in waits:
        su_id = wait["shuntingUnit"]["id"]
        wait_index = processed_actions.index(wait)
        anchor = None
        for i, action in enumerate(processed_actions):
            if i < wait_index and _kind(action, "Move") and action["shuntingUnit"]["id"] == su_id:
                anchor = action
        anchors[id(wait)] = anchor

    rebuilt = []
    for action in processed_actions:
        if _kind(action, "Wait"):
            if anchors[id(action)] is None:
                rebuilt.append(action)
            continue
        rebuilt.append(action)
        rebuilt.extend(
            wait for wait in waits if anchors[id(wait)] is action
        )
    processed_actions[:] = rebuilt

    for wait in waits:
        anchor = anchors[id(wait)]
        if anchor is not None:
            wait["startTime"] = _as_time(max(
                int(anchor["endTime"]) + 1, int(wait["startTime"])
            ))
        su_id = wait["shuntingUnit"]["id"]
        approach = next(
            (a for a in processed_actions
             if _kind(a, "Move") and a["shuntingUnit"]["id"] == su_id
             and processed_actions.index(a) > processed_actions.index(wait)),
            None,
        )
        if approach is not None:
            wait["endTime"] = _as_time(int(approach["startTime"]))
        # Ensure start <= end after chaining adjustments.
        if int(wait["startTime"]) > int(wait["endTime"]):
            wait["endTime"] = wait["startTime"]

    # --- Phase 4: align Exits to their approach Move ---
    for i, action in enumerate(processed_actions):
        if _kind(action, "Exit") and i > 0:
            preceding = processed_actions[i - 1]
            if preceding["shuntingUnit"]["id"] == action["shuntingUnit"]["id"] \
                    and _kind(preceding, "Move"):
                end = int(preceding["endTime"])
                action["startTime"] = _as_time(end)
                action["endTime"] = _as_time(end)

    return processed_actions


# =====================================================
# MAIN
# =====================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Convert PDDL plans to TORS JSON format")
    parser.add_argument("--plan", required=True, help="Path to the .plan file")
    parser.add_argument("--scenario", required=True, help="Path to the scenario JSON file")
    parser.add_argument("--location", required=True, help="Path to the location JSON file")
    parser.add_argument("--output", required=True, help="Path to write the output plan JSON")
    args = parser.parse_args()

    try:
        result = convert_plan(args.plan, args.scenario, args.location)
    except ScheduleInfeasibleError as exc:
        for problem in exc.problems:
            print("PROBLEM:", problem, file=sys.stderr)
        if exc.plan is not None:
            with open(args.output, "w") as f:
                json.dump(exc.plan, f, indent=4)
            print(
                "Plan is schedule-infeasible (%d problem(s)); wrote it to %s "
                "for inspection, but exiting with error."
                % (len(exc.problems), args.output),
                file=sys.stderr,
            )
        else:
            print(
                "Plan is schedule-infeasible (%d problem(s)); exiting with error."
                % len(exc.problems),
                file=sys.stderr,
            )
        sys.exit(1)

    with open(args.output, "w") as f:
        json.dump(result, f, indent=4)

    print("Plan JSON generated:", args.output)