using PDDL, SymbolicPlanners

function plan(domain_file::String, problem_file::String)
    planner = AStarPlanner(HAdd())
    domain = load_domain(domain_file)
    problem = load_problem(problem_file)
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

problem_file = ARGS[1]
domain_file = ARGS[2]
plan(domain_file, problem_file)