# Robust-Rail Planning 
An AI Planning approach to solving the TUSPwSS.


- [Documentation on the Problem class in unified-planning (Python)](https://unified-planning.readthedocs.io/en/latest/api/model/Problem.html)
- [Documentation on the PDDL.jl representation of PDDL (Julia)](https://juliaplanners.github.io/PDDL.jl/stable/tutorials/getting_started/)
- [Documentation on the SymbolicPlanners.jl library (Julia)](https://juliaplanners.github.io/SymbolicPlanners.jl/dev/)

## Structure
- `src`
  - `convert.py`: contains any code for converting a scenario to a PDDL instancec and creating a PDDL domain
  - `cli.py`: where the arguments are defined
  - `evaluate.py`: calls the TORS evaluator. TODO: Create conversion from our plans to their plans
  - `pipeline.py`: runs the pipeline end to end for a number of instances
  - `plan`: contains any code for updating/running a planner (using SymbolicPlanners.jl)
- `data` contains any PDDL files, make sure to keep versions separate 

## Usage
Run commands from the `planning-approach` folder in the main repo, not from inside the dev container.

```
cd planning-approach
```
1. Build the project using a setup script:

Linux/macOS:
```
bash setup.sh
```
Windows PowerShell:
```
.\setup.ps1
```
Windows Command Prompt:``
```
setup.bat
```

2. Activate the Conda environment
```
conda activate robust-rail-planning
```

1. After activating the Conda environment, run:
```
robust-rail-plan
```

Examples

```robust-rail-plan --generate```

```robust-rail-plan --examples```

```robust-rail-plan --simple```

<!-- ```robust-rail-plan --subproblem parking``` -->

<!-- ```robust-rail-plan --subproblem matching``` -->

<!-- ```robust-rail-plan --subproblem combined``` -->

```robust-rail-plan --log-level DEBUG```

## Converter variants

| Converter | Main difference |
| --- | --- |
| `baseline_no_parameters` | Baseline model with planner-controlled matching and continuous metre-based train positions. |
| `corridor_no_switch_unlimited_order_servicing_discrete` | Replaces continuous positions with occupied track length and discrete A/B-side blocking, while retaining planner-controlled matching. |
| `corridor_no_switch_unlimited_order_servicing_discrete_ordered_matching` | Uses the discrete model and precomputes an order-preserving matching before planning. |
| `corridor_no_switch_unlimited_order_servicing_discrete_compiled_matching` | Uses composition-preserving pre-matching and lower-arity request actions. Complete incoming compositions are reused where possible; other compositions can be uncoupled, moved, and assembled from the front or back. It produces a scenario-specific domain for the selected request sequence. |

### Compiled-matching results

ENHSP runs using the compiled-matching converter produced the following search
results:

| Scenario | Planning time | Plan length | Expanded nodes |
| --- | ---: | ---: | ---: |
| `scenario_solver_example1.json` | 3.429 s | 41 | 617 |
| `scenario_solver_example2.json` | 0.236 s | 24 | 29 |
| `scenario_solver_example3.json` | 0.239 s | 21 | 24 |
| `scenario_solver_random1.json` | 1.934 s | 93 | 169 |

The plans can also be checked with the AI-generated `validate_plan.py` and
`audit_discrete_plan.py` scripts. These validation scripts have not yet been
thoroughly independently verified. The two random-distribution scenarios are
not included in these results.

## Previous work
Thesis:

> N.B. Lonyuk. (2024). *Using PDDL models to solve TUSS: How to model TUSS as an Automated Planning problem and solve it*. MSc Thesis. Delft University of Technology. https://resolver.tudelft.nl/uuid:242e8fdd-95d2-4915-bd28-f0697559514e


- [Repository with PDDL models and generator](https://github.com/LonyuNaz/tusp-pddl-models)
  - [Generator repository](https://github.com/LonyuNaz/tusp-pddl-generator)
- [Repository with Constraint Programming post-processing](https://github.com/LonyuNaz/tusp-pddl-post-processing)
- [Repository with experiment setup for thesis](https://github.com/LonyuNaz/tusp-pddl-experiments)
  - [Older version](https://github.com/LonyuNaz/tusp-cp-postprocessing)
- [Comparison of numeric and temporal models](https://github.com/LonyuNaz/tusp-numeric-temporal-pddl)
