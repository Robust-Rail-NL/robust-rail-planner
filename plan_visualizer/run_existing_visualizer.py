import argparse
import http.server
import json
import socketserver
import subprocess
import sys
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(
        description="Web UI to generate and serve a visualizer for Robust-Rail scenarios."
    )
    parser.add_argument("--port", type=int, default=8767)
    # Both default to the local checkout layout, so running this from a clone
    # needs no arguments. They exist for the container, where the repos are not
    # siblings on disk: the inputs are bind-mounted, and 127.0.0.1 inside a
    # container is not reachable from the host.
    parser.add_argument(
        "--inputs-root", type=Path, default=None,
        help="Directory holding the Location_* folders (default: the sibling "
             "scenario-planning-inputs checkout).",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=None,
        help="Where to write generated visualizer HTML (default: ./data).",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="Address to bind. Use 0.0.0.0 in a container to expose the port.",
    )
    return parser.parse_args()


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def json_bytes(obj):
    return json.dumps(obj).encode("utf-8")


def _list_relative(base, patterns):
    """Files matching any pattern, as paths relative to base.

    Relative paths rather than bare names because the classified corpus under
    fixtures/{feasible,infeasible,unresolved}/ holds scenarios and plans with the
    same shape as scenarios/ and plans/, and a bare name could not say which
    directory it came from. It also makes the picker show the bucket, which is
    the interesting part of a fixture.
    """
    found = set()
    for pattern in patterns:
        for path in base.glob(pattern):
            if path.is_file():
                found.add(path.relative_to(base).as_posix())
    return sorted(found)


def _safe_join(base, relative):
    """Resolve `relative` under `base`, refusing anything that escapes it.

    The picker only ever sends paths this server listed, but it binds 0.0.0.0 in
    the container, so the request is reachable from outside the host it runs on.
    """
    target = (base / relative).resolve()
    if not target.is_relative_to(base.resolve()):
        raise ValueError(f"path escapes the location directory: {relative}")
    return target


HTML = None


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML.encode("utf-8"))
            return
        if self.path == "/api/locations":
            base = self.server.inputs_root
            dirs = sorted(
                d.name for d in base.iterdir() if d.is_dir() and (d / "location.json").exists()
            ) if base.is_dir() else []
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json_bytes(dirs))
            return
        if self.path.startswith("/api/location-files"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            loc = (qs.get("location") or [""])[0]
            base = self.server.inputs_root / loc
            scenarios = _list_relative(base, ["scenarios/*.json", "fixtures/*/scenario_*.json"])
            plans = _list_relative(base, ["plans/*.json", "fixtures/*/plan_*.json"])
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json_bytes({"scenarios": scenarios, "plans": plans}))
            return
        if self.path == "/api/visualizer-html":
            html_path = self.server.vis_html_path
            if html_path and Path(html_path).exists():
                with open(html_path, "r", encoding="utf-8") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(content.encode("utf-8"))
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
        if self.path == "/api/generate":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            data = json.loads(body)
            loc_name = data.get("location", "Location_KleineBinckhorst")
            scenario_name = data.get("scenario", "")
            plan_name = data.get("plan", "")
            location_dir = self.server.inputs_root / loc_name
            location_path = location_dir / "location.json"
            # Both names are paths relative to the location directory now, so
            # that fixtures/<bucket>/ entries can be selected alongside the
            # scenarios/ and plans/ ones.
            try:
                scenario_path = _safe_join(location_dir, scenario_name)
                plan_path = _safe_join(location_dir, plan_name)
            except ValueError as exc:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json_bytes({"error": str(exc)}))
                return
            data_dir = self.server.output_dir
            output_name = f"{Path(scenario_name).stem}_{Path(plan_name).stem}_visualizer.html"
            output_path = data_dir / output_name
            if loc_name == "Location_SimpleService":
                layout_path = self.server.script_dir / "layouts" / "simple_service.json"
            else:
                layout_path = self.server.script_dir / "layouts" / "kleine_binckhorst.json"
            if not location_path.exists():
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json_bytes({"error": f"Location not found: {location_path}"}))
                return
            data_dir.mkdir(parents=True, exist_ok=True)
            cmd = [
                sys.executable,
                str(self.server.script_dir / "visualize_plan.py"),
                "--location", str(location_path),
                "--scenario", str(scenario_path),
                "--plan", str(plan_path),
                "--layout", str(layout_path),
                "--output", str(output_path),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json_bytes({"error": result.stderr or result.stdout}))
                return
            self.server.vis_html_path = str(output_path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json_bytes({"ok": True}))
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        if "/api/" in str(args[0]):
            super().log_message(format, *args)


def main():
    args = parse_args()

    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    inputs_root = args.inputs_root or repo_root.parent / "scenario-planning-inputs"
    output_dir = args.output_dir or repo_root / "data"

    PORT = args.port
    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer((args.host, PORT), Handler)
    server.server_port = PORT
    server.inputs_root = inputs_root
    server.output_dir = output_dir
    server.script_dir = script_dir
    server.vis_html_path = None

    if not inputs_root.is_dir():
        # Worth saying up front: the UI's location list would otherwise just come
        # back empty, which looks like there is nothing to visualise.
        print(f"WARNING: no inputs at {inputs_root} — pass --inputs-root.",
              file=sys.stderr)

    print(f"Visualizer server: http://{args.host}:{PORT}")
    print("Choose location/scenario/plan and click Generate & View.")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Visualizer</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: system-ui, sans-serif; background: #1a1d23; color: #e2e8f8; display: flex; flex-direction: column; height: 100vh; overflow: hidden; }

#controls { padding: 14px 20px; background: #181c27; border-bottom: 1px solid #2a2f42; display: flex; gap: 12px; align-items: flex-end; flex-wrap: wrap; flex-shrink: 0; }
#controls label { font-size: 11px; color: #6b7599; display: flex; flex-direction: column; gap: 3px; }
#controls select, #controls input { padding: 6px 10px; border: 1px solid #2a2f42; border-radius: 5px; background: #0f1117; color: #e2e8f8; font-size: 12px; outline: none; min-width: 160px; }
#controls select:focus { border-color: #3b82f6; }
#controls button { padding: 6px 18px; border: none; border-radius: 5px; background: #3b82f6; color: #fff; font-size: 12px; font-weight: 600; cursor: pointer; }
#controls button:hover { background: #2563eb; }
#controls button:disabled { opacity: 0.4; cursor: default; }
#status { font-size: 11px; color: #6b7599; align-self: center; margin-left: auto; }

#frame-wrap { flex: 1; overflow: hidden; background: #0f1117; }
#vis-frame { width: 100%; height: 100%; border: none; }
</style>
</head>
<body>

<div id="controls">
  <label>Location
    <select id="vis-location"></select>
  </label>
  <label>Scenario
    <select id="vis-scenario"></select>
  </label>
  <label>Plan
    <select id="vis-plan"></select>
  </label>
  <button id="btn-generate">Generate & View</button>
  <span id="status"></span>
</div>
<div id="frame-wrap">
  <iframe id="vis-frame"></iframe>
</div>

<script>
async function loadLocations() {
  const resp = await fetch('/api/locations');
  const locs = await resp.json();
  const sel = document.getElementById('vis-location');
  sel.innerHTML = locs.map(l => `<option value="${l}">${l.replace('Location_', '')}</option>`).join('');
  sel.onchange = loadFiles;
  await loadFiles();
}

async function loadFiles() {
  const loc = document.getElementById('vis-location').value;
  const selSce = document.getElementById('vis-scenario');
  const selPla = document.getElementById('vis-plan');
  selSce.innerHTML = '<option>Loading...</option>';
  selPla.innerHTML = '<option>Loading...</option>';
  const resp = await fetch(`/api/location-files?location=${encodeURIComponent(loc)}`);
  const data = await resp.json();
  selSce.innerHTML = data.scenarios.map(s => `<option value="${s}">${s}</option>`).join('');
  selPla.innerHTML = data.plans.map(p => `<option value="${p}">${p}</option>`).join('');
}

document.getElementById('btn-generate').onclick = async () => {
  const btn = document.getElementById('btn-generate');
  const status = document.getElementById('status');
  btn.disabled = true;
  status.textContent = 'Generating...';
  const resp = await fetch('/api/generate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      location: document.getElementById('vis-location').value,
      scenario: document.getElementById('vis-scenario').value,
      plan: document.getElementById('vis-plan').value,
    }),
  });
  const result = await resp.json();
  btn.disabled = false;
  if (result.error) {
    status.textContent = 'Error: ' + result.error;
  } else {
    status.textContent = 'Loading...';
    document.getElementById('vis-frame').src = '/api/visualizer-html';
    status.textContent = 'Ready';
  }
};

loadLocations();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
