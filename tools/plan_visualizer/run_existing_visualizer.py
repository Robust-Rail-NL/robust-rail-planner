import argparse
import http.server
import socket
import socketserver
import subprocess
import sys
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate and serve a visualizer for an existing Robust-Rail scenario/plan pair."
    )
    parser.add_argument("--location-name", default="Location_KleineBinckhorst")
    parser.add_argument("--scenario", default="scenario_solver_example2.json")
    parser.add_argument("--plan", default="plan_example2.json")
    parser.add_argument("--layout", default=None, help="Optional layout file. Defaults from location name.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-serve", action="store_true")
    return parser.parse_args()


def port_is_busy(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def main():
    args = parse_args()

    script_dir = Path(__file__).resolve().parent
    planning_repo = script_dir.parents[1]
    workspace_root = script_dir.parents[3]
    location_dir = workspace_root / "Robust-Rail-NL" / "scenario-planning-inputs" / args.location_name

    location = location_dir / "location_solver.json"
    scenario = location_dir / "scenarios" / args.scenario
    plan = location_dir / "plans" / args.plan
    if args.layout:
        layout = Path(args.layout)
    elif args.location_name == "Location_SimpleService":
        layout = script_dir / "layouts" / "simple_service.json"
    else:
        layout = script_dir / "layouts" / "kleine_binckhorst.json"
    data_dir = planning_repo / "data"
    output_name = f"{Path(args.scenario).stem}_{Path(args.plan).stem}_visualizer.html"
    output = data_dir / output_name

    for required in [location, scenario, plan, layout]:
        if not required.exists():
            raise FileNotFoundError(required)

    data_dir.mkdir(exist_ok=True)

    command = [
        sys.executable,
        str(script_dir / "visualize_plan.py"),
        "--location",
        str(location),
        "--scenario",
        str(scenario),
        "--plan",
        str(plan),
        "--layout",
        str(layout),
        "--output",
        str(output),
    ]

    print("Generating visualizer...", flush=True)
    subprocess.run(command, check=True)

    url = f"http://127.0.0.1:{args.port}/{output.name}"
    print(f"\nVisualizer ready:\n{url}\n")

    if args.no_serve:
        return

    if port_is_busy(args.port):
        print(f"Port {args.port} is already serving something. Open the URL above.")
        return

    print("Serving data folder. Press Ctrl+C to stop.")
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", args.port), http.server.SimpleHTTPRequestHandler) as server:
        original_cwd = Path.cwd()
        try:
            import os

            os.chdir(data_dir)
            server.serve_forever()
        finally:
            os.chdir(original_cwd)


if __name__ == "__main__":
    main()
