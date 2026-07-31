import argparse
import base64
import json
import re
from pathlib import Path


ACTION_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)?:\s*)?\(([^)]+)\)")
CALL_RE = re.compile(r"^\s*([A-Za-z_][\w-]*)\((.*)\)\s*$")


def load_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def sanitize_pddl_name(name):
    text = str(name).replace("-", "_")
    if not text:
        return text
    if text[0].isdigit():
        return "o_" + text
    return text


def unsanitize_track_token(token):
    return token[2:] if token.startswith("o_") else token


def build_track_maps(location):
    tracks = location.get("trackParts", [])
    id_to_track = {str(track["id"]): track for track in tracks}
    name_to_track = {str(track["name"]): track for track in tracks}
    return id_to_track, name_to_track


def to_track_id(token, id_to_track, name_to_track):
    token = str(token)
    if token in id_to_track:
        return token
    track = name_to_track.get(token)
    if track:
        return str(track["id"])
    stripped = unsanitize_track_token(token)
    if stripped != token:
        return to_track_id(stripped, id_to_track, name_to_track)
    return token


def track_name(track_id, id_to_track):
    track = id_to_track.get(str(track_id))
    return str(track["name"]) if track else str(track_id)


def build_edges(location, id_to_track):
    edges = []
    seen = set()
    for track in location.get("trackParts", []):
        src = str(track["id"])
        for nb_id in track.get("aSide", []):
            nb = id_to_track.get(str(nb_id))
            if not nb:
                continue
            tgt = str(nb["id"])
            key = tuple(sorted([src, tgt]))
            if key not in seen:
                edges.append({"source": src, "sourceSide": "a", "target": tgt, "targetSide": "b"})
                seen.add(key)
        for nb_id in track.get("bSide", []):
            nb = id_to_track.get(str(nb_id))
            if not nb:
                continue
            tgt = str(nb["id"])
            key = tuple(sorted([src, tgt]))
            if key not in seen:
                edges.append({"source": src, "sourceSide": "b", "target": tgt, "targetSide": "a"})
                seen.add(key)
    return edges


def parse_plan(path, id_to_track=None):
    if str(path).lower().endswith(".json"):
        return parse_solver_plan(path, id_to_track)
    steps = []
    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            match = ACTION_RE.match(line)
            if match:
                parts = match.group(1).split()
            else:
                call_match = CALL_RE.match(line)
                if not call_match:
                    continue
                parts = [call_match.group(1)] + [
                    item.strip() for item in call_match.group(2).split(",") if item.strip()
                ]
            if not parts:
                continue
            steps.append({"raw": line, "action": parts[0].lower(), "args": parts[1:]})
    return steps


def task_type_name(action):
    task_type = action.get("taskType", {})
    if "predefined" in task_type:
        return task_type["predefined"]
    if "other" in task_type:
        return task_type["other"]
    return str(task_type)


def action_track(action):
    resources = action.get("resources", [])
    for resource in reversed(resources):
        if resource.get("trackPartId"):
            return str(resource["trackPartId"])
    if action.get("location") is not None:
        return str(action["location"])
    return None


def action_path_resources(action):
    resources = action.get("resources", [])
    return [str(r["trackPartId"]) for r in resources if r.get("trackPartId") is not None]


def parse_solver_plan(path, id_to_track=None):
    plan = load_json(path)
    actions = sorted(
        plan.get("actions", []),
        key=lambda a: (int(a.get("startTime", 0)), int(a.get("endTime", 0))),
    )

    def display(track_id):
        return track_name(track_id, id_to_track) if id_to_track else str(track_id)

    steps = []
    for action in actions:
        task_name = task_type_name(action)
        members = action.get("shuntingUnit", {}).get("members", [])
        if members:
            train = "+".join(str(m["id"]) for m in members)
        else:
            train = "su_" + str(action.get("shuntingUnit", {}).get("id", "unknown"))
        track = action_track(action)
        path_raw = action_path_resources(action)
        if task_name == "Wait":
            steps.append({"raw": f"{action.get('startTime')}..{action.get('endTime')}: Wait {train}", "action": "wait", "args": [train]})
        elif task_name == "Combine":
            child_ids = action.get("shuntingUnit", {}).get("childIDs", [])
            combined_train = train
            for cid in child_ids:
                for a in actions:
                    if a.get("shuntingUnit", {}).get("id") == cid:
                        cm = a.get("shuntingUnit", {}).get("members", [])
                        if cm:
                            combined_train = "+".join(str(m["id"]) for m in cm)
                            break
            args = [combined_train]
            if track:
                args.append(track)
            steps.append({"raw": f"{action.get('startTime')}..{action.get('endTime')}: Combine {combined_train}", "action": "combine", "args": args})
        elif task_name == "Split":
            children = []
            for cid in action.get("shuntingUnit", {}).get("childIDs", []):
                for a in actions:
                    if a.get("shuntingUnit", {}).get("id") == cid:
                        cm = a.get("shuntingUnit", {}).get("members", [])
                        if cm:
                            children.append("+".join(str(m["id"]) for m in cm))
                            break
            args = [train]
            if track:
                args.append(track)
            label = f"Split {train}"
            if children:
                label += " \u2192 " + ", ".join(children)
            steps.append({"raw": f"{action.get('startTime')}..{action.get('endTime')}: {label}", "action": "split", "args": args, "children": children})
        elif not track:
            continue
        elif task_name == "Move":
            steps.append({"raw": f"{action.get('startTime')}..{action.get('endTime')}: Move {train} \u2192 {display(track)}", "action": "move_to", "args": [train, track], "path": path_raw})
        elif task_name == "Arrive":
            steps.append({"raw": f"{action.get('startTime')}: Arrive {train} @ {display(track)}", "action": "arrive", "args": [train, track], "path": path_raw})
        elif task_name == "Exit":
            standing_type = action.get("shuntingUnit", {}).get("standingType", "")
            action_name = "park" if standing_type == "OutStanding" else "depart"
            label = "Park" if action_name == "park" else "Depart"
            steps.append({"raw": f"{action.get('startTime')}: {label} {train} @ {display(track)}", "action": action_name, "args": [train, track], "path": path_raw})
        else:
            steps.append({"raw": f"{action.get('startTime')}..{action.get('endTime')}: {task_name} {train} @ {display(track)}", "action": "service", "args": [train, track], "path": path_raw})
    return steps


def entry_side_of(track_id, neighbor_id, location):
    """Which side of `track_id` connects to `neighbor_id` ('a' or 'b', None if unknown)."""
    track_id = str(track_id)
    neighbor_id = str(neighbor_id)
    for part in location.get("trackParts", []):
        if str(part.get("id")) != track_id:
            continue
        if neighbor_id in part.get("aSide", []):
            return "a"
        if neighbor_id in part.get("bSide", []):
            return "b"
        return None
    return None


def entry_side_from_path(raw_path, target, location):
    """Entry side of `target` given a path list whose last hop is <neighbor> -> <target>."""
    if raw_path and len(raw_path) >= 2:
        return entry_side_of(target, raw_path[-2], location)
    return None


def initial_train_positions(scenario, id_to_track):
    trains = {}

    def member_name(train):
        members = train.get("members", [])
        if members:
            return "+".join(str(m["trainUnit"]["id"]) for m in members)
        return "train" + str(train["id"])

    # Only include trains already standing in the yard at t=0
    # Incoming trains (section "in") are NOT in the yard yet — they appear via Arrive actions
    for train in scenario.get("inStanding", {}).get("trains", []):
        track_id = train.get("firstParkingTrackPart") or train.get("entryTrackPart")
        if track_id and str(track_id) in id_to_track:
            trains[member_name(train)] = {
                "track": str(track_id),
                "status": "active",
                "restSide": "b",
            }
    return trains


def dedupe_consecutive(raw_path):
    seen = []
    for t in raw_path:
        t = str(t)
        if t and (not seen or t != seen[-1]):
            seen.append(t)
    return seen


def member_lengths_from_scenario(scenario):
    lengths = {}

    def add_unit(unit):
        if not isinstance(unit, dict):
            return
        unit_id = str(unit.get("id", ""))
        length = unit.get("type", {}).get("length")
        if unit_id and length:
            lengths[unit_id] = float(length)

    def add_train(train):
        if not isinstance(train, dict):
            return
        for member in train.get("members", []) or []:
            add_unit(member.get("trainUnit"))

    if isinstance(scenario, dict):
        in_trains = scenario.get("in", {}).get("trains", []) if isinstance(scenario.get("in"), dict) else []
        standing = scenario.get("inStanding", {})
        standing_trains = standing.get("trains", []) if isinstance(standing, dict) else standing
        out_trains = scenario.get("out", {}).get("trainRequests", []) if isinstance(scenario.get("out"), dict) else []
        for train in list(in_trains) + list(standing_trains) + list(out_trains):
            add_train(train)
        for unit in scenario.get("trainUnitTypes", []) or []:
            add_unit(unit)
    return lengths


def member_lengths_from_plan(plan_path):
    lengths = {}
    if not plan_path or not Path(plan_path).exists():
        return lengths
    if not str(plan_path).lower().endswith(".json"):
        return lengths
    plan = load_json(plan_path)
    for action in plan.get("actions", []) or []:
        members = action.get("shuntingUnit", {}).get("members", []) or []
        for member in members:
            unit_id = str(member.get("id", ""))
            length = member.get("type", {}).get("length")
            if unit_id and length:
                lengths[unit_id] = float(length)
    return lengths


def collect_train_lengths(scenario, plan_path, states):
    """Map train name -> total length (m). Names are '+' -joined member ids."""
    member_lengths = {}
    member_lengths.update(member_lengths_from_scenario(scenario))
    member_lengths.update(member_lengths_from_plan(plan_path))
    if not member_lengths:
        return {}
    names = set()
    for state in states:
        names.update(state.get("trains", {}).keys())
    result = {}
    for name in names:
        total = sum(member_lengths.get(part, 0) for part in str(name).split("+"))
        if total > 0:
            result[name] = total
    return result


def simulate_steps(initial_trains, steps, id_to_track, location=None):
    states = [{"index": 0, "action": "initial", "action_type": "initial", "train": None, "raw": "Initial state", "trains": json.loads(json.dumps(initial_trains))}]
    trains = json.loads(json.dumps(initial_trains))

    def land(train, target, status="active"):
        prev_track = trains.get(train, {}).get("track")
        tid = to_track_id(target, id_to_track, {})
        entry = entry_side_from_path(raw_path, tid, location)
        if entry is None and location and prev_track and prev_track != tid:
            entry = entry_side_of(tid, prev_track, location)
        trains.setdefault(train, {"track": None, "status": "active"})
        trains[train]["track"] = tid
        trains[train]["status"] = status
        rest = {"a": "b", "b": "a"}.get(entry)
        if rest:
            trains[train]["restSide"] = rest
        return tid

    for index, step in enumerate(steps, start=1):
        action = step["action"]
        args = step["args"]
        label = step["raw"]
        involved_train = args[0] if args else None
        raw_path = step.get("path")

        if raw_path:
            train_path = {involved_train: dedupe_consecutive(raw_path)}
        else:
            train_path = {}

        if action == "move" and len(args) >= 3:
            train, source, target = args[:3]
            land(train, target, "active")
            action_type = "move"
        elif action == "move_to" and len(args) >= 2:
            train, target = args[:2]
            land(train, target, "active")
            action_type = "move"
        elif action == "arrive" and len(args) >= 2:
            train, target = args[:2]
            land(train, target, "active")
            action_type = "arrive"
        elif action == "park" and len(args) >= 2:
            train, track = args[:2]
            land(train, track, "parked")
            action_type = "park"
            if "+" in train:
                for member_id in train.split("+"):
                    if member_id in trains:
                        trains[member_id]["track"] = to_track_id(track, id_to_track, {})
                        trains[member_id]["status"] = "parked"
                        if trains[train].get("restSide"):
                            trains[member_id]["restSide"] = trains[train]["restSide"]
        elif action == "depart" and len(args) >= 2:
            train, track = args[:2]
            land(train, track, "departed")
            action_type = "depart"
            if "+" in train:
                for member_id in train.split("+"):
                    if member_id in trains:
                        trains[member_id]["status"] = "departed"
        elif action == "combine" and len(args) >= 1:
            train = args[0]
            track = args[1] if len(args) >= 2 else None
            if "+" in train:
                members = train.split("+")
                if track is None:
                    for m in members:
                        if m in trains and trains[m].get("track"):
                            track = trains[m]["track"]
                            break
                track = to_track_id(track, id_to_track, {}) if track else None
                side = next((trains[m].get("restSide") for m in members if m in trains and trains[m].get("restSide")), None)
                trains[train] = {"track": track, "status": "combined"}
                if side:
                    trains[train]["restSide"] = side
                for m in members:
                    if m in trains:
                        trains[m]["status"] = "absorbed"
            elif train in trains:
                trains[train]["status"] = "combined"
            action_type = "combine"
        elif action == "split" and len(args) >= 1:
            parent = args[0]
            track = args[1] if len(args) >= 2 else None
            combined = trains.pop(parent, None)
            if combined is None and "+" in parent:
                parent_set = set(parent.split("+"))
                for key in list(trains):
                    if "+" in key and set(key.split("+")) == parent_set:
                        combined = trains.pop(key)
                        break
            if track is None and combined:
                track = combined.get("track")
            track = to_track_id(track, id_to_track, {}) if track else None
            children = step.get("children") or (parent.split("+") if "+" in parent else [parent])
            for child in children:
                trains[child] = {"track": track, "status": "active"}
                if combined and combined.get("restSide"):
                    trains[child]["restSide"] = combined["restSide"]
            action_type = "split"
        elif action in ("move_aside_empty", "move_aside_occupied",
                        "move_bside_empty", "move_bside_occupied") and len(args) >= 3:
            train, source, target = args[:3]
            land(train, target, "active")
            action_type = "move"
        elif action in ("depart_aside", "depart_bside") and len(args) >= 2:
            train, track = args[:2]
            land(train, track, "departed")
            action_type = "depart"
        elif action == "service" and len(args) >= 2:
            train, target = args[:2]
            land(train, target, "service")
            action_type = "service"
        elif action in ("start_move", "end_move"):
            action_type = "wait"
        elif action in ("wait",):
            action_type = "wait"
        else:
            action_type = "service"

        states.append({
            "index": index,
            "action": action,
            "action_type": action_type,
            "train": involved_train,
            "raw": label,
            "trains": json.loads(json.dumps(trains)),
            "train_path": train_path,
        })
    return states


def load_layout(layout_path):
    if layout_path and Path(layout_path).exists():
        return load_json(layout_path)
    return {"tracks": {}}


def encode_image_base64(image_path):
    path = Path(image_path)
    if not path.exists():
        return None
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    ext = path.suffix.lower()
    mime = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "svg": "image/svg+xml",
    }.get(ext.lstrip("."), "image/png")
    return f"data:{mime};base64,{data}"


def render_html(location_name, states, edges, layout, output_path, image_data_uri=None, image_width=None, image_height=None, track_meta=None, train_lengths=None):
    payload = {
        "locationName": location_name,
        "states": states,
        "edges": edges,
        "positions": layout.get("tracks", {}),
        "imageDataUri": image_data_uri,
        "imageWidth": image_width,
        "imageHeight": image_height,
        "trackMeta": track_meta or {},
        "trainLengths": train_lengths or {},
    }
    data_json = json.dumps(payload)

    document = f"""<!doctype html>
<html lang="en" data-theme="light">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Shunting Plan \u2014 {location_name}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    [data-theme="light"] {{
      --bg: #f8f9fb; --surface: #ffffff; --surface2: #f3f4f6;
      --border: #e5e7ef; --text: #1a1d23; --text2: #374151; --muted: #6b7280; --heading: #111827;
      --badge-arrive-bg: #dbeafe; --badge-arrive-fg: #1d4ed8;
      --badge-move-bg: #ffedd5;   --badge-move-fg: #c2410c;
      --badge-park-bg: #d1fae5;   --badge-park-fg: #065f46;
      --badge-depart-bg: #f3f4f6; --badge-depart-fg: #6b7280;
      --badge-service-bg: #f3e8ff;--badge-service-fg: #7e22ce;
      --badge-wait-bg: #f9fafb;   --badge-wait-fg: #9ca3af;
      --badge-initial-bg: #f3f4f6;--badge-initial-fg: #374151;
      --badge-combine-bg: #f3e8ff;--badge-combine-fg: #7e22ce;
      --status-active-bg: #dbeafe;  --status-active-fg: #1d4ed8;
      --status-waiting-bg: #f9fafb; --status-waiting-fg: #9ca3af;
      --status-parked-bg: #d1fae5;  --status-parked-fg: #065f46;
      --status-service-bg: #fef3c7; --status-service-fg: #92400e;
      --status-departed-bg: #f3f4f6;--status-departed-fg: #9ca3af;
      --status-combined-bg: #f3e8ff;--status-combined-fg: #7e22ce;
      --status-absorbed-bg: #f3f4f6;--status-absorbed-fg: #9ca3af;
      --track-changed-bg: #fef3c7; --track-changed-fg: #92400e;
      --track-normal-fg: #374151; --track-departed-fg: #d1d5db;
      --action-bar-bg: #eff6ff; --action-bar-border: #bfdbfe; --action-bar-fg: #1e40af;
      --row-hover: #f0f7ff; --row-selected: #eff6ff;
      --timeline-hover: #f9fafb; --timeline-selected: #eff6ff;
      --timeline-selected-border: #3b82f6; --timeline-selected-fg: #1d4ed8;
      --th-bg: #f8f9fb; --stat-val: #111827;
      --btn-bg: #f3f4f6; --btn-border: #d1d5db; --btn-fg: #374151; --btn-hover: #e5e7eb;
      --play-bg: #3b82f6; --play-hover: #2563eb;
      --yard-bg: #f1f5f9; --yard-edge: #cbd5e1; --yard-node: #94a3b8;
    }}
    [data-theme="dark"] {{
      --bg: #0f1117; --surface: #181c27; --surface2: #1e2333;
      --border: #2a2f42; --text: #e2e8f8; --text2: #c9d1e8; --muted: #6b7599; --heading: #f1f5ff;
      --badge-arrive-bg: #1e3a5f;  --badge-arrive-fg: #93c5fd;
      --badge-move-bg: #431407;    --badge-move-fg: #fdba74;
      --badge-park-bg: #064e3b;    --badge-park-fg: #6ee7b7;
      --badge-depart-bg: #1f2937;  --badge-depart-fg: #9ca3af;
      --badge-service-bg: #2e1065; --badge-service-fg: #d8b4fe;
      --badge-wait-bg: #1f2937;    --badge-wait-fg: #6b7280;
      --badge-initial-bg: #1f2937; --badge-initial-fg: #9ca3af;
      --badge-combine-bg: #2e1065; --badge-combine-fg: #d8b4fe;
      --status-active-bg: #1e3a5f;  --status-active-fg: #93c5fd;
      --status-waiting-bg: #1f2937; --status-waiting-fg: #6b7280;
      --status-parked-bg: #064e3b;  --status-parked-fg: #6ee7b7;
      --status-service-bg: #451a03; --status-service-fg: #fbbf24;
      --status-departed-bg: #1f2937;--status-departed-fg: #6b7280;
      --status-combined-bg: #2e1065;--status-combined-fg: #d8b4fe;
      --status-absorbed-bg: #1f2937;--status-absorbed-fg: #6b7280;
      --track-changed-bg: #451a03; --track-changed-fg: #fdba74;
      --track-normal-fg: #c9d1e8; --track-departed-fg: #374151;
      --action-bar-bg: #1e3a5f; --action-bar-border: #1d4ed8; --action-bar-fg: #93c5fd;
      --row-hover: #1e2436; --row-selected: #1e3a5f;
      --timeline-hover: #1e2333; --timeline-selected: #1e2436;
      --timeline-selected-border: #3b82f6; --timeline-selected-fg: #93c5fd;
      --th-bg: #0f1117; --stat-val: #f1f5ff;
      --btn-bg: #1e2333; --btn-border: #2a2f42; --btn-fg: #e2e8f8; --btn-hover: #2a2f42;
      --play-bg: #3b82f6; --play-hover: #2563eb;
      --yard-bg: #141824; --yard-edge: #2a2f42; --yard-node: #374151;
    }}

    body {{ font-family: system-ui, sans-serif; background: var(--bg); color: var(--text); height: 100vh; display: flex; flex-direction: column; overflow: hidden; font-size: 13px; transition: background 0.2s, color 0.2s; }}

    header {{ display: flex; align-items: center; gap: 12px; padding: 10px 20px; background: var(--surface); border-bottom: 1px solid var(--border); flex-shrink: 0; flex-wrap: wrap; }}
    h1 {{ font-size: 14px; font-weight: 600; color: var(--heading); }}
    .loc {{ font-size: 12px; color: var(--muted); }}
    .controls {{ display: flex; align-items: center; gap: 8px; margin-left: auto; }}
    button {{ background: var(--btn-bg); border: 1px solid var(--btn-border); color: var(--btn-fg); padding: 5px 12px; border-radius: 6px; font-size: 12px; cursor: pointer; white-space: nowrap; transition: background 0.15s; }}
    button:hover {{ background: var(--btn-hover); }}
    button.play {{ background: var(--play-bg); color: #fff; border-color: var(--play-bg); }}
    button.play:hover {{ background: var(--play-hover); }}
    .ctr {{ font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; min-width: 50px; }}
    input[type=range] {{ width: 140px; accent-color: #3b82f6; }}

    #summary {{ display: flex; background: var(--surface); border-bottom: 1px solid var(--border); flex-shrink: 0; }}
    .stat {{ flex: 1; padding: 10px 20px; border-right: 1px solid var(--border); }}
    .stat:last-child {{ border-right: none; }}
    .stat-val {{ font-size: 20px; font-weight: 700; color: var(--stat-val); line-height: 1; }}
    .stat-label {{ font-size: 11px; color: var(--muted); margin-top: 3px; }}

    #action-bar {{ padding: 8px 20px; background: var(--action-bar-bg); border-bottom: 1px solid var(--action-bar-border); font-size: 12px; color: var(--action-bar-fg); flex-shrink: 0; min-height: 34px; display: flex; align-items: center; gap: 8px; }}
    .action-badge {{ padding: 2px 10px; border-radius: 20px; font-size: 11px; font-weight: 600; }}

    /* YARD MAP */
    #yard-panel {{ background: var(--yard-bg); border-bottom: 1px solid var(--border); flex-shrink: 0; padding: 8px 20px; display: flex; align-items: center; gap: 12px; }}
    #yard-label {{ font-size: 11px; color: var(--muted); font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; white-space: nowrap; }}
    #yard-svg-wrap {{ flex: 1; overflow: hidden; }}
    #yard-svg {{ width: 100%; display: block; }}
    #yard-legend {{ display: flex; flex-direction: column; gap: 4px; flex-shrink: 0; }}
    .yard-leg {{ display: flex; align-items: center; gap: 5px; font-size: 10px; color: var(--muted); white-space: nowrap; }}
    .yard-leg-dot {{ width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }}

    main {{ display: grid; grid-template-columns: 1fr 260px; flex: 1; overflow: hidden; }}

    #table-wrap {{ overflow: auto; padding: 16px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th {{ text-align: left; padding: 8px 12px; font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 2px solid var(--border); position: sticky; top: 0; background: var(--th-bg); z-index: 1; white-space: nowrap; }}
    td {{ padding: 8px 12px; border-bottom: 1px solid var(--border); vertical-align: middle; }}
    tr.data-row {{ cursor: pointer; }}
    tr.data-row:hover td {{ background: var(--row-hover); }}
    tr.data-row.selected td {{ background: var(--row-selected); }}

    .train-dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; flex-shrink: 0; }}
    .train-name {{ font-weight: 600; font-size: 12px; color: var(--heading); font-family: monospace; }}
    .train-len {{ font-size: 10px; color: var(--muted); font-weight: 400; margin-left: 4px; }}
    .status-badge {{ display: inline-block; padding: 2px 8px; border-radius: 20px; font-size: 11px; font-weight: 500; }}
    .status-active   {{ background: var(--status-active-bg);   color: var(--status-active-fg); }}
    .status-waiting  {{ background: var(--status-waiting-bg);  color: var(--status-waiting-fg); }}
    .status-parked   {{ background: var(--status-parked-bg);   color: var(--status-parked-fg); }}
    .status-service  {{ background: var(--status-service-bg);  color: var(--status-service-fg); }}
    .status-departed {{ background: var(--status-departed-bg); color: var(--status-departed-fg); }}
    .status-combined {{ background: var(--status-combined-bg); color: var(--status-combined-fg); }}
    .status-absorbed {{ background: var(--status-absorbed-bg); color: var(--status-absorbed-fg); }}
    .track-cell {{ font-family: monospace; font-size: 12px; font-weight: 500; }}
    .track-changed  {{ background: var(--track-changed-bg); color: var(--track-changed-fg); border-radius: 4px; padding: 2px 6px; }}
    .track-normal   {{ color: var(--track-normal-fg); }}
    .track-departed {{ color: var(--track-departed-fg); }}
    .prev-track {{ font-family: monospace; font-size: 11px; color: var(--muted); }}

    tr.data-row.is-combined td {{ opacity: 0.35; color: var(--muted); }}

    aside {{ border-left: 1px solid var(--border); background: var(--surface); display: flex; flex-direction: column; overflow: hidden; }}
    .aside-head {{ padding: 10px 14px; font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; border-bottom: 1px solid var(--border); flex-shrink: 0; display: flex; align-items: center; justify-content: space-between; }}
    #filter-label {{ font-size: 11px; color: #3b82f6; font-weight: 500; cursor: pointer; text-transform: none; letter-spacing: 0; }}
    #timeline {{ overflow-y: auto; flex: 1; }}
    .t-item {{ display: flex; gap: 8px; align-items: flex-start; padding: 7px 12px; cursor: pointer; border-left: 3px solid transparent; transition: background 0.1s; }}
    .t-item:hover {{ background: var(--timeline-hover); }}
    .t-item.current {{ background: var(--timeline-selected); border-left-color: var(--timeline-selected-border); }}
    .t-item.hidden {{ display: none; }}
    .t-num {{ font-size: 10px; color: var(--muted); min-width: 20px; font-variant-numeric: tabular-nums; padding-top: 2px; }}
    .t-badge {{ padding: 1px 7px; border-radius: 20px; font-size: 10px; font-weight: 600; white-space: nowrap; flex-shrink: 0; }}
    .t-text {{ font-size: 11px; color: var(--text2); line-height: 1.5; }}
    .t-item.current .t-text {{ color: var(--timeline-selected-fg); font-weight: 500; }}
    .badge-arrive  {{ background: var(--badge-arrive-bg);  color: var(--badge-arrive-fg); }}
    .badge-move    {{ background: var(--badge-move-bg);    color: var(--badge-move-fg); }}
    .badge-park    {{ background: var(--badge-park-bg);    color: var(--badge-park-fg); }}
    .badge-depart  {{ background: var(--badge-depart-bg);  color: var(--badge-depart-fg); }}
    .badge-service {{ background: var(--badge-service-bg); color: var(--badge-service-fg); }}
    .badge-wait    {{ background: var(--badge-wait-bg);    color: var(--badge-wait-fg); }}
    .badge-initial {{ background: var(--badge-initial-bg); color: var(--badge-initial-fg); }}
    .badge-combine {{ background: var(--badge-combine-bg); color: var(--badge-combine-fg); }}
    .badge-split   {{ background: var(--badge-combine-bg); color: var(--badge-combine-fg); }}
    .action-arrive  {{ background: var(--badge-arrive-bg);  color: var(--badge-arrive-fg); }}
    .action-move    {{ background: var(--badge-move-bg);    color: var(--badge-move-fg); }}
    .action-park    {{ background: var(--badge-park-bg);    color: var(--badge-park-fg); }}
    .action-depart  {{ background: var(--badge-depart-bg);  color: var(--badge-depart-fg); }}
    .action-service {{ background: var(--badge-service-bg); color: var(--badge-service-fg); }}
    .action-wait    {{ background: var(--badge-wait-bg);    color: var(--badge-wait-fg); }}
    .action-initial {{ background: var(--badge-initial-bg); color: var(--badge-initial-fg); }}
    .action-combine {{ background: var(--badge-combine-bg); color: var(--badge-combine-fg); }}
    .action-split   {{ background: var(--badge-combine-bg); color: var(--badge-combine-fg); }}
    .legend {{ padding: 10px 14px; border-top: 1px solid var(--border); display: flex; flex-direction: column; gap: 5px; flex-shrink: 0; }}
    .leg {{ display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--muted); }}
    .leg-dot {{ width: 10px; height: 10px; border-radius: 3px; flex-shrink: 0; }}
    #node-tooltip {{ position: fixed; background: var(--surface); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px; font-size: 12px; pointer-events: none; z-index: 1000; display: none; box-shadow: 0 2px 8px rgba(0,0,0,0.3); white-space: nowrap; }}
    #node-tooltip .tt-name {{ font-weight: 600; }}
    #node-tooltip .tt-type {{ color: var(--muted); font-size: 11px; margin-left: 4px; }}
    #node-tooltip .tt-parking {{ font-size: 10px; color: var(--muted); margin-left: 4px; }}
  </style>
</head>
<body>

<header>
  <h1>Shunting Plan</h1>
  <span class="loc">{location_name}</span>
  <div class="controls">
    <button onclick="prev()">&#8592; Prev</button>
    <button class="play" id="playBtn" onclick="togglePlay()">&#9654; Play</button>
    <button onclick="next()">Next &#8594;</button>
    <input type="range" id="slider" min="0" value="0" oninput="render(+this.value)">
    <span class="ctr" id="ctr">0 / 0</span>
    <button id="theme-btn" onclick="toggleTheme()">Dark</button>
  </div>
</header>

<div id="summary">
  <div class="stat"><div class="stat-val" id="s-trains">-</div><div class="stat-label">Trains</div></div>
  <div class="stat"><div class="stat-val" id="s-steps">-</div><div class="stat-label">Plan steps</div></div>
  <div class="stat"><div class="stat-val" id="s-departed">-</div><div class="stat-label">Departed</div></div>
  <div class="stat"><div class="stat-val" id="s-parked">-</div><div class="stat-label">Parked</div></div>
</div>

<div id="action-bar">
  <span id="action-badge" class="action-badge action-initial">start</span>
  <span id="action-desc">Initial state</span>
</div>

<!-- YARD MAP PANEL -->
<div id="yard-panel">
  <div id="yard-label">Yard map</div>
  <div id="yard-svg-wrap">
    <svg id="yard-svg" height="120" viewBox="0 0 1000 120" preserveAspectRatio="xMidYMid meet">
      <g id="edges-layer"></g>
      <g id="nodes-layer"></g>
      <g id="train-layer"></g>
    </svg>
  </div>
  <div id="yard-legend"></div>
  <div id="node-tooltip"><span class="tt-name"></span><span class="tt-type"></span><span class="tt-parking"></span></div>
</div>

<main>
  <div id="table-wrap">
    <table>
      <thead>
        <tr>
          <th style="min-width:140px">Train</th>
          <th style="min-width:85px">Status</th>
          <th style="min-width:90px">Track</th>
          <th style="min-width:90px">Previous track</th>
        </tr>
      </thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>
  <aside>
    <div class="aside-head">
      <span>Plan steps</span>
      <span id="filter-label" onclick="clearFilter()"></span>
    </div>
    <div id="timeline"></div>
    <div class="legend">
      <div class="leg"><div class="leg-dot" style="background:var(--badge-arrive-bg);border:1px solid var(--badge-arrive-fg)"></div>Arrive</div>
      <div class="leg"><div class="leg-dot" style="background:var(--badge-move-bg);border:1px solid var(--badge-move-fg)"></div>Move</div>
      <div class="leg"><div class="leg-dot" style="background:var(--badge-park-bg);border:1px solid var(--badge-park-fg)"></div>Park</div>
      <div class="leg"><div class="leg-dot" style="background:var(--badge-service-bg);border:1px solid var(--badge-service-fg)"></div>Service</div>
      <div class="leg"><div class="leg-dot" style="background:var(--badge-depart-bg);border:1px solid var(--badge-depart-fg)"></div>Depart</div>
      <div class="leg"><div class="leg-dot" style="background:var(--badge-combine-bg);border:1px solid var(--badge-combine-fg)"></div>Combine / Split</div>
    </div>
  </aside>
</main>

<script>
const data = {data_json};
let current = 0;
let timer = null;
let filterTrain = null;

const TRAIN_COLORS = ['#3b82f6','#f59e0b','#ef4444','#10b981','#8b5cf6','#ec4899','#06b6d4','#84cc16'];
const allTrains = [...new Set(data.states.flatMap(s => Object.keys(s.trains)))]
  .filter(t => data.states.some(s => s.trains[t] && s.trains[t].track))
  .sort((a, b) => {{
    const aCombo = a.includes('+'), bCombo = b.includes('+');
    if (aCombo && !bCombo) return 1;
    if (!aCombo && bCombo) return -1;
    return a.localeCompare(b);
  }});
const trainColorMap = {{}};
allTrains.forEach((t, i) => {{ trainColorMap[t] = TRAIN_COLORS[i % TRAIN_COLORS.length]; }});

function shortName(n) {{
  if (/^train_in_standing_\d+$/.test(n)) return n.replace(/^train_in_standing_(\d+)$/, 'Standing $1');
  if (/^train\D/.test(n)) return n.replace(/^train/, 'Train ');
  if (/^su_/.test(n)) return n.replace(/^su_/, 'SU ');
  if (n.includes('+')) {{ const parts = n.split('+'); return parts.join(' + ') + ' \u2014 combined'; }}
  return n;
}}
function actionLabel(a) {{
  return {{arrive:'arrive',move:'move',park:'park',depart:'depart',service:'service',wait:'wait',initial:'start',combine:'combine',split:'split'}}[a]||a;
}}
function plainDesc(state) {{
  const t = state.train ? shortName(state.train) : null;
  const raw = state.raw || '';
  const a = state.action_type;
  if (a==='initial') return 'Initial state \u2014 all trains at starting positions';
  if (a==='arrive'&&t) {{ const m=raw.match(/@\s*(\S+)/); return m?t+' arrived at track '+m[1]:t+' arrived'; }}
  if (a==='move'&&t) {{
    const info=data.states[current].trains[state.train];
    const prev=data.states[Math.max(0,current-1)].trains[state.train];
    const isCombined = state.train && state.train.includes('+');
    const suffix = isCombined ? ' \u2014 combined unit' : '';
    return t+' moved from track '+trackName(prev?prev.track:null)+' \u2192 track '+trackName(info?info.track:null)+suffix;
  }}
  if (a==='park'&&t) {{ const info=data.states[current].trains[state.train]; return t+' parked on track '+trackName(info?info.track:null); }}
  if (a==='depart'&&t) {{ const m=raw.match(/@\s*(\S+)/); return t+' departed from track '+(m?m[1]:'?')+' \u2713'; }}
  if (a==='service'&&t) return t+' \u2014 service: '+raw.replace(/^\d+(\.\.\d+)?:\s*/,'');
  if (a==='wait'&&t) return t+' waiting';
  if (a==='split'&&t) {{ const m=raw.match(/\u2192\s*(.+)$/); return t+' split into '+(m?m[1]:'?'); }}
  return raw.replace(/^\d+(\.\.\d+)?:\s*/,'');
}}

// ---- YARD MAP ----
const positions = data.positions || {{}};
const trackMeta = data.trackMeta || {{}};
const posKeys = Object.keys(positions);
const hasPositions = posKeys.length > 0;
function trackName(id) {{
  if (!id) return '?';
  const pos = positions[id];
  if (pos && pos.name) return pos.name;
  const meta = trackMeta[id];
  if (meta && meta.name) return meta.name;
  return id;
}}
let svgMinX=0, svgMinY=0, svgScaleX=1, svgScaleY=1, svgPad=20, svgNodeR=3, svgNodeRActive=5, svgNodeRPrev=4, svgTrackW=2, svgTrackWActive=3, svgTrackWPrev=2.5;

function portOf(pos, side) {{
  if (pos && Array.isArray(pos.shape) && pos.shape.length>=2) {{
    return side==='a' ? pos.shape[0] : pos.shape[pos.shape.length-1];
  }}
  return pos ? [pos.x, pos.y] : null;
}}
function nodeCircleR(pos, meta) {{
  const layoutSize=pos&&pos.size;
  const isParking=meta.parkingAllowed===true;
  if (layoutSize==='big') return svgNodeR;
  if (isParking) return svgNodeR;
  return Math.max(1, svgNodeR*0.22);
}}
function attachTooltip(el,id,meta,isParking) {{
  el.addEventListener('mouseover', function(e) {{
    const tip=document.getElementById('node-tooltip');
    tip.querySelector('.tt-name').textContent=trackName(id);
    tip.querySelector('.tt-type').textContent=meta.type?'('+meta.type+')':'';
    tip.querySelector('.tt-parking').textContent=isParking?'parking':'';
    tip.style.display='block';
  }});
  el.addEventListener('mousemove', function(e) {{
    const tip=document.getElementById('node-tooltip');
    tip.style.left=(e.clientX+12)+'px';
    tip.style.top=(e.clientY-8)+'px';
  }});
  el.addEventListener('mouseout', function() {{
    document.getElementById('node-tooltip').style.display='none';
  }});
}}

function buildYard() {{
  if (!hasPositions) {{ document.getElementById('yard-panel').style.display='none'; return; }}
  const xs=posKeys.map(k=>positions[k].x), ys=posKeys.map(k=>positions[k].y);
  const minX=Math.min(...xs),maxX=Math.max(...xs),minY=Math.min(...ys),maxY=Math.max(...ys);
  const hasImage = data.imageDataUri && data.imageWidth && data.imageHeight;
  if (hasImage) {{
    const imgW = data.imageWidth, imgH = data.imageHeight;
    svgPad = 0; svgScaleX = 1; svgScaleY = 1; svgMinX = 0; svgMinY = 0;
    svgNodeR = 14; svgNodeRActive = 20; svgNodeRPrev = 16;
    svgTrackW = 6; svgTrackWActive = 9; svgTrackWPrev = 7;
    const svg = document.getElementById('yard-svg');
    svg.setAttribute('viewBox', `0 0 ${{imgW}} ${{imgH}}`);
    const aspectH = Math.round(800 * imgH / imgW);
    svg.setAttribute('height', Math.max(200, aspectH));
    const img = document.createElementNS('http://www.w3.org/2000/svg','image');
    img.setAttribute('href', data.imageDataUri);
    img.setAttribute('x', 0); img.setAttribute('y', 0);
    img.setAttribute('width', imgW); img.setAttribute('height', imgH);
    svg.insertBefore(img, svg.firstChild);
  }} else {{
    const pad=20,svgW=1000,svgH=120;
    const scale=Math.min((svgW-pad*2)/(maxX-minX||1),(svgH-pad*2)/(maxY-minY||1));
    svgPad=20; svgScaleX=scale; svgScaleY=scale; svgMinX=minX; svgMinY=minY;
    svgNodeR=3; svgNodeRActive=5; svgNodeRPrev=4; svgTrackW=2; svgTrackWActive=3; svgTrackWPrev=2.5;
    document.getElementById('yard-svg').setAttribute('viewBox',`0 0 ${{svgW}} ${{svgH}}`);
  }}
  const edgesLayer=document.getElementById('edges-layer');
  data.edges.forEach(e => {{
    const a=positions[e.source],b=positions[e.target];
    if(!a||!b) return;
    const ap=portOf(a,e.sourceSide||'a'), bp=portOf(b,e.targetSide||'b');
    if(!ap||!bp) return;
    const line=document.createElementNS('http://www.w3.org/2000/svg','line');
    line.setAttribute('x1',toSvgX(ap[0])); line.setAttribute('y1',toSvgY(ap[1]));
    line.setAttribute('x2',toSvgX(bp[0])); line.setAttribute('y2',toSvgY(bp[1]));
    line.setAttribute('stroke','var(--yard-edge)'); line.setAttribute('stroke-width','1.5');
    line.setAttribute('data-source',e.source); line.setAttribute('data-target',e.target);
    edgesLayer.appendChild(line);
  }});
  const nodesLayer=document.getElementById('nodes-layer');
  posKeys.forEach(id => {{
    const pos=positions[id];
    const meta=trackMeta[id]||{{}};
    const isParking=meta.parkingAllowed===true;
    const shape = pos && Array.isArray(pos.shape) && pos.shape.length>=2 ? pos.shape : null;
    const nodeId='node-'+id.replace(/[^a-zA-Z0-9]/g,'_');
    let el;
    if (shape) {{
      const pts=shape.map(p=>toSvgX(p[0])+','+toSvgY(p[1])).join(' ');
      el=document.createElementNS('http://www.w3.org/2000/svg','polyline');
      el.setAttribute('points',pts);
      el.setAttribute('fill','none');
      el.setAttribute('stroke','var(--yard-node)');
      el.setAttribute('stroke-width',svgTrackW);
      el.setAttribute('stroke-linejoin','round');
      el.setAttribute('stroke-linecap','round');
      el.setAttribute('style','pointer-events:stroke');
      el.setAttribute('data-shape','1');
    }} else {{
      el=document.createElementNS('http://www.w3.org/2000/svg','circle');
      el.setAttribute('cx',toSvgX(pos.x)); el.setAttribute('cy',toSvgY(pos.y));
      el.setAttribute('r',nodeCircleR(pos,meta));
      el.setAttribute('fill','var(--yard-node)');
      el.setAttribute('stroke','#fff'); el.setAttribute('stroke-width','2');
    }}
    el.classList.add('t-node');
    el.setAttribute('data-parking',isParking?'1':'0');
    el.setAttribute('data-id',id);
    el.setAttribute('id',nodeId);
    attachTooltip(el,id,meta,isParking);
    nodesLayer.appendChild(el);
  }});
  const legendEl=document.getElementById('yard-legend');
  allTrains.forEach(train => {{
    const item=document.createElement('div'); item.className='yard-leg';
    item.innerHTML=`<div class="yard-leg-dot" style="background:${{trainColorMap[train]}}"></div>${{shortName(train)}}`;
    legendEl.appendChild(item);
  }});
}}
function toSvgX(x) {{ return svgPad+(x-svgMinX)*svgScaleX; }}
function toSvgY(y) {{ return svgPad+(y-svgMinY)*svgScaleY; }}

function polylineLength(shape) {{
  let total = 0;
  for (let i = 1; i < shape.length; i++) {{
    total += Math.hypot(shape[i][0]-shape[i-1][0], shape[i][1]-shape[i-1][1]);
  }}
  return total;
}}

// Sub-polyline of `shape` covering cumulative pixel-length fractions [fStart, fEnd].
function subPolyline(shape, fStart, fEnd) {{
  const total = polylineLength(shape);
  if (total <= 0 || fStart >= 1 || fEnd <= 0 || fEnd <= fStart) return [];
  const startD = Math.max(0, Math.min(total, fStart * total));
  const endD = Math.max(startD, Math.min(total, fEnd * total));
  const pts = [];
  let acc = 0;
  for (let i = 1; i < shape.length; i++) {{
    const a = shape[i-1], b = shape[i];
    const seg = Math.hypot(b[0]-a[0], b[1]-a[1]);
    if (seg <= 0) continue;
    const segStart = acc, segEnd = acc + seg;
    if (segEnd < startD) {{ acc = segEnd; continue; }}
    if (segStart > endD) break;
    if (pts.length === 0) {{
      const t0 = Math.max(0, (startD - segStart) / seg);
      pts.push([a[0] + (b[0]-a[0])*t0, a[1] + (b[1]-a[1])*t0]);
    }}
    if (segEnd >= endD) {{
      const t1 = Math.min(1, (endD - segStart) / seg);
      if (t1 > 0) pts.push([a[0] + (b[0]-a[0])*t1, a[1] + (b[1]-a[1])*t1]);
      break;
    }}
    pts.push([b[0], b[1]]);
    acc = segEnd;
  }}
  if (pts.length === 1) pts.push(pts[0]);
  return pts;
}}

function trainRatio(train, trackId) {{
  const trackLen = trackMeta[trackId] ? trackMeta[trackId].length : 0;
  const trainLen = data.trainLengths ? data.trainLengths[train] : 0;
  if (trainLen > 0 && trackLen > 0) return Math.min(1, trainLen / trackLen);
  return 1;
}}

function drawTrainSegment(trackId, fStart, fEnd, color) {{
  const pos = positions[trackId];
  const shape = pos && Array.isArray(pos.shape) && pos.shape.length >= 2 ? pos.shape : null;
  if (!shape) return;
  const pts = subPolyline(shape, fStart, fEnd);
  if (!pts.length) return;
  const poly = document.createElementNS('http://www.w3.org/2000/svg','polyline');
  poly.setAttribute('points', pts.map(p => toSvgX(p[0]) + ',' + toSvgY(p[1])).join(' '));
  poly.setAttribute('fill','none');
  poly.setAttribute('stroke', color);
  poly.setAttribute('stroke-width', svgTrackWActive);
  poly.setAttribute('stroke-linejoin','round');
  poly.setAttribute('stroke-linecap','round');
  poly.setAttribute('style','pointer-events:none');
  document.getElementById('train-layer').appendChild(poly);
}}

function updateYard(state, prevState) {{
  if(!hasPositions) return;
  document.querySelectorAll('#edges-layer line').forEach(l => {{
    l.setAttribute('stroke','var(--yard-edge)'); l.setAttribute('stroke-width','1.5');
  }});
  document.querySelectorAll('#nodes-layer .t-node').forEach(n => {{
    const id=n.getAttribute('data-id');
    const pos=id?positions[id]:null;
    const meta=id?(trackMeta[id]||{{}}):{{}};
    if (n.getAttribute('data-shape')==='1') {{
      n.setAttribute('stroke','var(--yard-node)');
      n.setAttribute('stroke-width',svgTrackW);
      n.setAttribute('fill','none');
    }} else {{
      n.setAttribute('fill','var(--yard-node)');
      n.setAttribute('stroke','#fff'); n.setAttribute('stroke-width','2');
      n.setAttribute('r',nodeCircleR(pos,meta));
    }}
  }});
  document.getElementById('train-layer').innerHTML='';
  const trainsToShow=filterTrain?[filterTrain]:allTrains;
  trainsToShow.forEach(train => {{
    const info=state.trains[train];
    if(!info||!info.track||info.status==='departed'||info.status==='absorbed') return;
    const color=trainColorMap[train];
    const trainPath = state.train_path && state.train_path[train];
    if (trainPath && trainPath.length >= 2) {{
      for (let i = 0; i < trainPath.length; i++) {{
        const pn = document.getElementById('node-'+trainPath[i].replace(/[^a-zA-Z0-9]/g,'_'));
        if (pn) {{
          const isLast = i === trainPath.length - 1;
          if (pn.getAttribute('data-shape')==='1') {{
            pn.setAttribute('stroke', color);
            pn.setAttribute('stroke-width', isLast ? svgTrackWActive : svgTrackWPrev);
          }} else {{
            pn.setAttribute('fill', color);
            pn.setAttribute('r', isLast ? svgNodeRActive : svgNodeRPrev);
          }}
        }}
        if (i < trainPath.length - 1) {{
          const a = trainPath[i], b = trainPath[i+1];
          document.querySelectorAll('#edges-layer line').forEach(l => {{
            const ls = l.getAttribute('data-source'), lt = l.getAttribute('data-target');
            if ((ls === a && lt === b) || (ls === b && lt === a)) {{
              l.setAttribute('stroke', color); l.setAttribute('stroke-width', '3');
            }}
          }});
        }}
      }}
    }}
    if(prevState) {{
      const prev=prevState.trains[train];
      if(prev&&prev.track&&prev.track!==info.track) {{
        const src=prev.track,tgt=info.track;
        document.querySelectorAll('#edges-layer line').forEach(l => {{
          const ls=l.getAttribute('data-source'),lt=l.getAttribute('data-target');
          if((ls===src&&lt===tgt)||(ls===tgt&&lt===src)) {{
            l.setAttribute('stroke',color); l.setAttribute('stroke-width','3');
          }}
        }});
        const pn=document.getElementById('node-'+src.replace(/[^a-zA-Z0-9]/g,'_'));
        if(pn&&(pn.getAttribute('fill')==='var(--yard-node)'||pn.getAttribute('stroke')==='var(--yard-node)')) {{
          if (pn.getAttribute('data-shape')==='1') {{ pn.setAttribute('stroke',color); pn.setAttribute('stroke-width',svgTrackWPrev); }}
          else {{ pn.setAttribute('fill',color); pn.setAttribute('r',svgNodeRPrev); }}
        }}
      }}
    }}
  }});

  // Parked / waiting trains: draw proportional-length segments along track shapes.
  // Each train rests flush against its restSide end (a-side or b-side), i.e. it
  // moved as far as possible away from the side it entered; unknown -> b-side.
  const groups = {{}};
  Object.keys(state.trains).forEach(train => {{
    if (filterTrain && train !== filterTrain) return;
    const info = state.trains[train];
    if (!info || !info.track || info.status==='departed' || info.status==='absorbed') return;
    if (state.train_path && state.train_path[train] && state.train_path[train].length >= 2) return;
    (groups[info.track] = groups[info.track] || []).push(train);
  }});
  Object.keys(groups).forEach(trackId => {{
    const pos = positions[trackId];
    const shape = pos && Array.isArray(pos.shape) && pos.shape.length >= 2 ? pos.shape : null;
    const node = document.getElementById('node-'+trackId.replace(/[^a-zA-Z0-9]/g,'_'));
    if (shape) {{
      const anchorA = [], anchorB = [];
      groups[trackId].forEach(train => {{
        const info = state.trains[train];
        (info.restSide === 'a' ? anchorA : anchorB).push(train);
      }});
      let cum = 0;
      anchorA.forEach(train => {{
        const end = Math.min(1, cum + trainRatio(train, trackId));
        if (end > cum) drawTrainSegment(trackId, cum, end, trainColorMap[train]);
        cum = end;
      }});
      let cumEnd = 1;
      anchorB.forEach(train => {{
        const start = Math.max(0, cumEnd - trainRatio(train, trackId));
        if (cumEnd > start) drawTrainSegment(trackId, start, cumEnd, trainColorMap[train]);
        cumEnd = start;
      }});
    }} else if (node) {{
      groups[trackId].forEach(train => {{
        node.setAttribute('fill', trainColorMap[train]);
        node.setAttribute('r', svgNodeRActive);
      }});
    }}
  }});
}}

// ---- TABLE ----
function buildRows() {{
  document.getElementById('tbody').innerHTML = allTrains.map(train => {{
    const isComboPair = train.includes('+');
    const rowClass = isComboPair ? 'data-row combo-row' : 'data-row';
    return `<tr class="${{rowClass}}" id="row-${{train}}" onclick="filterByTrain('${{train}}')">
      <td><span class="train-dot" style="background:${{trainColorMap[train]}}"></span><span class="train-name">${{shortName(train)}}</span>${{data.trainLengths && data.trainLengths[train] ? `<span class="train-len">\u00b7 ${{data.trainLengths[train]}} m</span>` : ''}}</td>
      <td id="status-${{train}}">-</td>
      <td id="track-${{train}}">-</td>
      <td id="prev-track-${{train}}"><span class="prev-track">-</span></td>
    </tr>`;
  }}).join('');
}}

// ---- TIMELINE ----
function buildTimeline() {{
  const tl=document.getElementById('timeline');
  tl.innerHTML='';
  data.states.forEach((state,i) => {{
    const item=document.createElement('div');
    item.className='t-item'; item.dataset.idx=i; item.dataset.train=state.train||'';
    const atype=state.action_type||'initial';
    const badgeClass = 'badge-'+atype;
    const badgeText = actionLabel(atype);
    item.innerHTML=`
      <div class="t-num">${{String(i).padStart(2,'0')}}</div>
      <span class="t-badge ${{badgeClass}}">${{badgeText}}</span>
      <div class="t-text">${{state.train?shortName(state.train)+' \u2014 ':''}}${{state.raw.replace(/^\d+(\.\.\d+)?:\s*/,'').replace(/\s*[-@\u2192]\s*/g,' \u2192 ')}}</div>
    `;
    item.onclick=()=>render(i);
    tl.appendChild(item);
  }});
}}

function filterByTrain(train) {{
  if(filterTrain===train){{clearFilter();return;}}
  filterTrain=train;
  document.getElementById('filter-label').textContent='Clear filter \u00d7';
  document.querySelectorAll('.data-row').forEach(r=>r.classList.toggle('selected',r.id==='row-'+train));
  applyFilter(); render(current);
}}
function clearFilter() {{
  filterTrain=null;
  document.getElementById('filter-label').textContent='';
  document.querySelectorAll('.data-row').forEach(r=>r.classList.remove('selected'));
  applyFilter(); render(current);
}}
function applyFilter() {{
  document.querySelectorAll('.t-item').forEach(el => {{
    el.classList.toggle('hidden',!(!filterTrain||el.dataset.train===filterTrain||el.dataset.idx==='0'));
  }});
}}

function updateSummary() {{
  const last=data.states[data.states.length-1].trains;
  document.getElementById('s-trains').textContent=allTrains.length;
  document.getElementById('s-steps').textContent=data.states.length-1;
  document.getElementById('s-departed').textContent=Object.values(last).filter(t=>t.status==='departed').length;
  document.getElementById('s-parked').textContent=Object.values(last).filter(t=>t.status==='parked').length;
}}

function render(idx) {{
  current=Math.max(0,Math.min(data.states.length-1,idx));
  const state=data.states[current];
  const prevState=data.states[Math.max(0,current-1)];
  const atype=state.action_type||'initial';

  const badge=document.getElementById('action-badge');
  badge.textContent=actionLabel(atype);
  badge.className='action-badge action-'+atype;
  document.getElementById('action-desc').textContent=plainDesc(state);
  document.getElementById('slider').value=current;
  document.getElementById('ctr').textContent=current+' / '+(data.states.length-1);

  allTrains.forEach(train => {{
    const info=state.trains[train];
    const prev=prevState.trains[train];
    const statusEl=document.getElementById('status-'+train);
    const trackEl=document.getElementById('track-'+train);
    const prevEl=document.getElementById('prev-track-'+train);
    const row=document.getElementById('row-'+train);

    if(!info){{statusEl.innerHTML='-';trackEl.innerHTML='-';prevEl.innerHTML='<span class="prev-track">-</span>';return;}}

    const changed=current>0&&prev&&prev.track!==info.track;
    const isCombined=info.status==='combined';

    if(row) {{
      row.classList.toggle('is-combined', info.status==='absorbed');
    }}
    let statusHTML='';
    if(info.status==='parked') statusHTML='<span class="status-badge status-parked">parked</span>';
    else if(info.status==='departed') statusHTML='<span class="status-badge status-departed">departed</span>';
    else if(info.status==='combined') statusHTML='<span class="status-badge status-combined">combined</span>';
    else if(info.status==='absorbed') statusHTML='<span class="status-badge status-absorbed">absorbed</span>';
    else if(info.status==='service') statusHTML='<span class="status-badge status-service">service</span>';
    else if(train===state.train && atype!=='wait' && atype!=='initial') statusHTML='<span class="status-badge status-active">moving</span>';
    else statusHTML='<span class="status-badge status-waiting">waiting</span>';
    statusEl.innerHTML=statusHTML;

    const isAbsorbed = info.status==='absorbed';
    const cls=changed?'track-cell track-changed':isAbsorbed?'track-cell track-departed':'track-cell track-normal';

    // Entry queue tag
    const trainsOnSameTrack=allTrains.filter(t=>state.trains[t]&&state.trains[t].track===info.track);
    const noneHaveMoved=trainsOnSameTrack.every(t=>{{
      const init=data.states[0].trains[t];
      return init&&init.track===state.trains[t].track;
    }});
    const entryTag=trainsOnSameTrack.length>1&&noneHaveMoved
      ?' <span style="font-size:10px;color:var(--muted);font-weight:400">(entry queue)</span>':'';
    const trackDisplay = info.track ? trackName(info.track) : (info.status==='absorbed' ? '\u2014 absorbed' : isCombined ? '\u2014 combined' : '\u2014 not yet in yard');
    trackEl.innerHTML=`<span class="${{cls}}">${{trackDisplay}}</span>${{entryTag}}`;
    prevEl.innerHTML=`<span class="prev-track">${{prev&&prev.track?trackName(prev.track):'-'}}</span>`;
  }});

  updateYard(state,prevState);

  document.querySelectorAll('.t-item').forEach((el,i)=>el.classList.toggle('current',i===current));
  const cur=document.querySelector('.t-item.current:not(.hidden)');
  if(cur) cur.scrollIntoView({{block:'nearest',behavior:'smooth'}});
}}

function prev(){{render(current-1);}}
function next(){{render(current+1);}}
function togglePlay(){{
  if(timer){{clearInterval(timer);timer=null;document.getElementById('playBtn').innerHTML='&#9654; Play';}}
  else{{
    document.getElementById('playBtn').innerHTML='&#9646;&#9646; Pause';
    timer=setInterval(()=>{{
      if(current>=data.states.length-1){{clearInterval(timer);timer=null;document.getElementById('playBtn').innerHTML='&#9654; Play';}}
      else render(current+1);
    }},900);
  }}
}}
function toggleTheme(){{
  const html=document.documentElement;
  const isDark=html.getAttribute('data-theme')==='dark';
  html.setAttribute('data-theme',isDark?'light':'dark');
  document.getElementById('theme-btn').textContent=isDark?'Dark':'Light';
  localStorage.setItem('shunting-theme',isDark?'light':'dark');
}}
const saved=localStorage.getItem('shunting-theme');
if(saved){{
  document.documentElement.setAttribute('data-theme',saved);
  document.getElementById('theme-btn').textContent=saved==='dark'?'Light':'Dark';
}}

document.getElementById('slider').max=data.states.length-1;
buildYard();
buildRows();
buildTimeline();
updateSummary();
render(0);
</script>
</body>
</html>
"""
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write(document)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--location", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--layout", default=None)
    parser.add_argument("--image", default=None)
    args = parser.parse_args()

    location = load_json(args.location)
    scenario = load_json(args.scenario)
    id_to_track, name_to_track = build_track_maps(location)
    edges = build_edges(location, id_to_track)
    initial = initial_train_positions(scenario, id_to_track)
    steps = parse_plan(args.plan, id_to_track)
    states = simulate_steps(initial, steps, id_to_track, location)
    train_lengths = collect_train_lengths(scenario, args.plan, states)

    layout = load_layout(args.layout)
    raw_positions = layout.get("tracks", {})
    positions = {}
    for key, pos in raw_positions.items():
        name = pos.get("name")
        if key in id_to_track:
            tid = key
            if not name:
                name = track_name(tid, id_to_track)
        elif key in name_to_track:
            tid = str(name_to_track[key]["id"])
            name = key
        else:
            tid = key
            name = name or key
        positions[tid] = {"x": pos["x"], "y": pos["y"], "size": pos.get("size"), "name": name, "shape": pos.get("shape")}
    layout["tracks"] = positions

    image_data_uri = None
    image_width = None
    image_height = None
    image_path = args.image or layout.get("image")
    if image_path:
        layout_dir = Path(args.layout).resolve().parent if args.layout else Path.cwd()
        abs_image_path = layout_dir / image_path
        image_data_uri = encode_image_base64(abs_image_path)
        image_width = layout.get("width")
        image_height = layout.get("height")

    track_meta = {str(t["id"]): {"name": str(t["name"]), "parkingAllowed": t.get("parkingAllowed", False), "type": t.get("type", ""), "length": t.get("length", 0)} for t in location.get("trackParts", [])}
    render_html(Path(args.location).parent.name, states, edges, layout, args.output,
                image_data_uri=image_data_uri, image_width=image_width, image_height=image_height,
                track_meta=track_meta, train_lengths=train_lengths)
    print(f"Wrote visualizer to {args.output}")
    print(f"Steps: {len(steps)}; trains: {len(initial)}; yard nodes: {len(positions)}")


if __name__ == "__main__":
    main()