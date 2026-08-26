# Robust-Rail Planning
An AI Planning approach to solving the TUSPwSS.

- [Documentation on the Problem class in unified-planning (Python)](https://unified-planning.readthedocs.io/en/latest/api/model/Problem.html)
- [Documentation on the PDDL.jl representation of PDDL (Julia)](https://juliaplanners.github.io/PDDL.jl/stable/tutorials/getting_started/)
- [Documentation on the SymbolicPlanners.jl library (Julia)](https://juliaplanners.github.io/SymbolicPlanners.jl/dev/)
- [ENHSP-20, the numeric heuristic planner used as an alternative backend](https://sites.google.com/view/enhsp/)

## Structure
- `main.py`: container entrypoint. Chains scenario → PDDL → plan → TORS JSON in one call; this is what the Docker image runs.
- `convert_to_pddl/`: converts a solver-format scenario/location pair into a PDDL domain + problem. One subfolder per model variant, each with its own `convert.py`:
  `md_files/` holds per-model design notes (currently `baseline_no_parameters.md`).
- `plan/`: Julia planner backends, run as subprocesses by `main.py`.
  - `symbolic_planner.jl`: SymbolicPlanners.jl `WeightedAStarPlanner(HAdd())`.
  - `enhsp_planner.jl`: shells out to the ENHSP jar (`ENHSP_JAR` env var, defaults to `/opt/enhsp/enhsp.jar`).
  - `Project.toml` / `Manifest.toml`: pinned Julia dependencies (`PDDL`, `SymbolicPlanners`).
- `convert_plan_to_tors/convert_to_tors.py`: converts a raw `.plan` (PDDL planner output) back into TORS JSON, given the original scenario and location files.
- `data/`: sample location and example scenarios. **Pre-unification and currently unusable** — string ids, `in` as an object, no `trainUnitTypes` — so the converters reject them. Use `tests/fixtures/simple_service/` (migrated and schema-validated) or the sibling `robust-rail-general` repo instead, until these are migrated or dropped.
- `Dockerfile`: builds the distributable image (`ENTRYPOINT ["python3", "main.py"]`) — this is what other tools in the pipeline run.
- `.devcontainer/`: VS Code dev container config, see [Dev container](#dev-container) below.
- `requirements.txt`: Python dependencies (`unified-planning`).

## Usage

### External usage (the common case)

This tool is meant to be run as a Docker container, taking a scenario in and
producing a TORS plan out.

1. Build the image from this folder:
   ```
   docker build -t planner:latest .
   ```
2. Run it directly, mounting a location directory (containing `location.json`
   and a `scenarios/` folder) to `/app/database`:
   ```
   docker run --rm \
     --mount type=bind,source=/path/to/Location_X,target=/app/database \
     planner:latest \
     --location /app/database/location.json \
     --scenario /app/database/scenarios/scenario_example1.json \
     --planner symbolic \
     --output /app/database/plans/plan_example1.json
   ```

| Flag | Description |
| --- | --- |
| `--location` | Path to `location.json` inside the container (required) |
| `--scenario` | Path to a `scenario_*.json` file inside the container (required) |
| `--planner {symbolic,enhsp}` | Planner backend to use (default: `symbolic`) |
| `--output` | Path to write the resulting TORS plan JSON (required) |

In practice this image is driven by the sibling `robust-rail-general`
repo, which mirrors the existing generator/solver/evaluator steps:
```
cd ../robust-rail-general
python run_planner.py                       # every location, every scenario
python run_planner.py --location Location_KleineBinckhorst --planner enhsp
python run_planner.py --dry-run              # print the docker commands only
```
It writes `plan_<name>.json` into each location's `plans/` folder — the same
convention `run_evaluator.py` already reads from.

### Running natively, without Docker

Requires Julia (with the `PDDL` and `SymbolicPlanners` packages from
`plan/Project.toml`), a JDK 17 + the ENHSP jar on `ENHSP_JAR` if you want the
`enhsp` planner, and the Python packages in `requirements.txt`. Then:
```
python main.py \
  --location tests/fixtures/simple_service/location.json \
  --scenario tests/fixtures/simple_service/scenarios/scenario_simple.json \
  --planner symbolic --output /tmp/plan.json
```

### Running a single stage (for debugging)

Each stage can be run standalone, which is useful when only one step is
misbehaving, or to try a converter variant `main.py` doesn't wire up yet:

```
FIX=tests/fixtures/simple_service

# 1. scenario -> PDDL
python convert_to_pddl/baseline_no_parameters/convert.py \
  -l $FIX/location.json -s $FIX/scenarios/scenario_simple.json \
  -d /tmp/domain.pddl -o /tmp/problem.pddl

# 2. PDDL -> plan
julia --project=plan plan/symbolic_planner.jl /tmp/domain.pddl /tmp/problem.pddl /tmp/plan.plan
# or: julia --project=plan plan/enhsp_planner.jl /tmp/domain.pddl /tmp/problem.pddl /tmp/plan.plan

# 3. plan -> TORS JSON
python convert_plan_to_tors/convert_to_tors.py \
  --plan /tmp/plan.plan --scenario $FIX/scenarios/scenario_simple.json \
  --location $FIX/location.json --output /tmp/plan.json
```

Discrete/corridor variants add `--precompute-matching` and/or
`--matching-variant N` flags — pass `-h` to any `convert.py` to see what it
accepts.

## Dev container

Open this folder in VS Code (or Cursor) and **Reopen in Container**. A few
things worth knowing:

- The dev container builds `.devcontainer/Dockerfile`, *not* the top-level
  `Dockerfile`. It provisions the same toolchain (Julia, JDK 17, ENHSP,
  Python) but doesn't bake in the app code — VS Code live-mounts your working
  copy instead, so edits show up without a rebuild.
- The Julia package depot is cached in a named volume
  (`planning-approach-refactor-julia-depot`), so Julia packages aren't
  re-downloaded on every container rebuild.
- `postCreateCommand` installs the `PDDL` and `SymbolicPlanners` Julia
  packages after the container starts (this is separate from the Python
  `pip install`, which happens at image build time).
- The `julialang.language-julia` and `ms-python.python` extensions, plus the
  Claude Code feature, are installed automatically.
- Because the full toolchain lives inside the container, run everything —
  `main.py`, individual converters — from an integrated
  terminal inside the dev container rather than trying to replicate Julia/JDK/
  ENHSP on the host.
- When you need the actual artifact external tools consume (e.g. what
  `run_planner.py` runs as `planner:latest`), build the top-level
  `Dockerfile` instead — that's a separate, standalone image, not the dev
  container.

## Previous work
Thesis:

> N.B. Lonyuk. (2024). *Using PDDL models to solve TUSS: How to model TUSS as an Automated Planning problem and solve it*. MSc Thesis. Delft University of Technology. https://resolver.tudelft.nl/uuid:242e8fdd-95d2-4915-bd28-f0697559514e

- [Repository with PDDL models and generator](https://github.com/LonyuNaz/tusp-pddl-models)
  - [Generator repository](https://github.com/LonyuNaz/tusp-pddl-generator)
- [Repository with Constraint Programming post-processing](https://github.com/LonyuNaz/tusp-pddl-post-processing)
- [Repository with experiment setup for thesis](https://github.com/LonyuNaz/tusp-pddl-experiments)
  - [Older version](https://github.com/LonyuNaz/tusp-cp-postprocessing)
- [Comparison of numeric and temporal models](https://github.com/LonyuNaz/tusp-numeric-temporal-pddl)
