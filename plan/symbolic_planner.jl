using PDDL, SymbolicPlanners

include(joinpath(@__DIR__, "rail_progress_heuristic.jl"))

const TMP_DIR = joinpath(dirname(dirname(@__FILE__)), "tmp")

function parse_args()
    domain_file = length(ARGS) > 0 ? ARGS[1] : joinpath(TMP_DIR, "domain.pddl")
    problem_file = length(ARGS) > 1 ? ARGS[2] : joinpath(TMP_DIR, "problem.pddl")
    plan_file = length(ARGS) > 2 ? ARGS[3] : joinpath(TMP_DIR, "plan.plan")
    mode = length(ARGS) > 3 ? lowercase(ARGS[4]) : "symbolic"
    return domain_file, problem_file, plan_file, mode
end

function run_symbolic_planner(domain_file, problem_file, plan_file, mode)
    heuristic = if mode == "symbolic-rail"
        RailProgressHeuristic()
    elseif mode == "symbolic"
        HAdd()
    else
        error("Unknown symbolic planner mode: $(mode)")
    end
    println("Planner backend: SymbolicPlanners.jl WeightedAStarPlanner($(typeof(heuristic)))")
    println("Loading domain from: ", domain_file)
    domain = load_domain(domain_file)

    println("Loading problem from: ", problem_file)
    problem = load_problem(problem_file)

    println("Planning...")
    planner = WeightedAStarPlanner(heuristic, 4)
    started = time_ns()
    sol = planner(domain, problem)
    search_seconds = (time_ns() - started) / 1e9
    # Machine-readable lines for experiment scripts; flushed so a killed run
    # keeps what it already printed.
    println("TIMING stage=search seconds=", round(search_seconds; digits=3))
    flush(stdout)

    if sol.status == :success
        println("Solved problem $(problem.name), plan length $(length(sol.plan))")
        println("STATS expanded=$(sol.expanded) plan_length=$(length(sol.plan))")
        flush(stdout)
        mkpath(dirname(plan_file))
        open(plan_file, "w") do file
            for action in sol.plan
                text = string(action)
                println(file, occursin('(', text) ? text : text * "()")
            end
        end
        println("Plan written to: ", plan_file)
        return true
    else
        println("Failed to find a solution.")
        return false
    end
end

function main()
    domain_file, problem_file, plan_file, mode = parse_args()
    ok = run_symbolic_planner(domain_file, problem_file, plan_file, mode)
    ok || exit(1)
end

main()
