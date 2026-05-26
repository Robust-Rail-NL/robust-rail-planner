using PDDL, SymbolicPlanners

function plan(domain_file::String, problem_file::String)
    domain = load_domain(domain_file)
    problem = load_problem(problem_file)

    println("Planning...")
    planner = AStarPlanner(HAdd())
    sol = planner(domain, problem)

    if sol.status == :success
        println("Solved $(domain.name) problem $(problem.name), plan length $(length(sol.plan))")
        open(replace(problem_file, ".pddl" => ".plan"), "w") do file
            write(file, join(sol.plan, '\n'))
        end
    else
        println("Failed to solve $(problem.name): $(sol.status)")
    end
end

function run_planner()
    data_dir = joinpath(dirname(dirname(dirname(@__FILE__))), "data")

    domain_file  = length(ARGS) > 0 ? ARGS[1] : joinpath(data_dir, "domain.pddl")
    problem_file = length(ARGS) > 1 ? ARGS[2] : joinpath(data_dir, "scenario_solver_example1.pddl")

    println("Loading domain from: ", domain_file)
    println("Loading problem from: ", problem_file)

    plan(domain_file, problem_file)
end

run_planner()