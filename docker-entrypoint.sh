#!/bin/sh
# Dispatch between the two things this image can do.
#
# The planner is the default and keeps its exact argument list, because
# robust-rail-general/run_planner.py invokes the image with
# `--location ... --scenario ... --planner ... --output ...` and nothing else.
# Any argument list that starts with a flag, or is empty, therefore goes
# straight to main.py — adding the visualizer must not change that contract.
#
#   docker run ... planner:latest --location ... --scenario ... --output ...
#   docker run ... -p 8767:8767 planner:latest visualizer --inputs-root /app/database
set -eu

case "${1:-}" in
    visualizer)
        shift
        # 0.0.0.0 rather than the script's 127.0.0.1 default: a container-local
        # loopback bind accepts no connection from the host, and the symptom is
        # a published port that silently refuses.
        exec python3 plan_visualizer/run_existing_visualizer.py --host 0.0.0.0 "$@"
        ;;
    plan)
        shift
        exec python3 main.py "$@"
        ;;
    *)
        exec python3 main.py "$@"
        ;;
esac
