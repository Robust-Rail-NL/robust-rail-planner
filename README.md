# Robust-Rail Planning 
An AI Planning approach to solving the TUSPwSS.


- [Documentation on the Problem class in unified-planning (Python)](https://unified-planning.readthedocs.io/en/latest/api/model/Problem.html)
- [Documentation on the PDDL.jl representation of PDDL (Julia)](https://juliaplanners.github.io/PDDL.jl/stable/tutorials/getting_started/)
- [Documentation on the SymbolicPlanners.jl library (Julia)](https://juliaplanners.github.io/SymbolicPlanners.jl/dev/)

## Structure
- `src`
  - `convert`: contains any code for converting scenarios from `scenario-planning-inputs` to PDDL 
  - `plan`: contains any code for updating/running a planner (using SymbolicPlanners.jl)
- `experiments` contains any code for running experiments and comparing to `robust-rail-solver`
- `data` contains any PDDL files, make sure to keep versions separate 

## Previous work
Thesis:

> N.B. Lonyuk. (2024). *Using PDDL models to solve TUSS: How to model TUSS as an Automated Planning problem and solve it*. MSc Thesis. Delft University of Technology. https://resolver.tudelft.nl/uuid:242e8fdd-95d2-4915-bd28-f0697559514e


- [Repository with PDDL models and generator](https://github.com/LonyuNaz/tusp-pddl-models)
  - [Generator repository](https://github.com/LonyuNaz/tusp-pddl-generator)
- [Repository with Constraint Programming post-processing](https://github.com/LonyuNaz/tusp-pddl-post-processing)
- [Repository with experiment setup for thesis](https://github.com/LonyuNaz/tusp-pddl-experiments)
  - [Older version](https://github.com/LonyuNaz/tusp-cp-postprocessing)
- [Comparison of numeric and temporal models](https://github.com/LonyuNaz/tusp-numeric-temporal-pddl)
