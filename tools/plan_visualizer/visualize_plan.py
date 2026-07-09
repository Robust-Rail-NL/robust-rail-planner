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


def track_aliases(track):
    values = {str(track.get("id", "")), str(track.get("name", ""))}
    return {value for item in values for value in (item, sanitize_pddl_name(item)) if value}


def build_track_maps(location):
    tracks = location.get("trackParts", [])
    id_to_track = {str(track["id"]): track for track in tracks}
    token_to_name = {}
    name_to_track = {}
    for track in tracks:
        name = str(track["name"])
        name_to_track[name] = track
        for alias in track_aliases(track):
            token_to_name[alias] = name
    return id_to_track, token_to_name, name_to_track


def build_edges(location, id_to_track):
    edges = []
    seen = set()
    for track in location.get("trackParts", []):
        src = str(track["name"])
        for nb_id in track.get("aSide", []) + track.get("bSide", []):
            nb = id_to_track.get(str(nb_id))
            if not nb:
                continue
            tgt = str(nb["name"])
            key = tuple(sorted([src, tgt]))
            if key not in seen:
                edges.append({"source": src, "target": tgt})
                seen.add(key)
    return edges


def parse_plan(path):
    if str(path).lower().endswith(".json"):
        return parse_solver_plan(path)
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


def parse_solver_plan(path):
    plan = load_json(path)
    actions = sorted(
        plan.get("actions", []),
        key=lambda a: (int(a.get("startTime", 0)), int(a.get("endTime", 0))),
    )
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
            steps.append({"raw": f"{action.get('startTime')}..{action.get('endTime')}: Combine {train}", "action": "combine", "args": [train]})
        elif not track:
            continue
        elif task_name == "Move":
            steps.append({"raw": f"{action.get('startTime')}..{action.get('endTime')}: Move {train} \u2192 {track}", "action": "move_to", "args": [train, track], "path": path_raw})
        elif task_name == "Arrive":
            steps.append({"raw": f"{action.get('startTime')}: Arrive {train} @ {track}", "action": "arrive", "args": [train, track], "path": path_raw})
        elif task_name == "Exit":
            steps.append({"raw": f"{action.get('startTime')}: Exit {train} @ {track}", "action": "depart", "args": [train, track], "path": path_raw})
        else:
            steps.append({"raw": f"{action.get('startTime')}..{action.get('endTime')}: {task_name} {train} @ {track}", "action": "service", "args": [train, track], "path": path_raw})
    return steps


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
            trains[member_name(train)] = {"track": str(id_to_track[str(track_id)]["name"]), "status": "active"}
    return trains


def normalize_track(token, token_to_name):
    if token in token_to_name:
        return token_to_name[token]
    stripped = unsanitize_track_token(token)
    return token_to_name.get(stripped, stripped)


def normalize_path(raw_path, token_to_name):
    seen = []
    for t in raw_path:
        norm = normalize_track(t, token_to_name)
        if norm and (not seen or norm != seen[-1]):
            seen.append(norm)
    return seen


def simulate_steps(initial_trains, steps, token_to_name):
    states = [{"index": 0, "action": "initial", "action_type": "initial", "train": None, "raw": "Initial state", "trains": json.loads(json.dumps(initial_trains))}]
    trains = json.loads(json.dumps(initial_trains))

    for index, step in enumerate(steps, start=1):
        action = step["action"]
        args = step["args"]
        label = step["raw"]
        involved_train = args[0] if args else None
        raw_path = step.get("path")

        if raw_path:
            train_path = {involved_train: normalize_path(raw_path, token_to_name)}
        else:
            train_path = {}

        if action == "move" and len(args) >= 3:
            train, source, target = args[:3]
            trains.setdefault(train, {"track": None, "status": "active"})
            trains[train]["track"] = normalize_track(target, token_to_name)
            trains[train]["status"] = "active"
            action_type = "move"
        elif action == "move_to" and len(args) >= 2:
            train, target = args[:2]
            trains.setdefault(train, {"track": None, "status": "active"})
            trains[train]["track"] = normalize_track(target, token_to_name)
            trains[train]["status"] = "active"
            action_type = "move"
            if "+" in train:
                for member in train.split("+"):
                    if member in trains:
                        trains[member]["status"] = "combined"
                        trains[member]["track"] = None
        elif action == "arrive" and len(args) >= 2:
            train, target = args[:2]
            trains.setdefault(train, {"track": None, "status": "active"})
            trains[train]["track"] = normalize_track(target, token_to_name)
            trains[train]["status"] = "active"
            action_type = "arrive"
            if "+" in train:
                for member in train.split("+"):
                    if member in trains:
                        trains[member]["status"] = "combined"
                        trains[member]["track"] = None
        elif action == "park" and len(args) >= 2:
            train, track = args[:2]
            trains.setdefault(train, {"track": None, "status": "active"})
            trains[train]["track"] = normalize_track(track, token_to_name)
            trains[train]["status"] = "parked"
            action_type = "park"
        elif action == "depart" and len(args) >= 2:
            train, track = args[:2]
            trains.setdefault(train, {"track": None, "status": "active"})
            trains[train]["track"] = normalize_track(track, token_to_name)
            trains[train]["status"] = "departed"
            action_type = "depart"
        elif action == "combine" and len(args) >= 1:
            train = args[0]
            if train in trains:
                trains[train]["status"] = "combined"
                trains[train]["track"] = None
            action_type = "combine"
        elif action in ("move_aside_empty", "move_aside_occupied",
                        "move_bside_empty", "move_bside_occupied") and len(args) >= 3:
            train, source, target = args[:3]
            trains.setdefault(train, {"track": None, "status": "active"})
            trains[train]["track"] = normalize_track(target, token_to_name)
            trains[train]["status"] = "active"
            action_type = "move"
        elif action in ("depart_aside", "depart_bside") and len(args) >= 2:
            train, track = args[:2]
            trains.setdefault(train, {"track": None, "status": "active"})
            trains[train]["track"] = normalize_track(track, token_to_name)
            trains[train]["status"] = "departed"
            action_type = "depart"
        elif action == "service" and len(args) >= 2:
            train, target = args[:2]
            trains.setdefault(train, {"track": None, "status": "active"})
            trains[train]["track"] = normalize_track(target, token_to_name)
            trains[train]["status"] = "service"
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


def render_html(location_name, states, edges, layout, output_path, image_data_uri=None, image_width=None, image_height=None, track_meta=None):
    payload = {
        "locationName": location_name,
        "states": states,
        "edges": edges,
        "positions": layout.get("tracks", {}),
        "imageDataUri": image_data_uri,
        "imageWidth": image_width,
        "imageHeight": image_height,
        "trackMeta": track_meta or {},
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
      --status-parked-bg: #d1fae5;  --status-parked-fg: #065f46;
      --status-service-bg: #fef3c7; --status-service-fg: #92400e;
      --status-departed-bg: #f3f4f6;--status-departed-fg: #9ca3af;
      --status-combined-bg: #f3e8ff;--status-combined-fg: #7e22ce;
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
      --status-parked-bg: #064e3b;  --status-parked-fg: #6ee7b7;
      --status-service-bg: #451a03; --status-service-fg: #fbbf24;
      --status-departed-bg: #1f2937;--status-departed-fg: #6b7280;
      --status-combined-bg: #2e1065;--status-combined-fg: #d8b4fe;
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
    .status-badge {{ display: inline-block; padding: 2px 8px; border-radius: 20px; font-size: 11px; font-weight: 500; }}
    .status-active   {{ background: var(--status-active-bg);   color: var(--status-active-fg); }}
    .status-parked   {{ background: var(--status-parked-bg);   color: var(--status-parked-fg); }}
    .status-service  {{ background: var(--status-service-bg);  color: var(--status-service-fg); }}
    .status-departed {{ background: var(--status-departed-bg); color: var(--status-departed-fg); }}
    .status-combined {{ background: var(--status-combined-bg); color: var(--status-combined-fg); }}
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
    .action-arrive  {{ background: var(--badge-arrive-bg);  color: var(--badge-arrive-fg); }}
    .action-move    {{ background: var(--badge-move-bg);    color: var(--badge-move-fg); }}
    .action-park    {{ background: var(--badge-park-bg);    color: var(--badge-park-fg); }}
    .action-depart  {{ background: var(--badge-depart-bg);  color: var(--badge-depart-fg); }}
    .action-service {{ background: var(--badge-service-bg); color: var(--badge-service-fg); }}
    .action-wait    {{ background: var(--badge-wait-bg);    color: var(--badge-wait-fg); }}
    .action-initial {{ background: var(--badge-initial-bg); color: var(--badge-initial-fg); }}
    .legend {{ padding: 10px 14px; border-top: 1px solid var(--border); display: flex; flex-direction: column; gap: 5px; flex-shrink: 0; }}
    .leg {{ display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--muted); }}
    .leg-dot {{ width: 10px; height: 10px; border-radius: 3px; flex-shrink: 0; }}
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
    </svg>
  </div>
  <div id="yard-legend"></div>
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
  return {{arrive:'arrive',move:'move',park:'park',depart:'depart',service:'service',wait:'wait',initial:'start',combine:'combine'}}[a]||a;
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
    return t+' moved from track '+(prev?prev.track:'?')+' \u2192 track '+(info?info.track:'?')+suffix;
  }}
  if (a==='park'&&t) {{ const info=data.states[current].trains[state.train]; return t+' parked on track '+(info?info.track:'?'); }}
  if (a==='depart'&&t) {{ const m=raw.match(/@\s*(\S+)/); return t+' departed from track '+(m?m[1]:'?')+' \u2713'; }}
  if (a==='service'&&t) return t+' \u2014 service: '+raw.replace(/^\d+(\.\.\d+)?:\s*/,'');
  if (a==='wait'&&t) return t+' waiting';
  return raw.replace(/^\d+(\.\.\d+)?:\s*/,'');
}}

// ---- YARD MAP ----
const positions = data.positions || {{}};
const trackMeta = data.trackMeta || {{}};
const posKeys = Object.keys(positions);
const hasPositions = posKeys.length > 0;
let svgMinX=0, svgMinY=0, svgScaleX=1, svgScaleY=1, svgPad=20, svgNodeR=3, svgNodeRActive=5, svgNodeRPrev=4;

function buildYard() {{
  if (!hasPositions) {{ document.getElementById('yard-panel').style.display='none'; return; }}
  const xs=posKeys.map(k=>positions[k].x), ys=posKeys.map(k=>positions[k].y);
  const minX=Math.min(...xs),maxX=Math.max(...xs),minY=Math.min(...ys),maxY=Math.max(...ys);
  const hasImage = data.imageDataUri && data.imageWidth && data.imageHeight;
  if (hasImage) {{
    const imgW = data.imageWidth, imgH = data.imageHeight;
    svgPad = 0; svgScaleX = 1; svgScaleY = 1; svgMinX = 0; svgMinY = 0;
    svgNodeR = 14; svgNodeRActive = 20; svgNodeRPrev = 16;
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
    svgNodeR=3; svgNodeRActive=5; svgNodeRPrev=4;
    document.getElementById('yard-svg').setAttribute('viewBox',`0 0 ${{svgW}} ${{svgH}}`);
  }}
  const edgesLayer=document.getElementById('edges-layer');
  data.edges.forEach(e => {{
    const a=positions[e.source],b=positions[e.target];
    if(!a||!b) return;
    const line=document.createElementNS('http://www.w3.org/2000/svg','line');
    line.setAttribute('x1',toSvgX(a.x)); line.setAttribute('y1',toSvgY(a.y));
    line.setAttribute('x2',toSvgX(b.x)); line.setAttribute('y2',toSvgY(b.y));
    line.setAttribute('stroke','var(--yard-edge)'); line.setAttribute('stroke-width','1.5');
    line.setAttribute('data-source',e.source); line.setAttribute('data-target',e.target);
    edgesLayer.appendChild(line);
  }});
  const nodesLayer=document.getElementById('nodes-layer');
  posKeys.forEach(name => {{
    const pos=positions[name];
    const meta=trackMeta[name]||{{}};
    const isParking=meta.parkingAllowed===true;
    const r=isParking?svgNodeR:svgNodeR*0.5;
    const c=document.createElementNS('http://www.w3.org/2000/svg','circle');
    c.setAttribute('cx',toSvgX(pos.x)); c.setAttribute('cy',toSvgY(pos.y));
    c.setAttribute('r',r); c.setAttribute('fill','var(--yard-node)');
    c.setAttribute('stroke','#fff'); c.setAttribute('stroke-width','2');
    c.setAttribute('data-parking',isParking?'1':'0');
    c.setAttribute('id','node-'+name.replace(/[^a-zA-Z0-9]/g,'_'));
    const title=document.createElementNS('http://www.w3.org/2000/svg','title'); title.textContent=name+(isParking?' (parking)':''); c.appendChild(title);
    nodesLayer.appendChild(c);
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

function updateYard(state, prevState) {{
  if(!hasPositions) return;
  document.querySelectorAll('#edges-layer line').forEach(l => {{
    l.setAttribute('stroke','var(--yard-edge)'); l.setAttribute('stroke-width','1.5');
  }});
  document.querySelectorAll('#nodes-layer circle').forEach(c => {{
    const isParking=c.getAttribute('data-parking')==='1';
    c.setAttribute('fill','var(--yard-node)');
    c.setAttribute('stroke','#fff'); c.setAttribute('stroke-width','2');
    c.setAttribute('r',isParking?svgNodeR:svgNodeR*0.5);
  }});
  const trainsToShow=filterTrain?[filterTrain]:allTrains;
  trainsToShow.forEach(train => {{
    const info=state.trains[train];
    if(!info||!info.track||info.status==='departed') return;
    const color=trainColorMap[train];
    const trainPath = state.train_path && state.train_path[train];
    if (trainPath && trainPath.length >= 2) {{
      for (let i = 0; i < trainPath.length; i++) {{
        const pn = document.getElementById('node-'+trainPath[i].replace(/[^a-zA-Z0-9]/g,'_'));
        if (pn) {{
          const isLast = i === trainPath.length - 1;
          pn.setAttribute('fill', color);
          pn.setAttribute('r', isLast ? svgNodeRActive : svgNodeRPrev);
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
    }} else {{
      const node=document.getElementById('node-'+info.track.replace(/[^a-zA-Z0-9]/g,'_'));
      if(node) {{ node.setAttribute('fill',color); node.setAttribute('r',svgNodeRActive); }}
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
        if(pn&&pn.getAttribute('fill')==='var(--yard-node)') {{ pn.setAttribute('fill',color); pn.setAttribute('r',svgNodeRPrev); }}
      }}
    }}
  }});
}}

// ---- TABLE ----
function buildRows() {{
  document.getElementById('tbody').innerHTML = allTrains.map(train => {{
    const isComboPair = train.includes('+');
    const rowClass = isComboPair ? 'data-row combo-row' : 'data-row';
    return `<tr class="${{rowClass}}" id="row-${{train}}" onclick="filterByTrain('${{train}}')">
      <td><span class="train-dot" style="background:${{trainColorMap[train]}}"></span><span class="train-name">${{shortName(train)}}</span></td>
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
    const isCombineAction = state.train && state.train.includes('+');
    const badgeClass = isCombineAction ? 'badge-combine' : 'badge-'+atype;
    const badgeText = isCombineAction ? 'combine' : actionLabel(atype);
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
      // Only grey out individual members that got absorbed, not the combined unit itself
      row.classList.toggle('is-combined', isCombined && !train.includes('+'));
    }}
    // For combined members, show which combined unit they belong to
    if (isCombined && !train.includes('+')) {{
      const combinedUnit = allTrains.find(t => t.includes('+') && t.split('+').includes(train) && state.trains[t] && state.trains[t].status !== 'combined');
      const tag = combinedUnit ? ` <span style="font-size:10px;background:var(--badge-combine-bg);color:var(--badge-combine-fg);padding:1px 5px;border-radius:4px;">in ${{combinedUnit.split('+').join(' + ')}}</span>` : '';
      trackEl.innerHTML = `<span class="track-cell track-departed">\u2014</span>${{tag}}`;
      return;
    }}

    let statusHTML='';
    if(info.status==='parked') statusHTML='<span class="status-badge status-parked">parked</span>';
    else if(info.status==='departed') statusHTML='<span class="status-badge status-departed">departed</span>';
    else if(info.status==='combined') statusHTML='<span class="status-badge status-combined">absorbed</span>';
    else if(info.status==='service') statusHTML='<span class="status-badge status-service">service</span>';
    else statusHTML='<span class="status-badge status-active">moving</span>';
    statusEl.innerHTML=statusHTML;

    const isMemberCombined = isCombined && !train.includes('+');
    const cls=changed?'track-cell track-changed':isMemberCombined?'track-cell track-departed':'track-cell track-normal';

    // Entry queue tag
    const trainsOnSameTrack=allTrains.filter(t=>state.trains[t]&&state.trains[t].track===info.track);
    const noneHaveMoved=trainsOnSameTrack.every(t=>{{
      const init=data.states[0].trains[t];
      return init&&init.track===state.trains[t].track;
    }});
    const entryTag=trainsOnSameTrack.length>1&&noneHaveMoved
      ?' <span style="font-size:10px;color:var(--muted);font-weight:400">(entry queue)</span>':'';
    const trackDisplay = info.track || (isCombined ? '\u2014 combined' : '\u2014 not yet in yard');
    trackEl.innerHTML=`<span class="${{cls}}">${{trackDisplay}}</span>${{entryTag}}`;
    prevEl.innerHTML=`<span class="prev-track">${{prev&&prev.track?prev.track:'-'}}</span>`;
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
    id_to_track, token_to_name, name_to_track = build_track_maps(location)
    edges = build_edges(location, id_to_track)
    initial = initial_train_positions(scenario, id_to_track)
    steps = parse_plan(args.plan)
    states = simulate_steps(initial, steps, token_to_name)

    layout = load_layout(args.layout)
    raw_positions = layout.get("tracks", {})
    name_set = set(name_to_track.keys())
    lower_map = {k.lower(): v for k, v in token_to_name.items()}
    positions = {}
    for key, pos in raw_positions.items():
        if key in name_set:
            track_name = key
        else:
            track_name = token_to_name.get(key) or lower_map.get(key.lower(), key)
        positions[track_name] = {"x": pos["x"], "y": pos["y"]}
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

    track_meta = {str(t["name"]): {"parkingAllowed": t.get("parkingAllowed", False), "type": t.get("type", "")} for t in location.get("trackParts", [])}
    render_html(Path(args.location).parent.name, states, edges, layout, args.output,
                image_data_uri=image_data_uri, image_width=image_width, image_height=image_height,
                track_meta=track_meta)
    print(f"Wrote visualizer to {args.output}")
    print(f"Steps: {len(steps)}; trains: {len(initial)}; yard nodes: {len(positions)}")


if __name__ == "__main__":
    main()