import argparse
import base64
import http.server
import json
import os
import socketserver
import subprocess
import sys
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(
        description="Layout editor + visualizer launcher."
    )
    parser.add_argument("--location-name", default="Location_KleineBinckhorst")
    parser.add_argument("--layout", default=None, help="Layout JSON to edit (default: auto-detected)")
    parser.add_argument("--port", type=int, default=8766)
    return parser.parse_args()


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def json_bytes(obj):
    return json.dumps(obj).encode("utf-8")


HTML = None  # will be filled at module level after this class


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML.encode("utf-8"))
            return
        if self.path == "/api/data":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json_bytes(self.server.editor_data))
            return
        if self.path == "/api/image":
            img_path = self.server.editor_data["image_path"]
            if img_path and Path(img_path).exists():
                with open(img_path, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(404)
                self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        if self.path == "/api/save":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            data = json.loads(body)
            layout_path = self.server.editor_data["layout_path"]
            current = load_json(layout_path)
            tracks = data.get("tracks") or {}
            out = {}
            for tid, t in tracks.items():
                entry = {"x": t["x"], "y": t["y"], "name": t.get("name", str(tid))}
                if t.get("size"):
                    entry["size"] = t["size"]
                if t.get("shape") and len(t["shape"]) >= 2:
                    entry["shape"] = [[int(round(px)), int(round(py))] for px, py in t["shape"]]
                out[str(tid)] = entry
            current["tracks"] = out
            with open(layout_path, "w", encoding="utf-8") as f:
                json.dump(current, f, indent=2)
            self.server.editor_data = build_editor_data(
                self.server.location_path,
                self.server.layout_path,
                self.server.location_name,
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        if "/api/" in str(args[0]):
            super().log_message(format, *args)


def build_editor_data(location_path, layout_path, location_name):
    location_path = Path(location_path)
    layout_path = Path(layout_path)

    if not location_path.exists():
        raise FileNotFoundError(f"Location file not found: {location_path}")
    if not layout_path.exists():
        print(f"Layout file not found at {layout_path}, starting empty")
        layout_data = {"image": "", "width": 0, "height": 0, "tracks": {}}
    else:
        layout_data = load_json(layout_path)

    # resolve image path
    rel_image = layout_data.get("image", "")
    if rel_image:
        abs_image = layout_path.resolve().parent / rel_image
    else:
        abs_image = location_path.parent / "kleine_binckhorst.png"
        if not abs_image.exists():
            abs_image = location_path.parent / "location.png"
            if not abs_image.exists():
                abs_image = None

    # read all track parts from location file
    location = load_json(location_path)
    all_tracks = []
    for part in location.get("trackParts", []):
        tid = str(part.get("id", ""))
        if not tid:
            continue
        entry = {
            "id": tid,
            "name": str(part.get("name", "")),
            "type": part.get("type", ""),
            "aSide": [str(x) for x in part.get("aSide", [])],
            "bSide": [str(x) for x in part.get("bSide", [])],
        }
        if tid in layout_data.get("tracks", {}):
            lay = layout_data["tracks"][tid]
            entry["x"] = lay["x"]
            entry["y"] = lay["y"]
            if lay.get("size"):
                entry["size"] = lay["size"]
            if lay.get("shape") and len(lay["shape"]) >= 2:
                entry["shape"] = [[float(px), float(py)] for px, py in lay["shape"]]
        all_tracks.append(entry)

    # build oriented edges from aSide/bSide references
    edge_set = set()
    edges = []
    for tp in location.get("trackParts", []):
        tid = str(tp["id"])
        for ref in tp.get("aSide", []):
            other = str(ref)
            key = tuple(sorted([tid, other]))
            if other and tid != other and key not in edge_set:
                edge_set.add(key)
                edges.append({"from": tid, "fromSide": "a", "to": other, "toSide": "b"})
        for ref in tp.get("bSide", []):
            other = str(ref)
            key = tuple(sorted([tid, other]))
            if other and tid != other and key not in edge_set:
                edge_set.add(key)
                edges.append({"from": tid, "fromSide": "b", "to": other, "toSide": "a"})

    # sort: positioned first, then unpositioned
    positioned = [t for t in all_tracks if "x" in t]
    unpositioned = [t for t in all_tracks if "x" not in t]
    positioned.sort(key=lambda t: t["id"])
    unpositioned.sort(key=lambda t: t["id"])
    sorted_tracks = positioned + unpositioned

    img_width = layout_data.get("width", 0)
    img_height = layout_data.get("height", 0)

    # Try to get image dimensions from the file if not in layout
    if not img_width and abs_image and abs_image.exists():
        try:
            from PIL import Image
            with Image.open(abs_image) as img:
                img_width, img_height = img.size
        except ImportError:
            pass

    return {
        "tracks": sorted_tracks,
        "edges": edges,
        "image_path": str(abs_image) if abs_image else None,
        "imageWidth": img_width,
        "imageHeight": img_height,
        "layout_path": str(layout_path),
        "location_name": location_name,
    }


def main():
    args = parse_args()

    script_dir = Path(__file__).resolve().parent
    workspace_root = script_dir.parents[2]
    location_dir = workspace_root / "scenario-planning-inputs" / args.location_name
    location_path = location_dir / "location_solver.json"

    if args.layout:
        layout_path = Path(args.layout)
    elif args.location_name == "Location_SimpleService":
        layout_path = script_dir / "layouts" / "simple_service.json"
    else:
        layout_path = script_dir / "layouts" / "kleine_binckhorst.json"

    editor_data = build_editor_data(location_path, layout_path, args.location_name)

    PORT = args.port
    server = socketserver.TCPServer(("127.0.0.1", PORT), Handler)
    server.editor_data = editor_data
    server.location_path = str(location_path)
    server.layout_path = str(layout_path)
    server.location_name = args.location_name
    server.allow_reuse_address = True
    server.server_port = PORT

    all_tracks = editor_data["tracks"]
    positioned = sum(1 for t in all_tracks if "x" in t)

    print(f"Layout editor: http://127.0.0.1:{PORT}")
    print(f"Editing: {layout_path}")
    print(f"Tracks: {len(all_tracks)} ({positioned} positioned, {len(all_tracks) - positioned} unpositioned)")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Layout Editor</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: system-ui, sans-serif; background: #1a1d23; color: #e2e8f8; display: flex; height: 100vh; overflow: hidden; }

#sidebar { width: 320px; min-width: 320px; background: #181c27; border-right: 1px solid #2a2f42; display: flex; flex-direction: column; overflow: hidden; }
#sidebar h2 { padding: 14px 16px; font-size: 14px; font-weight: 600; color: #f1f5ff; border-bottom: 1px solid #2a2f42; }
#sidebar .info { padding: 8px 16px; font-size: 11px; color: #6b7599; border-bottom: 1px solid #2a2f42; }
#search { margin: 8px 12px; padding: 8px 12px; border: 1px solid #2a2f42; border-radius: 6px; background: #0f1117; color: #e2e8f8; font-size: 12px; outline: none; }
#search:focus { border-color: #3b82f6; }
#track-list { flex: 1; overflow-y: auto; }
.track-item { display: flex; align-items: center; gap: 8px; padding: 6px 16px; cursor: pointer; font-size: 12px; border-left: 3px solid transparent; transition: background 0.1s; }
.track-item:hover { background: #1e2333; }
.track-item.selected { background: #1e3a5f; border-left-color: #3b82f6; color: #93c5fd; }
.track-item .dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.track-item .dot.done { background: #10b981; }
.track-item .dot.empty { background: #2a2f42; border: 1px solid #4a4f62; }
.track-item .id { font-weight: 500; min-width: 80px; }
.track-item .id-small { color: #6b7599; font-size: 10px; }
.track-item .type { color: #6b7599; font-size: 10px; }
.track-item .pos { color: #6b7599; font-size: 10px; margin-left: auto; }
.track-item .size-badge { font-size: 9px; color: #6b7599; background: #1e2333; padding: 1px 5px; border-radius: 3px; margin-left: 4px; }
.track-item.selected .size-badge { color: #93c5fd; background: #0f2033; }

#controls { padding: 8px 12px; border-bottom: 1px solid #2a2f42; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
#controls label { font-size: 11px; color: #6b7599; display: flex; align-items: center; gap: 4px; }
#controls button { padding: 4px 10px; border: 1px solid #2a2f42; border-radius: 4px; background: #0f1117; color: #e2e8f8; font-size: 11px; cursor: pointer; }
#controls button:hover { background: #1e2333; }
#controls button.active { background: #3b82f6; border-color: #3b82f6; color: #fff; }
#controls button.primary { background: #3b82f6; border-color: #3b82f6; color: #fff; font-weight: 600; }
#size-slider { width: 80px; accent-color: #3b82f6; cursor: pointer; }

#shape-tools { padding: 8px 12px; border-bottom: 1px solid #2a2f42; display: flex; flex-direction: column; gap: 6px; background: #151a28; }
#shape-tools.hidden { display: none; }
.shape-title { font-size: 11px; color: #8b93b5; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; }
.shape-hint { font-size: 10px; color: #6b7599; line-height: 1.4; }
.shape-row { display: flex; gap: 6px; }
.shape-row button { flex: 1; padding: 5px 8px; font-size: 11px; border: 1px solid #2a2f42; border-radius: 4px; background: #0f1117; color: #e2e8f8; cursor: pointer; white-space: nowrap; }
.shape-row button:hover:not(:disabled) { background: #1e2333; }
.shape-row button.active { background: #3b82f6; border-color: #3b82f6; color: #fff; }
.shape-row button:disabled { opacity: 0.4; cursor: default; }

#sidebar .actions { padding: 10px 12px; border-top: 1px solid #2a2f42; display: flex; gap: 8px; }
#sidebar .actions button { flex: 1; padding: 8px; border: 1px solid #2a2f42; border-radius: 6px; background: #0f1117; color: #e2e8f8; font-size: 12px; cursor: pointer; }
#sidebar .actions button:hover { background: #1e2333; }
#sidebar .actions .save { background: #3b82f6; border-color: #3b82f6; color: #fff; font-weight: 600; }
#sidebar .actions .save:hover { background: #2563eb; }

.labels-hidden .marker-label { display: none; }

#main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
#status { padding: 8px 16px; background: #0f1117; border-bottom: 1px solid #2a2f42; font-size: 12px; color: #6b7599; flex-shrink: 0; }
#image-wrap { flex: 1; overflow: auto; position: relative; background: #0f1117; }
#yard-image { display: block; max-width: none; }
#overlay { position: absolute; top: 0; left: 0; pointer-events: none; overflow: visible; }
#edge-svg { position: absolute; top: 0; left: 0; pointer-events: none; overflow: visible; }
.marker { position: absolute; width: 14px; height: 14px; border-radius: 50%; transform: translate(-50%, -50%); cursor: pointer; pointer-events: auto; }
.marker:hover { border-color: #fff !important; }
.marker.done { background: rgba(16, 185, 129, 0.7); border: 2px solid #10b981; }
.marker.active { background: rgba(59, 130, 246, 0.9); border: 2px solid #93c5fd; z-index: 10; }
.marker.size-big { border-width: 4px; }
.marker.size-small { opacity: 0.5; }
.marker-label { position: absolute; transform: translate(-50%, -100%); margin-top: -6px; font-size: 10px; color: #e2e8f8; white-space: nowrap; cursor: pointer; pointer-events: auto; text-shadow: 0 0 4px #000; }
.vertex-handle { position: absolute; width: 11px; height: 11px; border-radius: 50%; background: #3b82f6; border: 2px solid #93c5fd; transform: translate(-50%, -50%); cursor: move; pointer-events: auto; z-index: 20; }
.vertex-handle.vertex-end { width: 15px; height: 15px; background: #f59e0b; border-color: #fde68a; }
.vertex-handle:hover { transform: translate(-50%, -50%) scale(1.3); }
</style>
</head>
<body>

<div id="sidebar">
  <h2>Track Positions</h2>
  <div class="info" id="status-bar">Loading...</div>
  <input id="search" type="text" placeholder="Filter tracks...">
  <div id="controls">
    <button id="btn-labels">Labels: on</button>
    <label>Size: <input id="size-slider" type="range" min="4" max="28" value="14"></label>
    <button id="btn-node-size">Node: default</button>
    <button id="btn-auto-seed" class="primary">Auto-seed shapes</button>
  </div>
  <div id="shape-tools" class="hidden">
    <div class="shape-title">Shape tools</div>
    <div class="shape-row">
      <button id="btn-shape-mode">Edit shape: off</button>
      <button id="btn-shape-finish">Finish</button>
    </div>
    <div class="shape-row">
      <button id="btn-shape-undo">Undo point</button>
      <button id="btn-shape-flip">Flip A/B</button>
      <button id="btn-shape-clear">Clear shape</button>
    </div>
    <div class="shape-hint">Edit shape on: click the image to add points along the rail. Drag the blue/yellow handles to move points. A = start (a-side), B = end (b-side); connections follow the dashed lines.</div>
  </div>
  <div id="track-list"></div>
  <div class="actions">
    <button id="btn-save" class="save">Save</button>
    <button id="btn-reload">Reload</button>
    <button id="btn-reset">Reset</button>
  </div>
</div>

<div id="main">
  <div id="status">Click a track name, then click on the image to set its position (or draw a shape for rail tracks).</div>
  <div id="image-wrap">
    <img id="yard-image" src="" alt="Yard map">
    <svg id="edge-svg"></svg>
    <div id="overlay"></div>
  </div>
</div>

<script>
let tracks = [];
let edges = [];
let selectedId = null;
let imgW = 0, imgH = 0;
let dirty = false;
let shapeMode = false;
let activeVertex = null;

const imgEl = document.getElementById('yard-image');
const overlay = document.getElementById('overlay');
const edgeSvg = document.getElementById('edge-svg');
const listEl = document.getElementById('track-list');
const searchEl = document.getElementById('search');
const statusBar = document.getElementById('status-bar');

let showLabels = true;
let nodeSize = 14;

function setStatus(msg) { document.getElementById('status').textContent = msg; }

function hasShape(t) { return !!(t && t.shape && t.shape.length >= 2); }
function shapeMid(t) {
  if (!t.shape || !t.shape.length) return null;
  let sx = 0, sy = 0;
  for (const p of t.shape) { sx += p[0]; sy += p[1]; }
  return [sx / t.shape.length, sy / t.shape.length];
}
function syncXY(t) {
  const m = shapeMid(t);
  if (m) { t.x = Math.round(m[0]); t.y = Math.round(m[1]); }
}
function portOf(t, side) {
  if (hasShape(t)) return side === 'a' ? t.shape[0] : t.shape[t.shape.length - 1];
  if (t.x !== undefined) return [t.x, t.y];
  return null;
}
function avg(pts) {
  if (!pts.length) return null;
  let x = 0, y = 0;
  pts.forEach(p => { x += p[0]; y += p[1]; });
  return [Math.round(x / pts.length), Math.round(y / pts.length)];
}
function redraw() {
  renderMarkers();
  drawEdges();
  renderVertices();
}

document.getElementById('btn-labels').onclick = () => {
  showLabels = !showLabels;
  overlay.classList.toggle('labels-hidden', !showLabels);
  document.getElementById('btn-labels').textContent = 'Labels: ' + (showLabels ? 'on' : 'off');
  document.getElementById('btn-labels').classList.toggle('active', showLabels);
};

document.getElementById('size-slider').oninput = () => {
  nodeSize = parseInt(document.getElementById('size-slider').value);
  renderMarkers();
};

document.getElementById('btn-node-size').onclick = () => {
  if (!selectedId) { setStatus('Select a track first.'); return; }
  const t = tracks.find(x => x.id === selectedId);
  if (!t) return;
  const next = t.size === 'big' ? 'small' : t.size === 'small' ? undefined : 'big';
  t.size = next;
  dirty = true;
  setStatus(`${t.name} (#${t.id}) size set to ${next || 'default'}.`);
  document.getElementById('btn-node-size').textContent = 'Size: ' + (next || 'default');
  renderList();
  renderMarkers();
};

function updateShapeTools() {
  const box = document.getElementById('shape-tools');
  const t = tracks.find(x => x.id === selectedId);
  box.classList.toggle('hidden', !t);
  if (!t) return;
  const modeBtn = document.getElementById('btn-shape-mode');
  modeBtn.textContent = 'Edit shape: ' + (shapeMode ? 'on' : 'off');
  modeBtn.classList.toggle('active', shapeMode);
  document.getElementById('btn-shape-undo').disabled = !shapeMode;
  document.getElementById('btn-shape-flip').disabled = !shapeMode || !hasShape(t);
  document.getElementById('btn-shape-clear').disabled = !shapeMode;
}

document.getElementById('btn-shape-mode').onclick = () => {
  if (!selectedId) return;
  shapeMode = !shapeMode;
  const t = tracks.find(x => x.id === selectedId);
  if (shapeMode && t && !t.shape) t.shape = [];
  updateShapeTools();
  setStatus(shapeMode ? `Shape mode on — click along the rail of ${t.name} to add points.` : 'Shape mode off.');
  redraw();
};

document.getElementById('btn-shape-finish').onclick = () => {
  shapeMode = false;
  updateShapeTools();
  const t = tracks.find(x => x.id === selectedId);
  setStatus(t ? `Finished ${t.name}. Edit shape is off.` : 'Finished.');
  redraw();
};

document.getElementById('btn-shape-undo').onclick = () => {
  const t = tracks.find(x => x.id === selectedId);
  if (!t || !t.shape || !t.shape.length) return;
  t.shape.pop();
  if (!t.shape.length) delete t.shape;
  syncXY(t);
  dirty = true;
  setStatus(`Undid last point (${t.name} now has ${t.shape ? t.shape.length : 0} points).`);
  redraw();
};

document.getElementById('btn-shape-flip').onclick = () => {
  const t = tracks.find(x => x.id === selectedId);
  if (!t || !hasShape(t)) return;
  t.shape.reverse();
  dirty = true;
  setStatus(`Flipped A/B for ${t.name} — A is now at the start point.`);
  redraw();
};

document.getElementById('btn-shape-clear').onclick = () => {
  const t = tracks.find(x => x.id === selectedId);
  if (!t) return;
  delete t.shape;
  dirty = true;
  setStatus(`Cleared shape for ${t.name}.`);
  redraw();
};

document.getElementById('btn-auto-seed').onclick = () => {
  let count = 0;
  const posMap = {};
  tracks.forEach(t => { if (t.x !== undefined) posMap[t.id] = [t.x, t.y]; });
  tracks.forEach(t => {
    if (t.type !== 'RailRoad') return;
    if (hasShape(t)) return;
    const aPts = (t.aSide || []).map(id => posMap[id]).filter(Boolean);
    const bPts = (t.bSide || []).map(id => posMap[id]).filter(Boolean);
    const aAvg = avg(aPts), bAvg = avg(bPts);
    const mid = t.x !== undefined ? [t.x, t.y] : null;
    if (aAvg && bAvg) t.shape = [aAvg, bAvg];
    else if (aAvg && mid) t.shape = [aAvg, mid];
    else if (bAvg && mid) t.shape = [mid, bAvg];
    else return;
    syncXY(t);
    count++;
  });
  if (count) { dirty = true; redraw(); renderList(); }
  setStatus(`Seeded ${count} straight shapes. For curved tracks: select one, keep Edit shape on, and click along the rail to add points.`);
};

async function load() {
  const resp = await fetch('/api/data');
  const data = await resp.json();
  tracks = data.tracks;
  edges = data.edges || [];
  imgW = data.imageWidth;
  imgH = data.imageHeight;
  tracks.forEach(t => { if (hasShape(t)) syncXY(t); });

  if (data.image_path) {
    imgEl.src = '/api/image';
    imgEl.onload = () => {
      if (!imgW || !imgH) {
        imgW = imgEl.naturalWidth;
        imgH = imgEl.naturalHeight;
      }
      sizeOverlay();
      redraw();
    };
  }
  const positioned = tracks.filter(t => t.x !== undefined).length;
  const shaped = tracks.filter(t => hasShape(t)).length;
  statusBar.textContent = `${tracks.length} tracks (${positioned} positioned, ${shaped} with shapes)`;

  renderList();
  if (imgW && imgH) { redraw(); }
}

function sizeOverlay() {
  if (!imgEl.complete) return;
  overlay.style.width = imgEl.offsetWidth + 'px';
  overlay.style.height = imgEl.offsetHeight + 'px';
}

function renderList() {
  const q = searchEl.value.toLowerCase();
  listEl.innerHTML = '';
  tracks.forEach(t => {
    const match = t.id.toLowerCase().includes(q) || t.name.toLowerCase().includes(q) || t.type.toLowerCase().includes(q);
    if (q && !match) return;
    const div = document.createElement('div');
    div.className = 'track-item' + (t.id === selectedId ? ' selected' : '');
    div.dataset.id = t.id;
    const hasPos = t.x !== undefined;
    const posText = !hasPos ? '' : (hasShape(t) ? 'shape ' + t.shape.length : '(' + t.x + ', ' + t.y + ')');
    div.innerHTML = `
      <span class="dot ${hasPos ? 'done' : 'empty'}"></span>
      <span class="id">${t.name}</span>
      <span class="id-small">#${t.id}</span>
      <span class="type">${t.type}</span>
      <span class="pos">${posText}</span>
      ${hasShape(t) ? `<span class="size-badge">shape</span>` : (t.size ? `<span class="size-badge">${t.size}</span>` : '')}
    `;
    div.onclick = () => selectTrack(t.id);
    listEl.appendChild(div);
  });
}

function renderMarkers() {
  overlay.innerHTML = '';
  if (!imgW || !imgH) return;
  const dispW = imgEl.offsetWidth;
  const dispH = imgEl.offsetHeight;
  if (!dispW || !dispH) return;
  overlay.style.width = dispW + 'px';
  overlay.style.height = dispH + 'px';
  const scaleX = dispW / imgW;
  const scaleY = dispH / imgH;

  tracks.forEach(t => {
    if (t.x === undefined) return;
    const sx = t.x * scaleX;
    const sy = t.y * scaleY;
    if (!hasShape(t)) {
      const mk = document.createElement('div');
      mk.className = 'marker' + (t.id === selectedId ? ' active' : ' done') + (t.size === 'big' ? ' size-big' : '') + (t.size === 'small' ? ' size-small' : '');
      mk.style.left = sx + 'px';
      mk.style.top = sy + 'px';
      const sz = t.size === 'big' ? nodeSize + 6 : t.size === 'small' ? nodeSize - 4 : nodeSize;
      mk.style.width = sz + 'px';
      mk.style.height = sz + 'px';
      mk.onclick = (e) => { e.stopPropagation(); selectTrack(t.id); };
      overlay.appendChild(mk);
    }
    const lb = document.createElement('div');
    lb.className = 'marker-label';
    lb.style.left = sx + 'px';
    lb.style.top = sy + 'px';
    lb.textContent = t.name || t.id;
    lb.onclick = (e) => { e.stopPropagation(); selectTrack(t.id); };
    overlay.appendChild(lb);
  });
}

function drawEdges() {
  edgeSvg.innerHTML = '';
  if (!imgW || !imgH) return;
  const dispW = imgEl.offsetWidth;
  const dispH = imgEl.offsetHeight;
  if (!dispW || !dispH) return;
  edgeSvg.setAttribute('width', dispW);
  edgeSvg.setAttribute('height', dispH);
  edgeSvg.style.width = dispW + 'px';
  edgeSvg.style.height = dispH + 'px';
  const scaleX = dispW / imgW;
  const scaleY = dispH / imgH;
  const ns = 'http://www.w3.org/2000/svg';

  tracks.forEach(t => {
    if (!hasShape(t)) return;
    const pts = t.shape.map(p => (p[0] * scaleX) + ',' + (p[1] * scaleY)).join(' ');
    const poly = document.createElementNS(ns, 'polyline');
    poly.setAttribute('points', pts);
    poly.setAttribute('fill', 'none');
    poly.setAttribute('stroke', t.id === selectedId ? '#3b82f6' : '#10b981');
    poly.setAttribute('stroke-width', t.id === selectedId ? 4 : 3);
    poly.setAttribute('stroke-linejoin', 'round');
    poly.setAttribute('stroke-linecap', 'round');
    poly.setAttribute('opacity', t.id === selectedId ? 1 : 0.5);
    edgeSvg.appendChild(poly);
  });

  edges.forEach(e => {
    const a = tracks.find(x => x.id === e.from);
    const b = tracks.find(x => x.id === e.to);
    if (!a || !b) return;
    const ap = portOf(a, e.fromSide);
    const bp = portOf(b, e.toSide);
    if (!ap || !bp) return;
    const line = document.createElementNS(ns, 'line');
    line.setAttribute('x1', ap[0] * scaleX);
    line.setAttribute('y1', ap[1] * scaleY);
    line.setAttribute('x2', bp[0] * scaleX);
    line.setAttribute('y2', bp[1] * scaleY);
    line.setAttribute('stroke', '#4a5568');
    line.setAttribute('stroke-width', '1.5');
    line.setAttribute('stroke-dasharray', '4 3');
    edgeSvg.appendChild(line);
  });

  const sel = tracks.find(x => x.id === selectedId);
  if (sel && hasShape(sel)) {
    addEndpointLabel(edgeSvg, ns, sel.shape[0][0] * scaleX, sel.shape[0][1] * scaleY, 'A', '#3b82f6');
    const last = sel.shape[sel.shape.length - 1];
    addEndpointLabel(edgeSvg, ns, last[0] * scaleX, last[1] * scaleY, 'B', '#f59e0b');
  }
}

function addEndpointLabel(svg, ns, x, y, text, color) {
  const g = document.createElementNS(ns, 'g');
  const circle = document.createElementNS(ns, 'circle');
  circle.setAttribute('cx', x); circle.setAttribute('cy', y); circle.setAttribute('r', 8);
  circle.setAttribute('fill', '#0f1117'); circle.setAttribute('stroke', color); circle.setAttribute('stroke-width', 1.5);
  g.appendChild(circle);
  const txt = document.createElementNS(ns, 'text');
  txt.setAttribute('x', x); txt.setAttribute('y', y + 3.5);
  txt.setAttribute('text-anchor', 'middle'); txt.setAttribute('font-size', '10'); txt.setAttribute('font-weight', '700');
  txt.setAttribute('fill', color);
  txt.textContent = text;
  g.appendChild(txt);
  svg.appendChild(g);
}

function renderVertices() {
  overlay.querySelectorAll('.vertex-handle').forEach(el => el.remove());
  if (!shapeMode) return;
  const t = tracks.find(x => x.id === selectedId);
  if (!t || !hasShape(t)) return;
  const dispW = imgEl.offsetWidth, dispH = imgEl.offsetHeight;
  if (!dispW || !dispH) return;
  const scaleX = dispW / imgW, scaleY = dispH / imgH;
  t.shape.forEach((p, i) => {
    const h = document.createElement('div');
    h.className = 'vertex-handle' + (i === 0 || i === t.shape.length - 1 ? ' vertex-end' : '');
    h.style.left = (p[0] * scaleX) + 'px';
    h.style.top = (p[1] * scaleY) + 'px';
    h.onmousedown = (e) => startVertexDrag(e, t, i);
    overlay.appendChild(h);
  });
}

function startVertexDrag(e, t, index) {
  e.preventDefault();
  e.stopPropagation();
  activeVertex = index;
  const rect = imgEl.getBoundingClientRect();
  const move = (ev) => {
    const x = Math.round((ev.clientX - rect.left) / rect.width * imgW);
    const y = Math.round((ev.clientY - rect.top) / rect.height * imgH);
    t.shape[index] = [x, y];
    syncXY(t);
    dirty = true;
    renderVertices();
    drawEdges();
  };
  const up = () => {
    document.removeEventListener('mousemove', move);
    document.removeEventListener('mouseup', up);
    activeVertex = null;
    setStatus(`${t.name} (#${t.id}) point ${index + 1} moved to (${t.shape[index][0]}, ${t.shape[index][1]}).`);
  };
  document.addEventListener('mousemove', move);
  document.addEventListener('mouseup', up);
}

function selectTrack(id) {
  selectedId = id;
  const t = tracks.find(x => x.id === id);
  renderList();
  const items = listEl.querySelectorAll('.track-item');
  for (const item of items) {
    if (item.dataset.id === id) { item.scrollIntoView({ block: 'nearest' }); break; }
  }
  if (t && hasShape(t)) {
    shapeMode = true;
    setStatus(`Selected ${t.name} (#${t.id}) — shape with ${t.shape.length} points. A = start (a-side), B = end (b-side). Click adds points, drag handles to move them.`);
  } else if (t && t.x !== undefined) {
    shapeMode = false;
    setStatus(`Selected ${t.name} (#${t.id}) — currently at (${t.x}, ${t.y}). Click the image to move it, or turn on Edit shape to draw one.`);
  } else {
    shapeMode = false;
    setStatus(`Selected ${t.name} (#${t.id}) — click on the image to set its position.`);
  }
  document.getElementById('btn-node-size').textContent = 'Size: ' + (t && t.size ? t.size : 'default');
  updateShapeTools();
  redraw();
}

imgEl.onclick = (e) => {
  if (!selectedId) { setStatus('First click a track name in the sidebar to select it.'); return; }
  if (activeVertex !== null) return;
  const rect = imgEl.getBoundingClientRect();
  const x = Math.round((e.clientX - rect.left) / rect.width * imgW);
  const y = Math.round((e.clientY - rect.top) / rect.height * imgH);
  const t = tracks.find(x => x.id === selectedId);
  if (!t) return;
  if (shapeMode) {
    if (!t.shape) t.shape = [];
    t.shape.push([x, y]);
    syncXY(t);
    dirty = true;
    setStatus(`${t.name} (#${t.id}) point ${t.shape.length} added at (${x}, ${y}). Click more points, drag handles, or Finish.`);
    renderList();
    renderMarkers();
    drawEdges();
    renderVertices();
  } else {
    if (hasShape(t)) { setStatus(`${t.name} (#${t.id}) already has a shape — turn on Edit shape to change it.`); return; }
    t.x = x;
    t.y = y;
    dirty = true;
    setStatus(`${t.name} (#${t.id}) set to (${x}, ${y}).`);
    renderList();
    renderMarkers();
    drawEdges();
  }
};

window.onresize = () => { sizeOverlay(); redraw(); };

document.getElementById('btn-save').onclick = async () => {
  const payload = { tracks: {} };
  tracks.forEach(t => {
    if (t.x !== undefined) {
      const entry = { x: t.x, y: t.y, name: t.name || t.id };
      if (t.size) entry.size = t.size;
      if (hasShape(t)) entry.shape = t.shape.map(p => [Math.round(p[0]), Math.round(p[1])]);
      payload.tracks[t.id] = entry;
    }
  });
  const resp = await fetch('/api/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const result = await resp.json();
  if (result.ok) {
    dirty = false;
    setStatus('Saved!');
    setTimeout(() => {
      const pos = tracks.filter(t => t.x !== undefined).length;
      const shaped = tracks.filter(t => hasShape(t)).length;
      setStatus(`Saved — ${pos}/${tracks.length} tracks positioned, ${shaped} with shapes.`);
    }, 1500);
  }
};

document.getElementById('btn-reload').onclick = () => {
  if (dirty && !confirm('Discard unsaved changes and reload from disk?')) return;
  selectedId = null;
  shapeMode = false;
  dirty = false;
  setStatus('Reloading from disk...');
  load();
};

document.getElementById('btn-reset').onclick = () => {
  if (!confirm('Remove all positions for the selected track?')) return;
  const t = tracks.find(x => x.id === selectedId);
  if (t) {
    delete t.x;
    delete t.y;
    delete t.shape;
    shapeMode = false;
    dirty = true;
    setStatus(`${t.name} (#${t.id}) position and shape cleared.`);
    renderList();
    renderMarkers();
    drawEdges();
    renderVertices();
    updateShapeTools();
  }
};

searchEl.oninput = renderList;

load();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
