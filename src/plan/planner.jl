using PDDL, SymbolicPlanners

function workspace_root()
    return dirname(dirname(dirname(dirname(dirname(@__FILE__)))))
end

function default_enhsp_jar()
    return joinpath(
        workspace_root(),
        "public",
        "tusp-pddl-experiments-setups",
        "ENHSP-Public",
        "enhsp-dist",
        "enhsp.jar",
    )
end

function default_java()
    microsoft_java = raw"C:\Program Files\Microsoft\jdk-17.0.18.8-hotspot\bin\java.exe"
    return isfile(microsoft_java) ? microsoft_java : "java"
end

function parse_args()
    data_dir = joinpath(dirname(dirname(dirname(@__FILE__))), "data")
    domain_file = length(ARGS) > 0 ? ARGS[1] : joinpath(data_dir, "domain.pddl")
    problem_file = length(ARGS) > 1 ? ARGS[2] : joinpath(data_dir, "scenario_solver_example1.pddl")
    backend = length(ARGS) > 2 ? lowercase(ARGS[3]) : "symbolic"
    return domain_file, problem_file, backend
end

function run_symbolic_planner(domain_file, problem_file)
    println("Planner backend: SymbolicPlanners.jl AStarPlanner(HAdd())")
    println("Loading domain from: ", domain_file)
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
        println("Plan written to: ", out_file)
    else
        println("Failed to find a solution.")
    end
end

run_planner()
