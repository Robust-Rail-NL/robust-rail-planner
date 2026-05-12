using PDDL, SymbolicPlanners

function run_planner()
    data_dir = joinpath(dirname(dirname(dirname(@__FILE__))), "data")

    # Allow passing domain and problem files as command line arguments, fallback to defaults
    domain_file = length(ARGS) > 0 ? ARGS[1] : joinpath(data_dir, "scenario_solver_example1_domain.pddl")
    problem_file = length(ARGS) > 1 ? ARGS[2] : joinpath(data_dir, "scenario_solver_example1.pddl")

    println("Loading domain from: ", domain_file)
    domain = load_domain(domain_file)

    println("Loading problem from: ", problem_file)
    problem = load_problem(problem_file)

    println("Planning...")
    planner = AStarPlanner(HAdd())
    sol = planner(domain, problem)

    if sol.status == :success
        println("Solved problem $(problem.name), plan length $(length(sol.plan))")
        out_file = replace(problem_file, ".pddl" => ".plan")
        open(out_file, "w") do file
            write(file, join(sol.plan, '\n'))
        end
        println("Plan written to: ", out_file)
    else
        println("Failed to find a solution.")
    end
end

run_planner()