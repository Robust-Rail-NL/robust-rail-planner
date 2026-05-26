#!/bin/bash
set -e  # stop on any error

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

echo "Installing Python dependencies..."
uv sync

echo "Installing Julia dependencies..."
julia --project="$REPO_ROOT" -e '
using Pkg
Pkg.activate("'$REPO_ROOT'")
Pkg.instantiate()
Pkg.status()
'

echo "Setup complete!"