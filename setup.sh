#!/bin/bash
set -e

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_ROOT"

ENV_NAME="robust-rail-planning"

echo "Creating/updating Conda environment..."
conda env update --name "$ENV_NAME" --file env.yml --prune

echo "Installing Julia dependencies..."
julia --project="$REPO_ROOT" -e '
using Pkg
Pkg.activate("'"$REPO_ROOT"'")
Pkg.resolve()
Pkg.instantiate()
Pkg.precompile()
Pkg.status()
'

echo "Setup complete!"
echo ""
echo "Activate the Python environment with:"
echo "  conda activate robust-rail-planning"