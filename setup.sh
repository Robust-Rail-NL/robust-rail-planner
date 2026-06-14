#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_ROOT"
ENV_NAME="robust-rail-planning"

echo "Creating/updating Conda environment..."
conda env update --name "$ENV_NAME" --file env.yml --prune

# Activate so the verification below runs inside the env
eval "$(conda shell.bash hook)"
conda activate "$ENV_NAME"

echo "Checking Java (needed by ENHSP)..."
java -version || echo "WARNING: java not found on PATH — ENHSP will fail."

echo "Checking which planning engines registered..."
python - <<'PY'
import sys
from unified_planning.shortcuts import get_environment
engines = sorted(get_environment().factory.engines)
print("Registered engines:", engines)
if "enhsp" not in engines:
    print("ERROR: enhsp not registered", file=sys.stderr)
    sys.exit(1)
print("  enhsp OK")
PY

echo "Installing Julia dependencies..."
julia --project="$REPO_ROOT" -e '
using Pkg
Pkg.activate("'"$REPO_ROOT"'")
Pkg.resolve()
Pkg.instantiate()
Pkg.precompile()
Pkg.status()
'

echo "Setup complete."