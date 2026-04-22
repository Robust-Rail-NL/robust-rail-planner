using PDDL, SymbolicPlanners


function run_planner()
    planner = AStarPlanner(HAdd())
    domain = load_domain(joinpath(dirname(dirname(dirname(@__FILE__))), "data", "example_domain.pddl"))

    problem_file = joinpath(dirname(dirname(dirname(@__FILE__))), "data", "scenario_solver_example1.pddl")
    problem = load_problem(problem_file)
    sol = planner(domain, problem)
    if sol.status == :success
        println("Solved $(domain) problem $(problem.name), plan length $(length(sol.plan))")
        open(replace(problem_file, ".pddl" => ".plan"), "w") do file
            write(file, join(sol.plan, '\n'))
        end
    end
end

run_planner()
