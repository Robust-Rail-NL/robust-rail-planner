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

# --- ENHSP standalone planner (tools/enhsp) ---------------------------------
# Cloned + built from source only if not already present.
# Source: https://gitlab.com/enricos83/ENHSP-Public  (branch via ENHSP_REF)
# Build:  ./compile  (plain javac + jar; deps bundled in libs/)  -> enhsp-dist/enhsp.jar
TOOLS_DIR="$REPO_ROOT/tools/planners"
ENHSP_DIR="$TOOLS_DIR/enhsp"
ENHSP_REF="${ENHSP_REF:-enhsp-20}"
ENHSP_REPO="${ENHSP_REPO:-https://gitlab.com/enricos83/ENHSP-Public.git}"
ENHSP_JAR="$ENHSP_DIR/enhsp-dist/enhsp.jar"

if [[ -f "$ENHSP_JAR" ]]; then
    echo "ENHSP already present at $ENHSP_JAR — skipping download."
else
    echo "ENHSP not found in tools/ — fetching and building ($ENHSP_REF)..."
    if ! command -v git >/dev/null 2>&1; then
        echo "WARNING: git not found — cannot fetch ENHSP into tools/."
    elif ! command -v javac >/dev/null 2>&1; then
        echo "WARNING: javac (a JDK) not found — cannot build ENHSP."
        echo "         You appear to have only a JRE. Install a JDK (see note below)."
    else
        mkdir -p "$TOOLS_DIR"
        # Clean any partial/failed previous clone so the build starts fresh.
        [[ -d "$ENHSP_DIR" && ! -f "$ENHSP_JAR" ]] && rm -rf "$ENHSP_DIR"
        if git clone --depth 1 --branch "$ENHSP_REF" "$ENHSP_REPO" "$ENHSP_DIR"; then
            (
                cd "$ENHSP_DIR"
                chmod +x compile 2>/dev/null || true
                ./compile
            ) && echo "ENHSP built: $ENHSP_JAR" \
              || echo "WARNING: ENHSP build failed — see output above."
        else
            echo "WARNING: failed to clone ENHSP from $ENHSP_REPO ($ENHSP_REF)."
        fi
    fi
fi

if [[ -f "$ENHSP_JAR" ]]; then
    export ENHSP_HOME="$ENHSP_DIR"
    echo "ENHSP_HOME=$ENHSP_HOME"
    echo "Run directly with: java -jar \"$ENHSP_JAR\" -o <domain.pddl> -f <problem.pddl>"
fi
# ---------------------------------------------------------------------------
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