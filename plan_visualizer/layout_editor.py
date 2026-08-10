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
            current["tracks"] = data["tracks"]
            with open(layout_path, "w", encoding="utf-8") as f:
                json.dump(current, f, indent=2)
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
        abs_image = location_dir / "kleine_binckhorst.png"
        if not abs_image.exists():
            abs_image = location_dir / "location.png"
            if not abs_image.exists():
                abs_image = None

    # read all track names from location file
    location = load_json(location_path)
    all_tracks = []
    for part in location.get("trackParts", []):
        name = str(part.get("name", ""))
        if name:
            entry = {"id": name, "name": name, "type": part.get("type", "")}
            if name in layout_data.get("tracks", {}):
                entry["x"] = layout_data["tracks"][name]["x"]
                entry["y"] = layout_data["tracks"][name]["y"]
            all_tracks.append(entry)

    # build edges from aSide/bSide references
    id_to_name = {tp["id"]: tp["name"] for tp in location.get("trackParts", [])}
    edge_set = set()
    for tp in location.get("trackParts", []):
        name = tp["name"]
        for ref in tp.get("aSide", []):
            other = id_to_name.get(ref)
            if other and name != other:
                edge_set.add(tuple(sorted([name, other])))
        for ref in tp.get("bSide", []):
            other = id_to_name.get(ref)
            if other and name != other:
                edge_set.add(tuple(sorted([name, other])))
    edges = [{"from": a, "to": b} for a, b in sorted(edge_set)]

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

    editor_data = {
        "tracks": sorted_tracks,
        "edges": edges,
        "image_path": str(abs_image) if abs_image else None,
        "imageWidth": img_width,
        "imageHeight": img_height,
        "layout_path": str(layout_path),
        "location_name": args.location_name,
    }

    PORT = args.port
    server = socketserver.TCPServer(("127.0.0.1", PORT), Handler)
    server.editor_data = editor_data
    server.allow_reuse_address = True
    server.server_port = PORT

    print(f"Layout editor: http://127.0.0.1:{PORT}")
    print(f"Editing: {layout_path}")
    print(f"Tracks: {len(all_tracks)} ({len(positioned)} positioned, {len(unpositioned)} unpositioned)")
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
.track-item .type { color: #6b7599; font-size: 10px; }
.track-item .pos { color: #6b7599; font-size: 10px; margin-left: auto; }

#controls { padding: 8px 12px; border-bottom: 1px solid #2a2f42; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
#controls label { font-size: 11px; color: #6b7599; display: flex; align-items: center; gap: 4px; }
#controls button { padding: 4px 10px; border: 1px solid #2a2f42; border-radius: 4px; background: #0f1117; color: #e2e8f8; font-size: 11px; cursor: pointer; }
#controls button:hover { background: #1e2333; }
#controls button.active { background: #3b82f6; border-color: #3b82f6; color: #fff; }
#size-slider { width: 80px; accent-color: #3b82f6; cursor: pointer; }

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
.marker-label { position: absolute; transform: translate(-50%, -100%); margin-top: -6px; font-size: 10px; color: #e2e8f8; white-space: nowrap; cursor: pointer; pointer-events: auto; text-shadow: 0 0 4px #000; }
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
  </div>
  <div id="track-list"></div>
  <div class="actions">
    <button id="btn-save" class="save">Save</button>
    <button id="btn-reset">Reset</button>
  </div>
</div>

<div id="main">
  <div id="status">Click a track name, then click on the image to set its position.</div>
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

const imgEl = document.getElementById('yard-image');
const overlay = document.getElementById('overlay');
const edgeSvg = document.getElementById('edge-svg');
const listEl = document.getElementById('track-list');
const searchEl = document.getElementById('search');
const statusBar = document.getElementById('status-bar');

let showLabels = true;
let nodeSize = 14;

document.getElementById('btn-labels').onclick = () => {
  showLabels = !showLabels;
  overlay.classList.toggle('labels-hidden', !showLabels);
  document.getElementById('btn-labels').textContent = 'Labels: ' + (showLabels ? 'on' : 'off');
  document.getElementById('btn-labels').classList.toggle('active', showLabels);
};

document.getElementById('size-slider').oninput = () => {
  nodeSize = parseInt(document.getElementById('size-slider').value);
  renderMarkers();
  drawEdges();
};

async function load() {
  const resp = await fetch('/api/data');
  const data = await resp.json();
  tracks = data.tracks;
  edges = data.edges || [];
  imgW = data.imageWidth;
  imgH = data.imageHeight;

  if (data.image_path) {
    imgEl.src = '/api/image';
    imgEl.onload = () => {
      if (!imgW || !imgH) {
        imgW = imgEl.naturalWidth;
        imgH = imgEl.naturalHeight;
      }
      sizeOverlay();
      renderMarkers();
      drawEdges();
    };
  }
  const positioned = tracks.filter(t => t.x !== undefined).length;
  statusBar.textContent = `${tracks.length} tracks (${positioned} positioned, ${tracks.length - positioned} remaining)`;

  renderList();
  if (imgW && imgH) { renderMarkers(); drawEdges(); }
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
    const hasPos = t.x !== undefined;
    div.innerHTML = `
      <span class="dot ${hasPos ? 'done' : 'empty'}"></span>
      <span class="id">${t.id}</span>
      <span class="type">${t.type}</span>
      <span class="pos">${hasPos ? '(' + t.x + ', ' + t.y + ')' : ''}</span>
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
    const mk = document.createElement('div');
    mk.className = 'marker' + (t.id === selectedId ? ' active' : ' done');
    mk.style.left = sx + 'px';
    mk.style.top = sy + 'px';
    mk.style.width = nodeSize + 'px';
    mk.style.height = nodeSize + 'px';
    mk.onclick = (e) => { e.stopPropagation(); selectTrack(t.id); };
    overlay.appendChild(mk);
    const lb = document.createElement('div');
    lb.className = 'marker-label';
    lb.style.left = sx + 'px';
    lb.style.top = sy + 'px';
    lb.textContent = t.id;
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
  const posMap = {};
  tracks.forEach(t => { if (t.x !== undefined) posMap[t.id] = { x: t.x * scaleX, y: t.y * scaleY }; });

  const ns = 'http://www.w3.org/2000/svg';
  edges.forEach(e => {
    const a = posMap[e.from];
    const b = posMap[e.to];
    if (!a || !b) return;
    const line = document.createElementNS(ns, 'line');
    line.setAttribute('x1', a.x);
    line.setAttribute('y1', a.y);
    line.setAttribute('x2', b.x);
    line.setAttribute('y2', b.y);
    line.setAttribute('stroke', '#4a5568');
    line.setAttribute('stroke-width', '2');
    line.setAttribute('stroke-linecap', 'round');
    edgeSvg.appendChild(line);
  });
}

function selectTrack(id) {
  selectedId = id;
  renderList();
  const items = listEl.querySelectorAll('.track-item');
  for (const item of items) {
    if (item.querySelector('.id').textContent === id) {
      item.scrollIntoView({ block: 'nearest' });
      break;
    }
  }
  const t = tracks.find(x => x.id === id);
  if (t && t.x !== undefined) {
    document.getElementById('status').textContent = `Selected ${id} — currently at (${t.x}, ${t.y}). Click the image to move it, or click a different track.`;
  } else {
    document.getElementById('status').textContent = `Selected ${id} — click on the image to set its position.`;
  }
}

imgEl.onclick = (e) => {
  if (!selectedId) {
    document.getElementById('status').textContent = 'First click a track name in the sidebar to select it.';
    return;
  }
  const rect = imgEl.getBoundingClientRect();
  const x = Math.round((e.clientX - rect.left) / rect.width * imgW);
  const y = Math.round((e.clientY - rect.top) / rect.height * imgH);
  const t = tracks.find(t => t.id === selectedId);
  if (t) {
    t.x = x;
    t.y = y;
    dirty = true;
    document.getElementById('status').textContent = `${selectedId} set to (${x}, ${y}).`;
    renderList();
    renderMarkers();
    drawEdges();
  }
};

window.onresize = () => { sizeOverlay(); renderMarkers(); drawEdges(); };

document.getElementById('btn-save').onclick = async () => {
  const payload = { tracks: {} };
  tracks.forEach(t => {
    if (t.x !== undefined) {
      payload.tracks[t.id] = { x: t.x, y: t.y };
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
    document.getElementById('status').textContent = 'Saved!';
    setTimeout(() => {
      const pos = tracks.filter(t => t.x !== undefined).length;
      document.getElementById('status').textContent = `Saved — ${pos}/${tracks.length} tracks positioned.`;
    }, 1500);
  }
};

document.getElementById('btn-reset').onclick = () => {
  if (!confirm('Remove all positions for the selected track?')) return;
  const t = tracks.find(t => t.id === selectedId);
  if (t) {
    delete t.x;
    delete t.y;
    dirty = true;
    document.getElementById('status').textContent = `${selectedId} position cleared.`;
    renderList();
    renderMarkers();
    drawEdges();
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
