using PDDL, SymbolicPlanners

const TMP_DIR = joinpath(dirname(dirname(@__FILE__)), "tmp")

function parse_args()
    domain_file = length(ARGS) > 0 ? ARGS[1] : joinpath(TMP_DIR, "domain.pddl")
    problem_file = length(ARGS) > 1 ? ARGS[2] : joinpath(TMP_DIR, "problem.pddl")
    plan_file = length(ARGS) > 2 ? ARGS[3] : joinpath(TMP_DIR, "plan.plan")
    return domain_file, problem_file, plan_file
end

function run_symbolic_planner(domain_file, problem_file, plan_file)
    println("Planner backend: SymbolicPlanners.jl WeightedAStarPlanner(HAdd())")
    println("Loading domain from: ", domain_file)
    domain = load_domain(domain_file)

    println("Loading problem from: ", problem_file)
    problem = load_problem(problem_file)

    println("Planning...")
    planner = WeightedAStarPlanner(HAdd(), 4)
    sol = planner(domain, problem)

    if sol.status == :success
        println("Solved problem $(problem.name), plan length $(length(sol.plan))")
        mkpath(dirname(plan_file))
        open(plan_file, "w") do file
            write(file, join(sol.plan, '\n'))
        end
        println("Plan written to: ", plan_file)
        return true
    else
        println("Failed to find a solution.")
        return false
    end
end

function main()
    domain_file, problem_file, plan_file = parse_args()
    ok = run_symbolic_planner(domain_file, problem_file, plan_file)
    ok || exit(1)
end

main()
