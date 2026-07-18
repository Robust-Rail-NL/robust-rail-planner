import sys
from unified_planning.shortcuts import *
from unified_planning.io import PDDLReader
from unified_planning.plans import SequentialPlan, ActionInstance


def parse_plan_file(problem, plan_file):
    """
    Parse a simple .plan file of the form:

    move(a,b)
    park(t1,r3)
    """

    action_map = {a.name: a for a in problem.actions}

    plan_actions = []

    with open(plan_file) as f:
        lines = [
            line.strip()
            for line in f.readlines()
            if line.strip() and not line.startswith(";")
        ]

    for line_no, line in enumerate(lines, start=1):

        if "(" not in line or not line.endswith(")"):
            raise ValueError(f"Invalid syntax at line {line_no}: {line}")

        if line.startswith("("):
            # PDDL-style: (action_name arg1 arg2)
            inner = line[1:-1]
            parts = inner.split(None, 1)
            action_name = parts[0]
            args = [a.strip() for a in parts[1].split()] if len(parts) > 1 else []
        else:
            # Python-style: action_name(arg1, arg2)
            action_name = line[:line.index("(")]
            args_string = line[line.index("(")+1:-1]
            args = [a.strip() for a in args_string.split(",")] if args_string.strip() else []

        if action_name not in action_map:
            raise ValueError(f"Unknown action '{action_name}' at line {line_no}")

        action = action_map[action_name]

        if len(args) != len(action.parameters):
            raise ValueError(
                f"Wrong parameter count for '{action_name}' "
                f"at line {line_no}: expected {len(action.parameters)}, got {len(args)}"
            )

        object_args = []

        for arg_name, param in zip(args, action.parameters):
            obj = problem.object(arg_name)
            object_args.append(obj)

        ai = ActionInstance(action, tuple(object_args))
        plan_actions.append(ai)

    return SequentialPlan(plan_actions)


def format_fluent_value(state, fluent):
    try:
        return state.get_value(fluent)
    except Exception:
        return "<?>"


def _ground_precondition(precondition, action, action_instance):
    return precondition.substitute({
        p: a
        for p, a in zip(action.parameters, action_instance.actual_parameters)
    })


def _precondition_status(state, grounded_precondition):
    try:
        value = state.get_value(grounded_precondition)
        return True, value
    except Exception as exc:
        return False, exc


def _trace_condition(state, expr, indent="  "):
    if expr.is_and():
        print(f"{indent}AND {expr}")
        for child_idx, child in enumerate(expr.args, start=1):
            child_ok, child_result = _trace_condition(state, child, indent + "  ")
            if not child_ok:
                return False, child_result
        return True, None

    ok, result = _precondition_status(state, expr)
    if ok:
        try:
            holds = bool(result.bool_constant_value())
            status = "OK" if holds else "FAIL"
            print(f"{indent}[{status}] {expr}")
            return holds, result if holds else expr
        except Exception as exc:
            print(f"{indent}[ERROR] {expr}")
            print(f"{indent}  Could not interpret value: {result}")
            print(f"{indent}  Details: {type(exc).__name__}: {exc}")
            return False, exc

    print(f"{indent}[ERROR] {expr}")
    print(f"{indent}  Details: {type(result).__name__}: {result}")
    return False, result



def validate_plan(domain_file, problem_file, plan_file):

    reader = PDDLReader()

    print(f"Loading domain:  {domain_file}")
    print(f"Loading problem: {problem_file}")

    problem = reader.parse_problem(domain_file, problem_file)

    print(f"Loading plan:    {plan_file}")

    plan = parse_plan_file(problem, plan_file)

    simulator = SequentialSimulator(problem)

    state = simulator.get_initial_state()

    print()
    print("=== VALIDATING PLAN ===")
    print()

    for idx, action_instance in enumerate(plan.actions, start=1):

        print(f"Step {idx}: {action_instance}")

        applicable = simulator.is_applicable(state, action_instance)

        if not applicable:

            print()
            print("VALIDATION FAILED")
            print(f"Failed at step {idx}")
            print(f"Action: {action_instance}")
            print()

            action = action_instance.action

            print("Precondition check:")

            for pre_idx, precondition in enumerate(action.preconditions, start=1):

                grounded = _ground_precondition(precondition, action, action_instance)
                print(f"  {pre_idx}.")
                ok, result = _trace_condition(state, grounded, indent="    ")

                if not ok:
                    print(f"    First failing condition: {result}")
                    break

            print()
            print("Tip: the first [FAIL] or [ERROR] line above is the blocking condition.")

            print()
            return False

        state = simulator.apply(state, action_instance)

    print()
    print("PLAN IS VALID")
    print()

    goal_satisfied = simulator.is_goal(state)
    print("Goal satisfied:", goal_satisfied)

    if not goal_satisfied:
        print("\nVALIDATION FAILED")
        print("The plan executed without precondition errors, but it does not reach the goal state.")
        return False

    return True


def main():

    if len(sys.argv) != 4:
        print("Usage:")
        print("python validate_plan.py domain.pddl problem.pddl plan.plan")
        sys.exit(1)

    domain_file = sys.argv[1]
    problem_file = sys.argv[2]
    plan_file = sys.argv[3]

    ok = validate_plan(domain_file, problem_file, plan_file)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
