import os
import re
import json
import sys
from collections import deque


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

# PDDL plan format: (action_name arg1 arg2 ...)
# Interchange schema version this converter writes. Bumped together with the
# generator, solver and evaluator; see SCHEMA_CHANGELOG.md there.
SCHEMA_VERSION = 1


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
# The corridor model's form of the above. It has no slot argument — four args
# rather than five — because compile_precomputed_actions bakes the unit-to-slot
# matching into the action itself, and the name gains a compiled_ prefix and
# loses the _su. Absent this, the only departure the corridor model ever emits
# matched nothing and every plan ended at its last service task.
COMPILED_DEPART_FOR_REQUEST_RE = re.compile(
    r"\(compiled_depart_(?:aside|bside)_for_request ([^ ]+) ([^ ]+) ([^ ]+) ([^)]+)\)"
)

# Coupling / splitting / service / match
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


COMBINE_DURATION = 180
SPLIT_DURATION = 120


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
    
    # From incoming trains
    for train in scenario.get("in", []):
        for member in train.get("members", []):
            lookup[f"unit{member['id']}"] = member
    
    # From standing trains
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
# HELPERS
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

    # Handle shunting unit IDs. post_process_actions calls this with an already
    # assigned integer id rather than a PDDL name, so guard the string test.
    if isinstance(train_id, str) and train_id.startswith("su_"):
        # Try to resolve from unit lookup
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

    # standingType has been dropped from the schema: a train that was already
    # in the yard, or stays in it, is expressed by the task type itself.
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


def create_park_action(train_id, time, track,
                       train_lookup, track_lookup, unit_lookup=None,
                       standing_type="", track_id_lookup=None):
    """Create a Park action.

    Currently unreachable: nothing calls this, and no converted plan contains a
    Park. Note before wiring it up that "Park" is not one of the schema's
    predefined task types, so the evaluator would reject the plan — parking is
    expressed by where a Move ends, not by an action of its own. standing_type
    is accepted for call compatibility and unused.
    """
    resource = convert_track(track, track_lookup, track_id_lookup)
    shunting_unit = make_shunting_unit(train_id, train_lookup, unit_lookup)

    return {
        "startTime": _as_time(time),
        "endTime": _as_time(time),
        "taskType": {
            "predefined": "Park"
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
    
    # Default durations
    return COMBINE_DURATION if duration_type == "combine" else SPLIT_DURATION


# =====================================================
# GRAPH/Topology
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
            nb_id = str(nb_id)
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
                if nb not in visited and nb in switch_ids:
                    visited.add(nb)
                    queue.append(path + [nb])
        return []

    a_path = _bfs(a_adj, start, goal)
    b_path = _bfs(b_adj, start, goal)
    if a_path and b_path:
        return a_path if len(a_path) <= len(b_path) else b_path
    return a_path or b_path or [start, goal]


def remove_loops(path):
    """Remove loops from a raw move path (e.g. A->B->A->C becomes A->C).
    Direction-flip excursions added by the planner are redundant for a Move:
    TORS only needs the net displacement, and re-entering a track the train
    already stands on (like 906a) makes the Move invalid."""
    if not path:
        return path
    result = []
    seen = set()
    for track in path:
        if track in seen:
            idx = result.index(track)
            result = result[:idx + 1]
            seen = set(result)
        else:
            seen.add(track)
            result.append(track)
    return result


def expand_path(path, a_adj, b_adj, switch_ids):
    if not path:
        return path
    path = remove_loops(path)
    expanded = [path[0]]
    for i in range(len(path) - 1):
        segment = bfs_through_switches(a_adj, b_adj, path[i], path[i + 1], switch_ids)
        expanded.extend(segment[1:])
    return expanded


TRACK_CROSSING_TIME = 60
SWITCH_CROSSING_TIME = 30
SWITCH_COST = {"Switch": 1, "EnglishSwitch": 2, "HalfEnglishSwitch": 2}


def switch_cost_map(location):
    """Map trackPart id -> switch cost (1 per Switch, 2 per English/HalfEnglish switch, else 0)."""
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


def compute_move_duration(expanded_path, a_adj, b_adj, switch_costs=None, reversal_duration=0):
    """Compute a Move action's duration using the C# solver's ComputeDuration model:

        duration = (Tracks.Length + TotalReversals) * TrackCrossingTime
                 + TotalSwitches * SwitchCrossingTime
                 + TotalReversals * ReversalDuration

    Tracks.Length is the path length (start track included), TotalSwitches is the
    summed switch cost over the path, TotalReversals counts same-side turn-arounds.
    Unknown tracks are treated as plain tracks (60s, no switch cost).
    """
    if switch_costs is None:
        switch_costs = {}
    reversals = compute_reversals(expanded_path, a_adj, b_adj)
    total_switches = sum(switch_costs.get(tid, 0) for tid in expanded_path)
    return (
        (len(expanded_path) + reversals) * TRACK_CROSSING_TIME
        + total_switches * SWITCH_CROSSING_TIME
        + reversals * reversal_duration
    )


def get_reversal_duration(train, train_lookup):
    """ReversalDuration for a train: max across its composed member types (0 if unknown)."""
    entry = train_lookup.get(train)
    if not entry:
        return 0
    durations = []
    for t in entry.get("member_types", []):
        try:
            durations.append(int(t.get("reversalDuration", 0) or 0))
        except (TypeError, ValueError):
            durations.append(0)
    return max(durations, default=0)


# =====================================================
# CONVERTER
# =====================================================

def convert_plan(plan_file, scenario_file, location_file):
    """Main conversion function with support for coupling/uncoupling/service"""

    with open(scenario_file) as f:
        scenario = json.load(f)

    with open(location_file) as f:
        location = json.load(f)

    train_lookup = build_train_lookup(scenario)
    unit_lookup = build_unit_lookup(scenario)
    request_lookup = build_request_lookup(scenario)
    track_lookup = build_track_lookup(location)
    track_id_lookup = build_track_id_lookup(location)
    a_adj = build_directed_adj(location, "aSide")
    b_adj = build_directed_adj(location, "bSide")
    switch_ids = build_switch_sets(location)
    switch_costs = switch_cost_map(location)
    parkable_tracks = {tp["id"] for tp in location["trackParts"] if tp.get("parkingAllowed")}
    zero_length_tracks = {tp["id"] for tp in location["trackParts"] if tp.get("length", 0) == 0}
    scenario_end_time = int(scenario.get("endTime", 0))

    # Scenario arrival times and physical entry tracks, from the unified "in"
    # list. TORS materializes arrivals on the first parking track (e.g. 906a),
    # not on the entry signal (Sein70); the parking track must be free at
    # arrival time, so it is the track used for arrival holds.
    scenario_arrival_times = {}
    scenario_entry_tracks = {}
    for train in scenario.get("in", []):
        arrival = int(train.get("arrival", 0))
        scenario_arrival_times[f"train{train['id']}"] = arrival
        scenario_arrival_times[f"su_train{train['id']}"] = arrival
        entry = train.get("firstParkingTrackPart") or train.get("entryTrackPart")
        if entry is not None:
            entry_id = convert_track(entry, track_lookup, track_id_lookup)["id"]
            scenario_entry_tracks[f"train{train['id']}"] = entry_id
            scenario_entry_tracks[f"su_train{train['id']}"] = entry_id

    def _strip_trailing_zero_length(path):
        """Remove trailing zero-length tracks (bumpers/signals) from path.
        These are points like Sein70 that trains cannot physically occupy."""
        while len(path) > 1 and path[-1] in zero_length_tracks:
            path = path[:-1]
        return path

    def _strip_for_departure(path):
        """Strip trailing non-parkable tracks for a departing train.
        The train needs to wait on a parkable track before departing."""
        while len(path) > 1 and path[-1] not in parkable_tracks:
            path = path[:-1]
        return path

    current_time = 0
    # Scheduler state: per-SU availability clocks plus a track-occupancy ledger,
    # so independent shunting units can be scheduled in parallel (collision-free)
    # while the scenario's arrival/departure times stay hard constraints.
    su_next_free = {}          # SU name -> earliest time its next action may start
    track_intervals = {}       # trackPartId -> sorted, non-overlapping (start, end) reservations
    arrival_holds = {}         # SU name -> {"track", "arrival", "closed"} (entry-track hold until first action)
    problems = []              # infeasibility diagnostics collected during conversion
    departing_sus = set()      # SU names with a scenario departure deadline
    su_departure_deadline = {} # SU name -> requested departure time (hard deadline)
    waiting_on_messages = {}   # parked-out SU name -> parking slot id (unit waits there)
    active_trains = {}
    train_locations = {}  # Track where each train is currently located
    train_arrival_times = {}  # Track when trains arrive
    shunting_unit_composition = {}  # Track composition of shunting units
    su_identity = {}  # PDDL request SU name -> existing physical SU name
    actions = []
    
    # SU ID mapping: internal name -> sequential integer ID
    su_name_to_int = {}
    next_su_id = 0
    
    # Map from SU name (e.g. su_request4000) to departure time from scenario
    su_departure_time = {}
    
    def get_su_id(name):
        """Assign each PDDL shunting-unit name a stable integer id.

        The result is used as ShuntingUnit.id and inside parentIDs/childIDs, all
        of which the schema types as integers. This used to store str(next_su_id)
        — harmless when every id on the wire was a string, but now it both fails
        validation and, more quietly, breaks the memberIDs fill pass below, which
        matches actions by su["id"]: a string '0' and an integer 0 are different
        keys, so Arrive actions silently came out with no members at all.
        """
        nonlocal next_su_id
        if name not in su_name_to_int:
            su_name_to_int[name] = next_su_id
            next_su_id += 1
        return su_name_to_int[name]

    def _resolve_su(name):
        """Return the physical SU represented by a PDDL request alias."""
        return su_identity.get(name, name)

    def _set_request_departure(su_id, request_su):
        """Assign the request's departure time to its physical SU."""
        request_name = request_su[3:] if request_su.startswith("su_") else request_su
        if request_name in request_lookup:
            departure = request_lookup[request_name].get("arrival")
            if departure is not None:
                su_departure_time[su_id] = int(departure)

    def _clock(name):
        """Resolve an SU name to its clock key (kept for parity with the
        compiled-matching models that alias single-unit request SUs)."""
        return name

    def _intervals(track):
        return track_intervals.setdefault(track, [])

    def _earliest_on_track(track, t_min, duration):
        """Earliest start >= t_min on one track for a contiguous `duration`
        second block that does not overlap any existing reservation."""
        t = t_min
        for start, end in _intervals(track):
            if end <= t:
                continue
            if start >= t + duration:
                break
            t = end
        return t

    def earliest_start(tracks, t_min, duration):
        """Earliest start >= t_min for a `duration` second block that is free
        on ALL `tracks` simultaneously (fixed point over the track set)."""
        t = t_min
        changed = True
        while changed:
            changed = False
            for tr in tracks:
                nt = _earliest_on_track(tr, t, duration)
                if nt > t:
                    t = nt
                    changed = True
        return t

    def reserve(tracks, start, end):
        """Record occupancy [start, end) on each track, merging overlapping or
        touching intervals so the ledger stays sorted and non-overlapping."""
        if end <= start:
            return
        for tr in tracks:
            ivs = _intervals(tr)
            merged_start, merged_end = start, end
            kept = []
            for s, e in ivs:
                if e < merged_start:
                    kept.append((s, e))
                elif s > merged_end:
                    kept.append((s, e))
                else:
                    merged_start = min(merged_start, s)
                    merged_end = max(merged_end, e)
            kept.append((merged_start, merged_end))
            kept.sort(key=lambda x: (x[0], x[1]))
            track_intervals[tr] = kept

    def busy_on_track(track, t0, t1):
        """True if any reservation on `track` overlaps [t0, t1)."""
        for start, end in _intervals(track):
            if start >= t1:
                break
            if end > t0:
                return True
        return False

    def _close_hold(su, action_start):
        """Close an open entry-track hold for `su` at the moment its first
        physical action starts. The window [arrival, action_start + 1) is
        reserved on the hold track, or reported as a collision if another SU
        already booked that track while the train was waiting."""
        hold = arrival_holds.get(su)
        if not hold or hold["closed"]:
            return
        hold["closed"] = True
        if action_start + 1 <= hold["arrival"]:
            return
        entry_track = hold["track"]
        if busy_on_track(entry_track, hold["arrival"], action_start + 1):
            problems.append(
                f"INFEASIBLE: {su} arrives at {hold['arrival']} on track "
                f"{entry_track} but that track is occupied in [{hold['arrival']}, "
                f"{action_start}) so the train cannot wait there."
            )
        else:
            reserve([entry_track], hold["arrival"], action_start + 1)

    def _schedule_move(train, expanded_path, duration, t_min=None):
        """Earliest-start schedule a Move for `train` over `expanded_path`.

        Reserves the full expanded path for [start, end + 1), closes the SU's
        entry-track arrival hold, advances the SU's clock and returns
        (start, end)."""
        nonlocal current_time
        if t_min is None:
            t_min = su_next_free.get(_clock(train), 0)
        start = earliest_start(expanded_path, t_min, duration)
        end = start + duration
        _close_hold(train, start)
        reserve(expanded_path, start, end + 1)
        su_next_free[_clock(train)] = end + 1
        current_time = max(current_time, end + 1)
        return start, end

    def _normalize_plan_line(plan_line):
        """Convert SymbolicPlanners `action(arg1, arg2)` to PDDL `(action arg1 arg2)` format."""
        m = re.match(r"(\w[\w_]*)\((.+)\)$", plan_line)
        if m:
            action = m.group(1)
            args = re.split(r",\s*", m.group(2))
            return "(" + action + " " + " ".join(args) + ")"
        return plan_line

    with open(plan_file) as f:
        lines = [line.strip() for line in f if line.strip()]

    unhandled = []
    for line in lines:
        line = _normalize_plan_line(line)
        # --------------------------------
        # MATCH
        # --------------------------------
        m = MATCH_RE.match(line)
        if m:
            unit, slot = m.groups()
            continue

        # Both complete_request_composition and compiled_advance_request_N are logical
        # hence no TORS significance
        if COMPLETE_REQUEST_RE.match(line) or COMPILED_ADVANCE_RE.match(line):
            continue

        # --------------------------------
        # ARRIVE_SU
        # --------------------------------
        m = ARRIVE_SU_RE.match(line)
        if m:
            su_id = m.group(1)
            track = m.group(2)
            arrival = scenario_arrival_times.get(su_id, 0)
            train_arrival_times[su_id] = arrival
            train_locations[su_id] = track
            # The train physically enters on the scenario's entry track; it
            # holds that track from its arrival until its first action starts.
            hold_track = scenario_entry_tracks.get(
                su_id, convert_track(track, track_lookup, track_id_lookup)["id"]
            )
            arrival_holds[su_id] = {
                "track": hold_track,
                "arrival": arrival,
                "closed": False,
            }
            su_next_free[_clock(su_id)] = max(su_next_free.get(_clock(su_id), arrival), arrival)
            continue

        m = ENTER_YARD_SU_RE.match(line)
        if m:
            su_id, entry, target = m.groups()
            entry_id = convert_track(entry, track_lookup, track_id_lookup)["id"]
            target_id = convert_track(target, track_lookup, track_id_lookup)["id"]
            path = expand_path([entry_id, target_id], a_adj, b_adj, switch_ids)
            duration = compute_move_duration(
                path,
                a_adj,
                b_adj,
                switch_costs,
                get_reversal_duration(su_id, train_lookup),
            )
            start_time, end_time = _schedule_move(su_id, path, duration)
            actions.append(
                create_move_action(
                    su_id,
                    start_time,
                    end_time,
                    path,
                    train_lookup,
                    track_id_lookup,
                    unit_lookup,
                )
            )
            train_locations[su_id] = target_id
            continue

        m = COMPILED_START_RE.match(line)
        if m:
            source_su, unit, request_su, track = m.groups()
            source_su = _resolve_su(source_su)
            su_identity[request_su] = source_su
            _set_request_departure(source_su, request_su)
            train_locations[source_su] = convert_track(
                track, track_lookup, track_id_lookup
            )["id"]
            continue

        # --------------------------------
        # START MOVE / START MOVE SU
        # --------------------------------
        m = START_MOVE_SU_RE.match(line)
        if m:
            train = _resolve_su(m.group(1))
            su_start = su_next_free.get(_clock(train), 0)
            if train in train_arrival_times:
                su_start = max(su_start, train_arrival_times[train])
            active_trains[train] = {
                "start_time": su_start,
                "path": []
            }
            
            # Look up initial position from scenario if not already known
            if train not in train_locations:
                stripped = train[3:] if train.startswith("su_") else train
                found = False
                for i, standing in enumerate(scenario.get("inStanding", [])):
                    for name in [f"train_in_standing_{i}", f"su_train_in_standing_{i}"]:
                        if name == stripped or name == train:
                            if "firstParkingTrackPart" in standing:
                                train_locations[train] = standing["firstParkingTrackPart"]
                            train_arrival_times[train] = int(standing.get("arrival", 0))
                            found = True
                            break
                    if found:
                        break
                if not found:
                    for incoming in scenario.get("in", []):
                        if stripped == f"train{incoming['id']}" or train == f"su_train{incoming['id']}":
                            if "entryTrackPart" in incoming:
                                train_locations[train] = incoming["entryTrackPart"]
                            train_arrival_times[train] = int(incoming.get("arrival", current_time))
                            break
                # Trains already in the yard (inStanding / arrived without an
                # arrive_su line) occupy their initial track until they move.
                if train not in arrival_holds and train in train_locations:
                    hold_track = convert_track(train_locations[train], track_lookup, track_id_lookup)["id"]
                    arrival_holds[train] = {
                        "track": hold_track,
                        "arrival": int(train_arrival_times.get(train, 0)),
                        "closed": False,
                    }
                    su_next_free[_clock(train)] = max(
                        su_next_free.get(_clock(train), 0),
                        int(train_arrival_times.get(train, 0)),
                    )
            
            continue

        # --------------------------------
        # MOVE / MOVE SU
        # --------------------------------
        m = MOVE_SU_RE.match(line)
        if m:
            train, from_track, to_track = m.groups()
            train = _resolve_su(train)
            
            if train not in active_trains:
                su_start = current_time
                if train in train_arrival_times:
                    su_start = max(current_time, train_arrival_times[train])
                active_trains[train] = {
                    "start_time": su_start,
                    "path": []
                }
            
            state = active_trains[train]
            from_id = convert_track(from_track, track_lookup, track_id_lookup)["id"]
            to_id = convert_track(to_track, track_lookup, track_id_lookup)["id"]

            if not state["path"]:
                state["path"].append(from_id)
            state["path"].append(to_id)
            
            train_locations[train] = to_id
            continue

        # --------------------------------
        # END MOVE / END MOVE SU
        # --------------------------------
        m = END_MOVE_SU_RE.match(line)
        if m:
            train, track = m.groups()
            train = _resolve_su(train)
            
            if train in active_trains:
                state = active_trains[train]
                dest_track = convert_track(track, track_lookup, track_id_lookup)["id"]
                
                if not state["path"] and train in train_locations:
                    state["path"] = [train_locations[train], dest_track]
                
                expanded_path = _strip_trailing_zero_length(expand_path(state["path"], a_adj, b_adj, switch_ids))
                duration = compute_move_duration(expanded_path, a_adj, b_adj, switch_costs, get_reversal_duration(train, train_lookup))
                if len(expanded_path) > 1:
                    start_time, end_time = _schedule_move(train, expanded_path, duration)
                    actions.append(
                        create_move_action(
                            train,
                            start_time,
                            end_time,
                            expanded_path,
                            train_lookup,
                            track_id_lookup,
                            unit_lookup
                        )
                    )
                else:
                    _close_hold(train, su_next_free.get(_clock(train), current_time))
                
                train_locations[train] = dest_track
                del active_trains[train]
            continue

        # --------------------------------
        # PARK / PARK SU
        # --------------------------------
        # The corridor models park in one step; the no_bumpers variant merges
        # the parking_fulfill semantics into park_su, so the 4-arg form
        # (su, unit, parking_slot, track) parks a unit in a slot, and the unit
        # then waits on that track until the scenario ends (or its request's
        # departure) before leaving the yard as an OutStanding exit.
        m = PARK_SU_RE.match(line)
        if m:
            train, unit, parking_slot, track = m.groups()
            train = _resolve_su(train)
            track_id = convert_track(track, track_lookup, track_id_lookup)["id"]
            
            if train in active_trains:
                state = active_trains[train]
                
                if not state["path"] and train in train_locations:
                    state["path"] = [train_locations[train], track_id]
                
                expanded_path = _strip_trailing_zero_length(expand_path(state["path"], a_adj, b_adj, switch_ids))
                duration = compute_move_duration(expanded_path, a_adj, b_adj, switch_costs, get_reversal_duration(train, train_lookup))
                if len(expanded_path) > 1:
                    start_time, end_time = _schedule_move(train, expanded_path, duration)
                    actions.append(
                        create_move_action(
                            train,
                            start_time,
                            end_time,
                            expanded_path,
                            train_lookup,
                            track_id_lookup,
                            unit_lookup
                        )
                    )
                else:
                    _close_hold(train, su_next_free.get(_clock(train), current_time))
                
                train_locations[train] = track_id
                del active_trains[train]
            
            train_locations[train] = track_id

            if unit is not None:
                # The no_bumpers park_su parks a specific unit in a specific
                # slot and marks both fulfilled. The unit stands there until the
                # end of the scenario horizon, then exits as OutStanding.
                if parking_slot is not None:
                    waiting_on_messages[train] = parking_slot
                ready_time = su_next_free.get(_clock(train), 0)
                exit_time = max(ready_time, scenario_end_time)
                if exit_time > ready_time:
                    reserve([track_id], ready_time, exit_time)
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
                actions.append(exit_action)
                su_next_free[_clock(train)] = exit_time + 1
                current_time = max(current_time, exit_time + 1)
            continue

        # --------------------------------
        # PARKING FULFILL
        # --------------------------------
        m = PARKING_FULFILL_RE.match(line)
        if m:
            su_id, unit, parking_slot, track = m.groups()
            su_id = _resolve_su(su_id)
            track_id = convert_track(track, track_lookup, track_id_lookup)["id"]
            waiting_on_messages[su_id] = parking_slot
            ready_time = su_next_free.get(_clock(su_id), 0)
            exit_time = max(ready_time, scenario_end_time)
            if exit_time > ready_time:
                reserve([track_id], ready_time, exit_time)
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
            actions.append(exit_action)
            su_next_free[_clock(su_id)] = exit_time + 1
            current_time = max(current_time, exit_time + 1)
            continue

        # --------------------------------
        # DEPART / DEPART SU / DEPART SU FOR REQUEST
        # --------------------------------
        m = (DEPART_SU_RE.match(line)
             or DEPART_SU_FOR_REQUEST_RE.match(line)
             or COMPILED_DEPART_FOR_REQUEST_RE.match(line))
        if m:
            groups = m.groups()
            train = _resolve_su(groups[0])
            track = groups[-1] if len(groups) > 2 else groups[1]

            # Bound here, not inside the branch below: a train that departs
            # without a preceding move — no start_move_su, so nothing in
            # active_trains — otherwise reached the exit_track line with this
            # name unbound and raised UnboundLocalError. Latent until compiled
            # departures started matching; it takes a plan whose first departure
            # belongs to a train that never moved, which
            # Location_KleineBinckhorst produces and the fixture does not.
            expanded_path = []

            if train in active_trains:
                state = active_trains[train]
                
                dest_track = convert_track(track, track_lookup, track_id_lookup)["id"]
                if not state["path"] and train in train_locations:
                    state["path"] = [train_locations[train], dest_track]
                
                if state["path"]:
                    expanded_path = _strip_for_departure(expand_path(state["path"], a_adj, b_adj, switch_ids))
                    duration = compute_move_duration(expanded_path, a_adj, b_adj, switch_costs, get_reversal_duration(train, train_lookup))
                    if len(expanded_path) > 1:
                        start_time, end_time = _schedule_move(train, expanded_path, duration)
                        actions.append(
                            create_move_action(
                                train,
                                start_time,
                                end_time,
                                expanded_path,
                                train_lookup,
                                track_id_lookup,
                                unit_lookup
                            )
                        )
                    else:
                        _close_hold(train, su_next_free.get(_clock(train), current_time))
                
                del active_trains[train]
            
            # Determine departure time: look up from request, but never before
            # the SU is physically ready on the exit track.
            ready_time = su_next_free.get(_clock(train), 0)
            exit_time = ready_time
            dep = None
            if len(groups) >= 4 and not train.startswith("su_request"):
                # Both departure-for-request forms end (…, request, track), so the
                # request is the second-to-last group whether or not the action
                # carries a slot argument. This read groups[4] and called it the
                # request; in the five-group form that is the track, so the lookup
                # always missed and every departure silently used the fallback
                # below. Counting from the right is correct for both.
                req_name_from_action = groups[-2]
                if req_name_from_action in request_lookup:
                    dep = request_lookup[req_name_from_action].get("arrival")
                else:
                    # Fallback: PDDL request names may differ from scenario names,
                    # so use the departure time from the scenario's outgoing requests
                    for req in scenario.get("out", []):
                        dep = req.get("arrival")
                        break
            elif train in su_departure_time:
                dep = su_departure_time[train]
            elif train.startswith("su_request"):
                req_name = "request" + train[10:]
                if req_name in request_lookup:
                    dep = request_lookup[req_name].get("arrival")
            
            if dep is not None:
                dep = int(dep)
                su_departure_deadline[train] = dep
                departing_sus.add(train)
                if dep > exit_time:
                    exit_time = dep
            
            # Use the stripped path's last track for exit location
            exit_track = expanded_path[-1] if len(expanded_path) > 0 else track
            if exit_time > ready_time:
                reserve(
                    [convert_track(exit_track, track_lookup, track_id_lookup)["id"]],
                    ready_time,
                    exit_time,
                )
            exit_action = create_exit_action(
                train,
                exit_time,
                exit_track,
                train_lookup,
                track_lookup,
                unit_lookup,
                track_id_lookup=track_id_lookup
            )
            actions.append(exit_action)
            su_next_free[_clock(train)] = exit_time + 1
            current_time = max(current_time, exit_time + 1)
            continue

        # --------------------------------
        # COUPLE (Combine)
        # --------------------------------
        m = COUPLE_RE.match(line)
        if m:
            su_a, su_b, su_result, unit_a, unit_b, track, slot_a, slot_b, request = m.groups()
            
            track_id = convert_track(track, track_lookup, track_id_lookup)["id"]
            action_loc = track_id
            
            combine_duration = max(
                get_train_duration(su_a, train_lookup, unit_lookup, "combine"),
                get_train_duration(su_b, train_lookup, unit_lookup, "combine")
            )
            
            t_min = max(
                su_next_free.get(_clock(su_a), 0),
                su_next_free.get(_clock(su_b), 0),
            )
            start_time = earliest_start([action_loc], t_min, combine_duration)
            end_time = start_time + combine_duration
            
            combine_actions, combined_members = create_combine_action(
                [su_a, su_b],
                su_result,
                start_time,
                end_time,
                action_loc,
                train_lookup,
                unit_lookup
            )
            actions.extend(combine_actions)
            reserve([action_loc], start_time, end_time + 1)
            
            shunting_unit_composition[su_result] = {
                "memberIDs": combined_members,
                "parentIDs": [su_a, su_b]
            }
            
            req_name = request
            if req_name in request_lookup:
                dep_time = request_lookup[req_name].get("arrival")
                if dep_time is not None:
                    su_departure_time[su_result] = int(dep_time)
            
            for n in (su_a, su_b):
                su_next_free[_clock(n)] = end_time + 1
            su_next_free[_clock(su_result)] = end_time + 1
            train_locations[su_result] = action_loc
            current_time = max(current_time, end_time + 1)
            continue

        # --------------------------------
        # SPLIT (Uncouple)
        # --------------------------------
        m = SPLIT_TWO_RE.match(line) or SPLIT_THREE_RE.match(line)
        if m:
            groups = m.groups()
            
            if len(groups) == 7:
                parent_su, left_su, right_su, unit_a, unit_b, composition, track = groups
                child_ids = [left_su, right_su]
            else:
                parent_su, first_su, second_su, third_su, unit_a, unit_b, unit_c, composition, track = groups
                child_ids = [first_su, second_su, third_su]
            
            track_id = convert_track(track, track_lookup, track_id_lookup)["id"]
            action_loc = track_id
            
            split_duration = get_train_duration(parent_su, train_lookup, unit_lookup, "split")
            
            t_min = su_next_free.get(_clock(parent_su), 0)
            start_time = earliest_start([action_loc], t_min, split_duration)
            end_time = start_time + split_duration
            
            split_action = create_split_action(
                parent_su,
                child_ids,
                start_time,
                end_time,
                action_loc,
                train_lookup,
                unit_lookup
            )
            actions.append(split_action)
            reserve([action_loc], start_time, end_time + 1)
            
            su_next_free[_clock(parent_su)] = end_time + 1
            for child in child_ids:
                su_next_free[_clock(child)] = end_time + 1
            current_time = max(current_time, end_time + 1)
            continue

        # --------------------------------
        # SERVICE
        # --------------------------------
        m = SERVICE_RE.match(line)
        if m:
            su_id, track, pddl_facility = m.groups()
            
            track_id = convert_track(track, track_lookup, track_id_lookup)["id"]
            
            # End active move if the train is currently moving
            if su_id in active_trains:
                state = active_trains[su_id]
                expanded_path = expand_path(state["path"], a_adj, b_adj, switch_ids)
                duration = compute_move_duration(expanded_path, a_adj, b_adj, switch_costs, get_reversal_duration(su_id, train_lookup))
                if len(expanded_path) > 1:
                    start_time, end_time = _schedule_move(su_id, expanded_path, duration)
                    actions.append(
                        create_move_action(
                            su_id,
                            start_time,
                            end_time,
                            expanded_path,
                            train_lookup,
                            track_id_lookup,
                            unit_lookup
                        )
                    )
                else:
                    _close_hold(su_id, su_next_free.get(_clock(su_id), current_time))
                
                train_locations[su_id] = track_id
                del active_trains[su_id]
            
            # Look up facility
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
            
            # TORS computes a service's duration from the scenario task (see
            # ServiceAction::GetDuration), so the plan must schedule with the
            # same value. Look up the matching task on the SU's members and use
            # its duration; fall back to a conservative default when unknown.
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
            t_min = su_next_free.get(_clock(su_id), 0)
            start_time = earliest_start([track_id], t_min, service_duration)
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
            _close_hold(su_id, start_time)
            reserve([track_id], start_time, end_time + 1)
            su_next_free[_clock(su_id)] = end_time + 1
            current_time = max(current_time, end_time + 1)
            continue

        # --------------------------------
        # UNCOUPLE (logical)
        # --------------------------------
        m = UNCOUPLE_RE.match(line)
        if m:
            unit, composition = m.groups()
            continue

        # --------------------------------
        # COMPILED ADOPT COMPOSITION (logical identity transfer)
        # --------------------------------
        m = ADOPT_COMPOSITION_RE.match(line)
        if m:
            source_su, request_su, track = m.groups()
            source_su = _resolve_su(source_su)
            su_identity[request_su] = source_su
            _set_request_departure(source_su, request_su)
            track_id = convert_track(track, track_lookup, track_id_lookup)["id"]

            # The arrived, serviced source SU becomes the departing request SU.
            # No TORS action exists for the transfer itself; what must carry
            # over is everything that makes the request's later moves and Exit
            # describe the real train: its units, its location, and its clock.
            if source_su in train_lookup:
                src = train_lookup[source_su]
                train_lookup[request_su] = {
                    "id": src.get("id", request_su),
                    "members": src.get("members", []),
                    "member_types": src.get("member_types", []),
                    "combine_duration": src.get("combine_duration", COMBINE_DURATION),
                    "split_duration": src.get("split_duration", SPLIT_DURATION),
                }
            if source_su in train_locations:
                train_locations[request_su] = track_id
            if source_su in su_next_free:
                su_next_free[request_su] = su_next_free[source_su]
            if source_su in active_trains:
                active_trains[request_su] = active_trains.pop(source_su)
            continue

        # Nothing matched. This used to fall through to the next line, so an
        # action the converter did not know about simply vanished — which is how
        # the corridor model's compiled departure went missing, taking the two
        # trailing moves with it, while conversion still reported success and
        # emitted a plan that merely stopped early.
        unhandled.append(line)

    if unhandled:
        raise ValueError(
            "convert_to_tors does not recognise these planner actions, so the "
            "plan would be silently truncated. Add a pattern for each, or "
            "confirm it carries no TORS action:\n  " + "\n  ".join(unhandled)
        )

    # OutStanding units matched to a parking slot normally get their Exit from
    # the park_su/parking_fulfill blocks. If a unit was recorded as waiting but
    # no Exit came out of the plan (the park reference never produced one),
    # close it out here at the scenario end. An exit that cannot be placed is
    # reported as infeasible rather than leaving the unit in the yard forever.
    emitted_exit_sus = {
        a["shuntingUnit"]["id"]
        for a in actions
        if a.get("taskType", {}).get("predefined") == "Exit"
        or a.get("taskType", {}).get("predefined") == "StandOut"
    }
    for su_name in list(waiting_on_messages):
        if _as_id(su_name) in emitted_exit_sus:
            continue
        track_id = train_locations.get(su_name)
        if track_id is None:
            problems.append(
                f"INFEASIBLE: parked-out SU {su_name} has no track to exit from."
            )
            continue
        ready_time = su_next_free.get(_clock(su_name), 0)
        exit_time = max(ready_time, scenario_end_time)
        if exit_time > ready_time:
            reserve([track_id], ready_time, exit_time)
        actions.append(
            create_exit_action(
                su_name,
                exit_time,
                track_id,
                train_lookup,
                track_lookup,
                unit_lookup,
                standing_type="OutStanding",
                track_id_lookup=track_id_lookup
            )
        )
        su_next_free[_clock(su_name)] = exit_time + 1
        current_time = max(current_time, exit_time + 1)

    # Assign integer SU IDs to all actions and fix members for combined SUs
    for action in actions:
        su = action["shuntingUnit"]
        old_id = su["id"]
        su["id"] = get_su_id(old_id)
        
        # Fix childIDs/parentIDs to use integer IDs
        su["childIDs"] = [get_su_id(c) for c in su.get("childIDs", [])]
        su["parentIDs"] = [get_su_id(c) for c in su.get("parentIDs", [])]
        
        # For combined SUs (from shunting_unit_composition), set members from composition
        if old_id in shunting_unit_composition:
            comp = shunting_unit_composition[old_id]
            if comp["memberIDs"]:
                su["memberIDs"] = comp["memberIDs"]
            su["parentIDs"] = [get_su_id(p) for p in comp.get("parentIDs", [])]
    
    # Also map train_arrival_times keys. Actions carry integer SU ids produced
    # by get_su_id(_as_id(name)) (make_shunting_unit derives them from the name
    # via _as_id), so the arrival map must use that same integer key or the
    # post-process Arrive actions would never find a scenario arrival time.
    train_arrival_times_int = {}
    for k, v in train_arrival_times.items():
        train_arrival_times_int[get_su_id(_as_id(k))] = v
    
    # Map train_locations keys
    train_locations_int = {}
    for k, v in train_locations.items():
        train_locations_int[get_su_id(_as_id(k))] = v

    # Post-process: Add Arrive actions and calculate Wait periods
    actions = post_process_actions(actions, train_lookup, unit_lookup, track_lookup, 
                                   track_id_lookup, train_locations_int, train_arrival_times_int, scenario, get_su_id,
                                   parkable_tracks)
    
    # Fill in missing members/parentIDs/childIDs for actions that reference SUs
    # by integer ID (e.g., Wait actions created by post_process_actions)
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

    # Ensure empty array fields are present for protobuf parser
    for a in actions:
        su = a["shuntingUnit"]
        if not su.get("parentIDs"):
            su["parentIDs"] = []
        if not su.get("childIDs"):
            su["childIDs"] = []
        if a.get("resources") is None:
            a["resources"] = []

    # Departure-deadline diagnostics: every departing SU's Exit must happen at
    # or before its requested departure time, and that departure must fit
    # within the scenario horizon.
    exit_by_su = {}
    for a in actions:
        if a.get("taskType", {}).get("predefined") == "Exit":
            sid = a["shuntingUnit"]["id"]
            exit_by_su.setdefault(sid, []).append(int(a["startTime"]))
    for su_name, deadline in su_departure_deadline.items():
        # Actions carry integer SU ids produced by get_su_id(_as_id(name)); the
        # remap loop derived them from make_shunting_unit's _as_id, so resolve
        # the deadline SU the same way the remap did.
        sid = get_su_id(_as_id(su_name))
        exit_times = exit_by_su.get(sid, [])
        if not exit_times:
            problems.append(
                f"INFEASIBLE: departing SU {su_name} has departure deadline "
                f"{deadline} but no Exit action was emitted."
            )
            continue
        latest = max(exit_times)
        if latest > deadline:
            problems.append(
                f"INFEASIBLE: SU {su_name} cannot meet its departure deadline "
                f"{deadline}; earliest feasible departure is {latest}."
            )
        if latest > scenario_end_time:
            problems.append(
                f"INFEASIBLE: SU {su_name} departs at {latest}, after the "
                f"scenario end time {scenario_end_time}."
            )

    result = {
        "schemaVersion": SCHEMA_VERSION,
        "actions": actions,
    }
    if problems:
        raise ScheduleInfeasibleError(problems, plan=result)

    return result


def post_process_actions(actions, train_lookup, unit_lookup, track_lookup, 
                         track_id_lookup, train_locations, train_arrival_times, scenario, su_id_fn=None,
                         parkable_tracks=None):
    """Add Arrive actions and Wait periods to make the plan realistic"""
    
    # Track when each shunting unit first appears or moves
    su_first_action = {}
    su_last_position = {}
    
    # Add arrive actions for incoming trains
    processed_actions = []
    
    # Find initial train positions from scenario, mapped to integer SU IDs.
    # Actions carry integer SU ids derived from get_su_id(_as_id(name)), so the
    # position/standing maps must be keyed the same way or the Arrive actions
    # would never find an entry track and every arrival would fall back to the
    # first action's location.
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
    
    # Determine which SU IDs correspond to real scenario trains (in/inStanding).
    # These are the only SUs that may receive an Arrive action. SUs that are
    # merely materialized by the planner (compiled adopt/start/couple request
    # placeholders, combine/split children) never appear in the scenario and
    # must not get a fabricated Arrive.
    scenario_in_su_ids = set()
    if su_id_fn:
        for train in scenario.get("in", []):
            for name in [f"train{train['id']}", f"su_train{train['id']}"]:
                scenario_in_su_ids.add(su_id_fn(_as_id(name)))
        for i in range(len(scenario.get("inStanding", []))):
            for name in [f"train_in_standing_{i}", f"su_train_in_standing_{i}"]:
                scenario_in_su_ids.add(su_id_fn(_as_id(name)))

    # Determine which SU IDs correspond to standing trains
    standing_su_ids = set()
    if su_id_fn:
        for i in range(len(scenario.get("inStanding", []))):
            standing_su_ids.add(su_id_fn(_as_id(f"su_train_in_standing_{i}")))
            standing_su_ids.add(su_id_fn(_as_id(f"train_in_standing_{i}")))
    
    # Determine which SU IDs correspond to outStanding trains
    out_standing_ids = set()
    if su_id_fn:
        for request in scenario.get("outStanding", []):
            key = f"su_outstanding_{request.get('id', '')}"
            out_standing_ids.add(su_id_fn(key))
    
    # Process each action and insert Arrive/Wait actions
    for action in actions:
        cur_su_id = action["shuntingUnit"]["id"]
        
        # If this is the first time we see this SU, add an Arrive action
        # Skip for SUs already created by Combine/Split (already in su_last_position)
        if cur_su_id not in su_first_action and cur_su_id not in su_last_position:
            # Use recorded arrival time if available; otherwise use first action time
            if cur_su_id in train_arrival_times:
                arrive_time = train_arrival_times[cur_su_id]
            else:
                arrive_time = int(action["startTime"])
            
            su_first_action[cur_su_id] = arrive_time
            
            # Only scenario trains (in/inStanding) get an Arrive action. SUs that
            # are not in the scenario were materialized by the planner (compiled
            # adopt/start/couple request placeholders) and already exist on the
            # network, so fabricating an Arrive would create a duplicate train.
            if cur_su_id in scenario_in_su_ids:
                # Determine arrival location
                if cur_su_id in initial_positions:
                    arrive_location = initial_positions[cur_su_id]
                else:
                    arrive_location = action["location"]
                
                # Determine standing type
                standing_type = ""
                if cur_su_id in standing_su_ids:
                    standing_type = "InStanding"
                elif cur_su_id in out_standing_ids:
                    standing_type = "OutStanding"
                
                # Add Arrive action
                if arrive_location in track_id_lookup:
                    resource = track_id_lookup[arrive_location]
                else:
                    resource = convert_track(arrive_location, track_lookup)
                shunting_unit = make_shunting_unit(cur_su_id, train_lookup, unit_lookup)
                arrive_action = {
                    "startTime": _as_time(arrive_time),
                    "endTime": _as_time(arrive_time),
                    # standingType is gone; StandIn says the same thing.
                    "taskType": {"predefined": "StandIn" if standing_type else "Arrive"},
                    "shuntingUnit": shunting_unit,
                    "location": resource["id"],
                    "resources": [resource]
                }
                processed_actions.append(arrive_action)
            

        elif cur_su_id not in su_first_action:
            # SU was created by Combine/Split - just mark it as seen
            su_first_action[cur_su_id] = int(action["startTime"])
        
        # TORS handles action-to-action time gaps internally via its own Wait mechanism.
        
        # Update last position
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
        
        # Handle Combine actions - parent units disappear, child unit appears
        if action["taskType"].get("predefined") == "Combine":
            for child_id in action["shuntingUnit"].get("childIDs", []):
                if child_id not in su_last_position:
                    su_last_position[child_id] = (action["location"], int(action["endTime"]))
        
        # Handle Split actions - parent disappears, children appear
        if action["taskType"].get("predefined") == "Split":
            for child_id in action["shuntingUnit"].get("childIDs", []):
                if child_id not in su_last_position:
                    su_last_position[child_id] = (action["location"], int(action["endTime"]))
        
        processed_actions.append(action)
    
    # Sort actions chronologically by startTime.
    # Arrive actions come first at the same time (logical ordering).
    processed_actions.sort(key=lambda a: (
        int(a["startTime"]),
        0 if a["taskType"].get("predefined") == "Arrive" else 1,
        int(a.get("endTime", "0"))
    ))
    
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
