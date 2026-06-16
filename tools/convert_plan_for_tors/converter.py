import re
import os
import json
from collections import deque
from pathlib import Path


# =====================================================
# REGEX
# =====================================================

START_RE = re.compile(r"start_move\(([^)]+)\)")
START_MOVE_SU_RE = re.compile(r"start_move_su\(([^)]+)\)")
END_RE = re.compile(r"end_move\(([^,]+),\s*([^)]+)\)")
END_MOVE_SU_RE = re.compile(r"end_move_su\(([^,]+),\s*([^)]+)\)")
MOVE_RE = re.compile(
    r"move_(?:aside|bside)_(?:empty|occupied)\(([^,]+),\s*([^,]+),\s*([^)]+)\)"
)
MOVE_SU_RE = re.compile(
    r"move_(?:aside|bside)_(?:empty|occupied)_su\(([^,]+),\s*([^,]+),\s*([^)]+)\)"
)
PARK_RE = re.compile(r"park\(([^,]+),\s*([^)]+)\)")
PARK_SU_RE = re.compile(r"park_su\(([^,]+),\s*([^)]+)\)")
DEPART_RE = re.compile(r"depart_(?:aside|bside)\(([^,]+),\s*([^)]+)\)")
DEPART_SU_RE = re.compile(r"depart_(?:aside|bside)_su\(([^,]+),\s*([^)]+)\)")
DEPART_SU_FOR_REQUEST_RE = re.compile(
    r"depart_(?:aside|bside)_su_for_request\(([^,]+),\s*([^,]+),\s*([^,]+),\s*([^,]+),\s*([^)]+)\)"
)

# New regex patterns for coupling/uncoupling/service actions
COUPLE_RE = re.compile(
    r"couple_two_sus\(([^,]+),\s*([^,]+),\s*([^,]+),\s*([^,]+),\s*([^,]+),\s*([^,]+),\s*([^,]+),\s*([^,]+),\s*([^)]+)\)"
)
SPLIT_TWO_RE = re.compile(
    r"split_two_unit_su\(([^,]+),\s*([^,]+),\s*([^,]+),\s*([^,]+),\s*([^,]+),\s*([^,]+),\s*([^)]+)\)"
)
SPLIT_THREE_RE = re.compile(
    r"split_three_unit_su\(([^,]+),\s*([^,]+),\s*([^,]+),\s*([^,]+),\s*([^,]+),\s*([^,]+),\s*([^,]+),\s*([^,]+),\s*([^)]+)\)"
)
SERVICE_RE = re.compile(r"service_su\(([^,]+),\s*([^,]+),\s*([^)]+)\)")
MATCH_RE = re.compile(r"match\(([^,]+),\s*([^)]+)\)")
ARRIVE_SU_RE = re.compile(r"arrive_su\(([^)]+)\)")
UNCOUPLE_RE = re.compile(r"uncouple\(([^,]+),\s*([^)]+)\)")


MOVE_DURATION = 600
COMBINE_DURATION = 180  # Default, will be overridden by train type data
SPLIT_DURATION = 120    # Default, will be overridden by train type data


# =====================================================
# LOAD LOOKUPS
# =====================================================

def build_train_lookup(scenario):
    """Build lookup for all trains including their types and durations"""
    lookup = {}

    # Incoming trains
    for train in scenario.get("in", {}).get("trains", []):
        names = [f"train{train['id']}", f"su_train{train['id']}"]
        members = []
        
        for member in train.get("members", []):
            tu = member["trainUnit"]
            members.append({
                "id": tu["id"],
                "type": tu["type"]
            })
        
        entry = {
            "id": train["id"],
            "members": members,
            "combine_duration": int(members[0]["type"].get("combineDuration", COMBINE_DURATION)) if members else COMBINE_DURATION,
            "split_duration": int(members[0]["type"].get("splitDuration", SPLIT_DURATION)) if members else SPLIT_DURATION,
        }
        for n in names:
            lookup[n] = entry

    # In standing trains
    for i, train in enumerate(scenario.get("inStanding", {}).get("trains", [])):
        names = [f"train_in_standing_{i}", f"su_train_in_standing_{i}"]
        members = []
        
        for member in train.get("members", []):
            tu = member["trainUnit"]
            members.append({
                "id": tu["id"],
                "type": tu["type"]
            })
        
        entry = {
            "id": train["id"],
            "members": members,
            "combine_duration": int(members[0]["type"].get("combineDuration", COMBINE_DURATION)) if members else COMBINE_DURATION,
            "split_duration": int(members[0]["type"].get("splitDuration", SPLIT_DURATION)) if members else SPLIT_DURATION,
        }
        for n in names:
            lookup[n] = entry

    # Also store under bare train unit IDs for combine/split member resolution
    for key, entry in list(lookup.items()):
        for m in entry["members"]:
            lookup[f"unit{m['id']}"] = {"id": m["id"], "members": [m]}

    return lookup


def build_unit_lookup(scenario):
    """Build lookup for individual train units"""
    lookup = {}
    
    # From incoming trains
    for train in scenario.get("in", {}).get("trains", []):
        for member in train.get("members", []):
            tu = member["trainUnit"]
            lookup[f"unit{tu['id']}"] = {
                "id": tu["id"],
                "type": tu["type"]
            }
    
    # From standing trains
    for train in scenario.get("inStanding", {}).get("trains", []):
        for member in train.get("members", []):
            tu = member["trainUnit"]
            lookup[f"unit{tu['id']}"] = {
                "id": tu["id"],
                "type": tu["type"]
            }
    
    return lookup


def build_request_lookup(scenario):
    """Build lookup for departure requests"""
    lookup = {}
    
    for request in scenario.get("out", {}).get("trainRequests", []):
        request_name = f"request{request['displayName']}"
        lookup[request_name] = {
            "id": request["displayName"],
            "trainUnits": request.get("trainUnits", []),
            "leaveTrackPart": request.get("leaveTrackPart"),
            "lastParkingTrackPart": request.get("lastParkingTrackPart")
        }
    
    return lookup


def build_track_lookup(location):
    """Creates track name to ID mapping"""
    lookup = {}

    for track in location["trackParts"]:
        track_name = track["name"]
        planner_key = "o_" + track_name.lower()
        
        lookup[planner_key] = {
            "name": track_name,
            "trackPartId": track["id"]
        }
        
        # Handle special names like Sein70
        if "sein" in track_name.lower():
            planner_key = track_name.lower()
            lookup[planner_key] = {
                "name": track_name,
                "trackPartId": track["id"]
            }

    return lookup


def build_track_id_lookup(location):
    """Build reverse lookup from track ID to track info"""
    return {
        tp["id"]: {"name": tp["name"], "trackPartId": tp["id"]}
        for tp in location["trackParts"]
    }


# =====================================================
# HELPERS
# =====================================================

def make_shunting_unit(train_id, train_lookup, unit_lookup=None, members=None):
    """Create a shunting unit object"""
    if members:
        return {
            "id": str(train_id),
            "members": members,
            "parentIDs": [],
            "childIDs": [],
            "standingType": ""
        }
    
    if train_id in train_lookup:
        metadata = train_lookup[train_id]
        return {
            "id": str(train_id),
            "members": metadata["members"],
            "parentIDs": [],
            "childIDs": [],
            "standingType": ""
        }
    
    # Handle shunting unit IDs
    if train_id.startswith("su_"):
        # Try to resolve from unit lookup
        unit_id = train_id.replace("su_unit", "")
        if unit_id in unit_lookup:
            return {
                "id": str(train_id),
                "members": [unit_lookup[unit_id]],
                "parentIDs": [],
                "childIDs": [],
                "standingType": ""
            }
    
    return {
        "id": str(train_id),
        "members": [],
        "parentIDs": [],
        "childIDs": [],
        "standingType": ""
    }


def convert_track(track_name, track_lookup):
    """Convert track name to track info"""
    if track_name in track_lookup:
        return track_lookup[track_name]

    cleaned = track_name.replace("o_", "")
    return {
        "name": cleaned,
        "trackPartId": cleaned
    }


def create_move_action(train_id, start, end, path,
                       train_lookup, track_id_lookup, unit_lookup=None):
    """Create a Move action"""
    resources = []
    for p in path:
        resources.append(track_id_lookup.get(p, {
            "name": p,
            "trackPartId": p
        }))

    location = resources[-1]["trackPartId"]
    shunting_unit = make_shunting_unit(train_id, train_lookup, unit_lookup)

    return {
        "startTime": str(start),
        "endTime": str(end),
        "taskType": {
            "predefined": "Move"
        },
        "shuntingUnit": shunting_unit,
        "location": location,
        "resources": resources,
        "trainUnitIds": []
    }


def create_arrive_action(train_id, time, track,
                         train_lookup, track_lookup, unit_lookup=None, 
                         standing_type=""):
    """Create an Arrive action"""
    resource = convert_track(track, track_lookup)
    shunting_unit = make_shunting_unit(train_id, train_lookup, unit_lookup)
    shunting_unit["standingType"] = standing_type

    return {
        "startTime": str(time),
        "endTime": str(time),
        "taskType": {
            "predefined": "Arrive"
        },
        "shuntingUnit": shunting_unit,
        "location": resource["trackPartId"],
        "resources": [resource],
        "trainUnitIds": []
    }


def create_exit_action(train_id, time, track,
                       train_lookup, track_lookup, unit_lookup=None):
    """Create an Exit action"""
    resource = convert_track(track, track_lookup)
    shunting_unit = make_shunting_unit(train_id, train_lookup, unit_lookup)

    return {
        "startTime": str(time),
        "endTime": str(time),
        "taskType": {
            "predefined": "Exit"
        },
        "shuntingUnit": shunting_unit,
        "location": resource["trackPartId"],
        "resources": [resource],
        "trainUnitIds": []
    }


def create_park_action(train_id, time, track,
                       train_lookup, track_lookup, unit_lookup=None):
    """Create a Park action"""
    resource = convert_track(track, track_lookup)
    shunting_unit = make_shunting_unit(train_id, train_lookup, unit_lookup)

    return {
        "startTime": str(time),
        "endTime": str(time),
        "taskType": {
            "predefined": "Park"
        },
        "shuntingUnit": shunting_unit,
        "location": resource["trackPartId"],
        "resources": [resource],
        "trainUnitIds": []
    }


def create_wait_action(train_id, start, end, location,
                       train_lookup, unit_lookup=None):
    """Create a Wait action"""
    shunting_unit = make_shunting_unit(train_id, train_lookup, unit_lookup)

    return {
        "startTime": str(start),
        "endTime": str(end),
        "taskType": {
            "predefined": "Wait"
        },
        "shuntingUnit": shunting_unit,
        "location": location,
        "resources": [],
        "trainUnitIds": []
    }


def create_combine_action(train_ids, result_id, start, end, location,
                          train_lookup, unit_lookup=None):
    """Create Combine actions for coupling"""
    actions = []
    combined_members = []
    
    for train_id in train_ids:
        shunting_unit = make_shunting_unit(train_id, train_lookup, unit_lookup)
        shunting_unit["childIDs"] = [str(result_id)]
        combined_members.extend(shunting_unit["members"])
        
        actions.append({
            "startTime": str(start),
            "endTime": str(end),
            "taskType": {
                "predefined": "Combine"
            },
            "shuntingUnit": shunting_unit,
            "location": location,
            "resources": [],
            "trainUnitIds": []
        })
    
    return actions, combined_members


def create_split_action(train_id, child_ids, start, end, location,
                        train_lookup, unit_lookup=None):
    """Create a Split action"""
    shunting_unit = make_shunting_unit(train_id, train_lookup, unit_lookup)
    shunting_unit["childIDs"] = [str(cid) for cid in child_ids]

    return {
        "startTime": str(start),
        "endTime": str(end),
        "taskType": {
            "predefined": "Split"
        },
        "shuntingUnit": shunting_unit,
        "location": location,
        "resources": [],
        "trainUnitIds": []
    }


def create_service_action(train_id, start, end, location, facility_id,
                          train_lookup, unit_lookup=None):
    """Create a Service action"""
    shunting_unit = make_shunting_unit(train_id, train_lookup, unit_lookup)

    return {
        "startTime": str(start),
        "endTime": str(end),
        "taskType": {
            "other": "Reinigingsperron"  # You might want to make this dynamic
        },
        "shuntingUnit": shunting_unit,
        "location": location,
        "resources": [
            {
                "name": str(facility_id),
                "facilityId": str(facility_id)
            }
        ],
        "trainUnitIds": []
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
# GRAPH/Topology (same as original)
# =====================================================

def build_switch_sets(location):
    return {
        tp["id"]
        for tp in location["trackParts"]
        if tp["type"] in ("Switch", "EnglishSwitch", "Intersection")
    }


def get_all_aside(track_part, switch_like_ids, id_to_tp):
    neighbors = set()
    for nb_id in track_part.get("aSide", []):
        if nb_id in switch_like_ids:
            neighbors.update(
                get_all_aside(id_to_tp[nb_id], switch_like_ids, id_to_tp)
            )
        else:
            neighbors.add(nb_id)
    return neighbors


def get_all_bside(track_part, switch_like_ids, id_to_tp):
    neighbors = set()
    for nb_id in track_part.get("bSide", []):
        if nb_id in switch_like_ids:
            neighbors.update(
                get_all_bside(id_to_tp[nb_id], switch_like_ids, id_to_tp)
            )
        else:
            neighbors.add(nb_id)
    return neighbors


def build_graph(location):
    id_to_tp = {tp["id"]: tp for tp in location["trackParts"]}
    switch_like = build_switch_sets(location)
    graph = {}
    for tp in location["trackParts"]:
        src = tp["id"]
        neighbors = set()
        neighbors |= get_all_aside(tp, switch_like, id_to_tp)
        neighbors |= get_all_bside(tp, switch_like, id_to_tp)
        graph[src] = neighbors
    return graph


def bfs(graph, start, goal):
    queue = deque([[start]])
    visited = set()
    while queue:
        path = queue.popleft()
        node = path[-1]
        if node == goal:
            return path
        if node in visited:
            continue
        visited.add(node)
        for nb in graph.get(node, []):
            queue.append(path + [nb])
    return [start, goal]


def expand_path(path, graph):
    if not path:
        return path
    expanded = [path[0]]
    for i in range(len(path) - 1):
        segment = bfs(graph, path[i], path[i + 1])
        expanded.extend(segment[1:])
    return expanded


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
    graph = build_graph(location)

    current_time = 0
    active_trains = {}
    train_locations = {}  # Track where each train is currently located
    train_arrival_times = {}  # Track when trains arrive
    shunting_unit_composition = {}  # Track composition of shunting units
    actions = []
    
    # SU ID mapping: internal name -> sequential integer ID
    su_name_to_int = {}
    next_su_id = 0
    
    def get_su_id(name):
        nonlocal next_su_id
        if name not in su_name_to_int:
            su_name_to_int[name] = str(next_su_id)
            next_su_id += 1
        return su_name_to_int[name]

    with open(plan_file) as f:
        lines = [line.strip() for line in f if line.strip()]

    for line in lines:
        # --------------------------------
        # MATCH
        # --------------------------------
        m = MATCH_RE.match(line)
        if m:
            # Just record matches, they don't create actions directly
            unit, slot = m.groups()
            continue

        # --------------------------------
        # ARRIVE_SU
        # --------------------------------
        m = ARRIVE_SU_RE.match(line)
        if m:
            su_id = m.group(1)
            train_arrival_times[su_id] = current_time
            
            # Look up entry track from scenario if not already known
            if su_id not in train_locations:
                stripped_id = su_id[3:] if su_id.startswith("su_") else su_id
                for train in scenario.get("in", {}).get("trains", []):
                    if str(train.get("id")) == stripped_id and "entryTrackPart" in train:
                        train_locations[su_id] = str(train["entryTrackPart"])
                        break
            continue

        # --------------------------------
        # START MOVE / START MOVE SU
        # --------------------------------
        m = START_RE.match(line) or START_MOVE_SU_RE.match(line)
        if m:
            train = m.group(1)
            active_trains[train] = {
                "start_time": current_time,
                "path": []
            }
            
            # Look up initial position from scenario if not already known
            if train not in train_locations:
                stripped = train[3:] if train.startswith("su_") else train
                # Check standing trains
                found = False
                for i, standing in enumerate(scenario.get("inStanding", {}).get("trains", [])):
                    for name in [f"train_in_standing_{i}", f"su_train_in_standing_{i}"]:
                        if name == stripped or name == train:
                            if "firstParkingTrackPart" in standing:
                                train_locations[train] = standing["firstParkingTrackPart"]
                            train_arrival_times[train] = current_time
                            found = True
                            break
                    if found:
                        break
                if not found:
                    # Check incoming trains
                    for incoming in scenario.get("in", {}).get("trains", []):
                        if stripped == f"train{incoming['id']}" or train == f"su_train{incoming['id']}":
                            if "entryTrackPart" in incoming:
                                train_locations[train] = incoming["entryTrackPart"]
                            train_arrival_times[train] = current_time
                            break
            
            continue

        # --------------------------------
        # MOVE / MOVE SU
        # --------------------------------
        m = MOVE_RE.match(line) or MOVE_SU_RE.match(line)
        if m:
            train, from_track, to_track = m.groups()
            
            if train not in active_trains:
                active_trains[train] = {
                    "start_time": current_time,
                    "path": []
                }
            
            state = active_trains[train]
            from_id = convert_track(from_track, track_lookup)["trackPartId"]
            to_id = convert_track(to_track, track_lookup)["trackPartId"]

            if not state["path"]:
                state["path"].append(from_id)
            state["path"].append(to_id)
            
            train_locations[train] = to_id
            continue

        # --------------------------------
        # END MOVE / END MOVE SU
        # --------------------------------
        m = END_RE.match(line) or END_MOVE_SU_RE.match(line)
        if m:
            train, track = m.groups()
            
            if train in active_trains:
                state = active_trains[train]
                end_time = current_time + MOVE_DURATION
                dest_track = convert_track(track, track_lookup)["trackPartId"]
                
                if not state["path"] and train in train_locations:
                    state["path"] = [train_locations[train], dest_track]
                
                expanded_path = expand_path(state["path"], graph)
                if len(expanded_path) > 1:
                    actions.append(
                        create_move_action(
                            train,
                            state["start_time"],
                            end_time,
                            expanded_path,
                            train_lookup,
                            track_id_lookup,
                            unit_lookup
                        )
                    )
                
                train_locations[train] = dest_track
                current_time = end_time
                del active_trains[train]
            continue

        # --------------------------------
        # PARK / PARK SU
        # --------------------------------
        m = PARK_RE.match(line) or PARK_SU_RE.match(line)
        if m:
            train, track = m.groups()
            track_id = convert_track(track, track_lookup)["trackPartId"]
            
            if train in active_trains:
                state = active_trains[train]
                current_time += MOVE_DURATION
                
                if not state["path"] and train in train_locations:
                    state["path"] = [train_locations[train], track_id]
                
                expanded_path = expand_path(state["path"], graph)
                if len(expanded_path) > 1:
                    actions.append(
                        create_move_action(
                            train,
                            state["start_time"],
                            current_time,
                            expanded_path,
                            train_lookup,
                            track_id_lookup,
                            unit_lookup
                        )
                    )
                
                train_locations[train] = track_id
                del active_trains[train]
            
            exit_action = create_exit_action(
                train,
                current_time,
                track,
                train_lookup,
                track_lookup,
                unit_lookup
            )
            exit_action["shuntingUnit"]["standingType"] = "OutStanding"
            actions.append(exit_action)
            
            train_locations[train] = track_id
            continue

        # --------------------------------
        # DEPART / DEPART SU / DEPART SU FOR REQUEST
        # --------------------------------
        m = DEPART_RE.match(line) or DEPART_SU_RE.match(line) or DEPART_SU_FOR_REQUEST_RE.match(line)
        if m:
            groups = m.groups()
            train = groups[0]
            track = groups[-1] if len(groups) > 2 else groups[1]
            
            if train in active_trains:
                state = active_trains[train]
                current_time += MOVE_DURATION
                
                dest_track = convert_track(track, track_lookup)["trackPartId"]
                if not state["path"] and train in train_locations:
                    state["path"] = [train_locations[train], dest_track]
                
                if state["path"]:
                    expanded_path = expand_path(state["path"], graph)
                    if len(expanded_path) > 1:
                        actions.append(
                            create_move_action(
                                train,
                                state["start_time"],
                                current_time,
                                expanded_path,
                                train_lookup,
                                track_id_lookup,
                                unit_lookup
                            )
                        )
                
                del active_trains[train]
            
            exit_action = create_exit_action(
                train,
                current_time,
                track,
                train_lookup,
                track_lookup,
                unit_lookup
            )
            exit_action["shuntingUnit"]["standingType"] = "OutStanding"
            actions.append(exit_action)
            continue

        # --------------------------------
        # COUPLE (Combine)
        # --------------------------------
        m = COUPLE_RE.match(line)
        if m:
            su_a, su_b, su_result, unit_a, unit_b, track, slot_a, slot_b, request = m.groups()
            
            # Get the location from track
            track_id = convert_track(track, track_lookup)["trackPartId"]
            action_loc = track_id
            
            # Determine combine duration based on train types
            combine_duration = max(
                get_train_duration(su_a, train_lookup, unit_lookup, "combine"),
                get_train_duration(su_b, train_lookup, unit_lookup, "combine")
            )
            
            start_time = current_time
            end_time = current_time + combine_duration
            
            # Create combine actions for both parent units
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
            
            # Record the combined shunting unit
            shunting_unit_composition[su_result] = {
                "members": combined_members,
                "parentIDs": [su_a, su_b]
            }
            
            train_locations[su_result] = action_loc
            current_time = end_time
            continue

        # --------------------------------
        # SPLIT (Uncouple)
        # --------------------------------
        m = SPLIT_TWO_RE.match(line) or SPLIT_THREE_RE.match(line)
        if m:
            groups = m.groups()
            
            if len(groups) == 7:  # split_two_unit_su
                parent_su, left_su, right_su, unit_a, unit_b, composition, track = groups
                child_ids = [left_su, right_su]
            else:  # split_three_unit_su
                parent_su, first_su, second_su, third_su, unit_a, unit_b, unit_c, composition, track = groups
                child_ids = [first_su, second_su, third_su]
            
            track_id = convert_track(track, track_lookup)["trackPartId"]
            action_loc = track_id
            
            # Get split duration
            split_duration = get_train_duration(parent_su, train_lookup, unit_lookup, "split")
            
            start_time = current_time
            end_time = current_time + split_duration
            
            # Create split action
            split_action = create_split_action(
                parent_su,
                [cid.replace("su_", "") for cid in child_ids],
                start_time,
                end_time,
                action_loc,
                train_lookup,
                unit_lookup
            )
            actions.append(split_action)
            
            current_time = end_time
            continue

        # --------------------------------
        # SERVICE
        # --------------------------------
        m = SERVICE_RE.match(line)
        if m:
            su_id, track, facility = m.groups()
            
            track_id = convert_track(track, track_lookup)["trackPartId"]
            
            # End active move if the train is currently moving
            if su_id in active_trains:
                state = active_trains[su_id]
                current_time += MOVE_DURATION
                
                actions.append(
                    create_move_action(
                        su_id,
                        state["start_time"],
                        current_time,
                        expand_path(state["path"], graph),
                        train_lookup,
                        track_id_lookup,
                        unit_lookup
                    )
                )
                
                train_locations[su_id] = track_id
                del active_trains[su_id]
            
            # Find facility ID from location
            facility_id = "72"  # Default Reinigingsperron ID
            
            # Look up facility ID from location data
            for fac in location.get("facilities", []):
                if track_id in [str(tp) for tp in fac.get("relatedTrackParts", [])]:
                    facility_id = fac["id"]
                    break
            
            # Service duration - look up from scenario
            service_duration = 900  # Default
            
            start_time = current_time
            end_time = current_time + service_duration
            
            # Create service action
            service_action = create_service_action(
                su_id,
                start_time,
                end_time,
                track_id,
                facility_id,
                train_lookup,
                unit_lookup
            )
            actions.append(service_action)
            
            current_time = end_time
            continue

        # --------------------------------
        # UNCOUPLE (logical)
        # --------------------------------
        m = UNCOUPLE_RE.match(line)
        if m:
            # Logical uncouple doesn't create a physical action in the JSON plan
            unit, composition = m.groups()
            continue

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
            if comp["members"]:
                su["members"] = comp["members"]
            su["parentIDs"] = [get_su_id(p) for p in comp.get("parentIDs", [])]
    
    # Also map train_arrival_times keys
    train_arrival_times_int = {}
    for k, v in train_arrival_times.items():
        train_arrival_times_int[get_su_id(k)] = v
    
    # Map train_locations keys
    train_locations_int = {}
    for k, v in train_locations.items():
        train_locations_int[get_su_id(k)] = v

    # Post-process: Add Arrive actions and calculate Wait periods
    actions = post_process_actions(actions, train_lookup, unit_lookup, track_lookup, 
                                   train_locations_int, train_arrival_times_int, scenario, get_su_id)
    
    # Fill in missing members/parentIDs/childIDs for actions that reference SUs
    # by integer ID (e.g., Wait actions created by post_process_actions)
    su_fill = {}
    for a in actions:
        su = a["shuntingUnit"]
        sid = su["id"]
        if su["members"] and sid not in su_fill:
            su_fill[sid] = {
                "members": su["members"],
                "parentIDs": su.get("parentIDs", []),
                "childIDs": su.get("childIDs", [])
            }
    for a in actions:
        su = a["shuntingUnit"]
        sid = su["id"]
        if sid in su_fill:
            if not su["members"]:
                su["members"] = su_fill[sid]["members"]
            if not su.get("parentIDs", []):
                su["parentIDs"] = su_fill[sid]["parentIDs"]
            if not su.get("childIDs", []):
                su["childIDs"] = su_fill[sid]["childIDs"]

    # Clean up empty fields that TORS protobuf parser may choke on
    for a in actions:
        su = a["shuntingUnit"]
        if not su.get("parentIDs", []):
            su.pop("parentIDs", None)
        if not su.get("childIDs", []):
            su.pop("childIDs", None)
        if not su.get("standingType", ""):
            su.pop("standingType", None)
        if not a.get("trainUnitIds", []):
            a.pop("trainUnitIds", None)
        if not a.get("resources", []):
            a.pop("resources", None)

    return {
        "actions": actions,
    }


def post_process_actions(actions, train_lookup, unit_lookup, track_lookup, 
                         train_locations, train_arrival_times, scenario, su_id_fn=None):
    """Add Arrive actions and Wait periods to make the plan realistic"""
    
    # Track when each shunting unit first appears or moves
    su_first_action = {}
    su_last_position = {}
    
    # Add arrive actions for incoming trains
    processed_actions = []
    
    # Find initial train positions from scenario, mapped to integer SU IDs
    initial_positions = {}
    if su_id_fn:
        for train in scenario.get("in", {}).get("trains", []):
            for name in [f"train{train['id']}", f"su_train{train['id']}"]:
                if "entryTrackPart" in train:
                    initial_positions[su_id_fn(name)] = train["entryTrackPart"]
        
        for i, train in enumerate(scenario.get("inStanding", {}).get("trains", [])):
            for name in [f"train_in_standing_{i}", f"su_train_in_standing_{i}"]:
                if "firstParkingTrackPart" in train:
                    initial_positions[su_id_fn(name)] = train["firstParkingTrackPart"]
                elif "entryTrackPart" in train:
                    initial_positions[su_id_fn(name)] = train["entryTrackPart"]
    
    # Determine which SU IDs correspond to standing trains
    standing_su_ids = set()
    if su_id_fn:
        for i in range(len(scenario.get("inStanding", {}).get("trains", []))):
            standing_su_ids.add(su_id_fn(f"su_train_in_standing_{i}"))
            standing_su_ids.add(su_id_fn(f"train_in_standing_{i}"))
    
    # Determine which SU IDs correspond to outStanding trains
    out_standing_ids = set()
    if su_id_fn:
        for request in scenario.get("outStanding", {}).get("trainRequests", []):
            key = f"su_outstanding_{request.get('displayName', '')}"
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
            arrive_action = {
                "startTime": str(arrive_time),
                "endTime": str(arrive_time),
                "taskType": {"predefined": "Arrive"},
                "shuntingUnit": dict(action["shuntingUnit"]),
                "location": arrive_location,
                "resources": [{"name": arrive_location, "trackPartId": arrive_location}],
                "trainUnitIds": []
            }
            arrive_action["shuntingUnit"]["standingType"] = standing_type
            processed_actions.append(arrive_action)
            
            # Add Wait if arrival time is before the actual action start
            if arrive_time < int(action["startTime"]):
                wait_action = create_wait_action(
                    cur_su_id,
                    arrive_time,
                    int(action["startTime"]),
                    arrive_location,
                    train_lookup,
                    unit_lookup
                )
                processed_actions.append(wait_action)
        elif cur_su_id not in su_first_action:
            # SU was created by Combine/Split - just mark it as seen
            su_first_action[cur_su_id] = int(action["startTime"])
        
        # Check if there's a gap between last position and current action
        action_time = int(action["startTime"])
        if cur_su_id in su_last_position:
            last_loc, last_time = su_last_position[cur_su_id]
            
            if action_time > last_time and action["taskType"].get("predefined") not in ["Arrive"]:
                # Add Wait action
                wait_action = create_wait_action(
                    cur_su_id,
                    last_time,
                    action_time,
                    last_loc,
                    train_lookup,
                    unit_lookup
                )
                processed_actions.append(wait_action)
        
        # Update last position
        if "location" in action:
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
    
    return processed_actions


# =====================================================
# SCENARIO CONVERTER (HIP → TORS)
# =====================================================

def make_type_name(type_obj):
    """Build a compound display name like 'SLT-4' from a type object."""
    display = type_obj.get("displayName", "Unknown")
    carriages = type_obj.get("carriages", "")
    return f"{display}-{carriages}"


def collect_type_from_member(member, type_registry):
    """Extract type info from a HIP-format member and register it."""
    tu = member if "trainUnit" not in member else member["trainUnit"]
    t = tu.get("type", {})
    type_name = make_type_name(t)
    if type_name not in type_registry:
        entry = {
            "displayName": type_name,
            "carriages": t.get("carriages", 0),
            "length": t.get("length", 0.0),
            "combineDuration": str(t.get("combineDuration", "180")),
            "splitDuration": str(t.get("splitDuration", "120")),
            "backNormTime": t.get("backNormTime", "0"),
            "backAdditionTime": t.get("backAdditionTime", "0"),
            "travelSpeed": "10",
            "typePrefix": t.get("displayName", ""),
            "needsElectricity": True,
            "startUpTime": "0",
            "needsLoco": False,
            "isLoco": False,
            "idPrefix": 0,
        }
        type_registry[type_name] = entry


def convert_member_to_tors(member):
    """Convert a HIP-format train member to TORS-format TrainUnit."""
    tu = member if "trainUnit" not in member else member["trainUnit"]
    tasks = member.get("tasks", [])
    # Convert tasks to TORS format if needed
    tors_tasks = []
    for task in tasks:
        tors_task = {
            "type": task["type"],
            "priority": task.get("priority", 1),
            "duration": task.get("duration", "0"),
        }
        if "requiredSkills" in task:
            tors_task["requiredSkills"] = task["requiredSkills"]
        tors_tasks.append(tors_task)

    return {
        "id": str(tu.get("id", "")),
        "typeDisplayName": make_type_name(tu.get("type", {})),
        "tasks": tors_tasks,
    }


def convert_incoming_train(hip_train, type_registry):
    """Convert a HIP-format incoming train to TORS-format train."""
    for m in hip_train.get("members", []):
        collect_type_from_member(m, type_registry)
    return {
        "time": str(hip_train.get("arrival", "0")),
        "id": str(hip_train.get("id", "")),
        "sideTrackPart": str(hip_train.get("entryTrackPart", "")),
        "parkingTrackPart": str(hip_train.get("firstParkingTrackPart", "")),
        "members": [convert_member_to_tors(m) for m in hip_train.get("members", [])],
        "canDepartFromAnyTrack": True,
        "standingIndex": 1.0,
        "minimumDuration": "60",
    }


def convert_outgoing_train(hip_request, type_registry):
    """Convert a HIP-format outgoing request to TORS-format train."""
    for tu in hip_request.get("trainUnits", []):
        collect_type_from_member({"trainUnit": {"id": tu.get("id", ""), "type": tu.get("type", {})}}, type_registry)
    return {
        "time": str(hip_request.get("departure", "0")),
        "id": str(hip_request.get("displayName", "")),
        "sideTrackPart": str(hip_request.get("leaveTrackPart", "")),
        "parkingTrackPart": str(hip_request.get("lastParkingTrackPart", "")),
        "members": [
            {
                "id": str(tu["id"]) if tu.get("id") else "****",
                "typeDisplayName": make_type_name(tu.get("type", {})),
                "tasks": [],
            }
            for tu in hip_request.get("trainUnits", [])
        ],
        "canDepartFromAnyTrack": False,
        "standingIndex": 0.0,
        "minimumDuration": "60",
    }


def convert_scenario_for_tors(scenario_file, location_file):
    """Convert a HIP/Solver-format scenario to TORS-format scenario."""
    with open(scenario_file) as f:
        hip = json.load(f)

    type_registry = {}

    tors = {}

    # Convert in (arriving trains)
    in_data = hip.get("in", {})
    if isinstance(in_data, dict):
        tors["in"] = [convert_incoming_train(t, type_registry) for t in in_data.get("trains", [])]
    elif isinstance(in_data, list):
        tors["in"] = in_data  # Already in TORS format

    # Convert out (departing requests)
    out_data = hip.get("out", {})
    if isinstance(out_data, dict):
        tors["out"] = [convert_outgoing_train(t, type_registry) for t in out_data.get("trainRequests", [])]
    elif isinstance(out_data, list):
        tors["out"] = out_data  # Already in TORS format

    # Convert inStanding
    inst_data = hip.get("inStanding", {})
    if isinstance(inst_data, dict):
        tors["inStanding"] = [convert_incoming_train(t, type_registry) for t in inst_data.get("trains", [])]
        # Override time to 0 for standing trains
        for t in tors["inStanding"]:
            t["time"] = "0"
    elif isinstance(inst_data, list):
        tors["inStanding"] = inst_data  # Already in TORS format
    else:
        tors["inStanding"] = []

    # Convert outStanding
    outst_data = hip.get("outStanding", {})
    if isinstance(outst_data, dict):
        tors["outStanding"] = [convert_outgoing_train(t, type_registry) for t in outst_data.get("trainRequests", [])]
        # Override time to 0 for standing trains
        for t in tors["outStanding"]:
            t["time"] = "0"
    elif isinstance(outst_data, list):
        tors["outStanding"] = outst_data  # Already in TORS format
    else:
        tors["outStanding"] = []

    # Build trainUnitTypes from collected type registry
    tors["trainUnitTypes"] = list(type_registry.values())

    # Top-level fields
    tors["startTime"] = str(hip.get("startTime", "0"))
    tors["endTime"] = str(hip.get("endTime", "0"))

    # Optional fields with empty defaults
    tors["nonServiceTraffic"] = hip.get("nonServiceTraffic", [])
    tors["disabledTrackPart"] = hip.get("disabledTrackPart", [])
    tors["workers"] = hip.get("workers", [])

    return tors


# =====================================================
# MAIN
# =====================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Convert PDDL plans and scenarios to TORS JSON format")
    parser.add_argument("--plan", required=True, help="Path to the .plan file")
    parser.add_argument("--scenario", required=True, help="Path to the scenario JSON file")
    parser.add_argument("--location", required=True, help="Path to the location JSON file")
    parser.add_argument("--output", required=True, help="Path to write the output plan JSON")
    parser.add_argument("--output-scenario", required=False, default=None,
                        help="Path to write the TORS-format scenario JSON (optional)")
    args = parser.parse_args()

    result = convert_plan(args.plan, args.scenario, args.location)

    with open(args.output, "w") as f:
        json.dump(result, f, indent=4)

    print("Plan JSON generated:", args.output)

    if args.output_scenario:
        tors_scenario = convert_scenario_for_tors(args.scenario, args.location)
        with open(args.output_scenario, "w") as f:
            json.dump(tors_scenario, f, indent=4)
        print("Scenario JSON generated:", args.output_scenario)