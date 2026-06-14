using PDDL, SymbolicPlanners

function plan(domain_file::String, problem_file::String, planner_backend::String, plan_file::String)
    domain = load_domain(domain_file)
    problem = load_problem(problem_file)

    println("Planner backend: SymbolicPlanners.jl AStarPlanner(HAdd())")
    println("Loading domain from: ", domain_file)
    println("Loading problem from: ", problem_file)
    println("Planning...")

    planner = AStarPlanner(HAdd())
    sol = planner(domain, problem)

    if sol.status == :success
        println("Solved problem $(problem.name), plan length $(length(sol.plan))")

        mkpath(dirname(plan_file))

        open(plan_file, "w") do file
            write(file, join(sol.plan, '\n'))
        end

        println("Plan written to: ", plan_file)
        exit(0)
    else
        println("Failed to solve $(problem.name): $(sol.status)")
        exit(2)
    end
end

function run_planner()
    if length(ARGS) < 2
        error("Usage: julia planner.jl <domain_file> <problem_file> [planner_backend] [plan_file]")
    end

    domain_file = ARGS[1]
    problem_file = ARGS[2]
    planner_backend = length(ARGS) >= 3 ? ARGS[3] : "symbolic"
    plan_file = length(ARGS) >= 4 ? ARGS[4] : replace(problem_file, ".pddl" => ".plan")

    plan(domain_file, problem_file, planner_backend, plan_file)
end

run_planner()