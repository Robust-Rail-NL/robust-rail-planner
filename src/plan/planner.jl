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
    # Standard cross-platform mechanism
    if haskey(ENV, "JAVA_HOME")
        exe = Sys.iswindows() ? "java.exe" : "java"
        candidate = joinpath(ENV["JAVA_HOME"], "bin", exe)
        isfile(candidate) && return candidate
    end
    # Known hardcoded paths for common setups
    for path in [raw"C:\Program Files\Microsoft\jdk-17.0.18.8-hotspot\bin\java.exe",
                 "/opt/homebrew/opt/openjdk@17/bin/java",
                 "/usr/local/opt/openjdk@17/bin/java"]
        isfile(path) && return path
    end
    return "java"
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
        return true
    else
        println("Failed to find a solution.")
        return false
    end
end

function run_enhsp_planner(domain_file, problem_file)
    println("Planner backend: ENHSP via Julia subprocess")
    plan_file = replace(problem_file, ".pddl" => ".plan")
    java = get(ENV, "JAVA_EXE", default_java())
    enhsp_jar = get(ENV, "ENHSP_JAR", default_enhsp_jar())

    if !isfile(enhsp_jar)
        error("ENHSP jar not found: $(enhsp_jar)")
    end

    command = `$(java) -jar $(enhsp_jar) -sp $(plan_file) -h hadd -s wa_star_4 -o $(domain_file) -f $(problem_file)`
    println("Running: ", command)
    run(command)

    if isfile(plan_file)
        steps = filter(line -> !isempty(strip(line)), readlines(plan_file))
        println("Plan written to: ", plan_file)
        println("Plan length: ", length(steps), " steps")
        return true
    else
        println("ENHSP finished without writing a plan file.")
        return false
    end
end

function run_planner()
    domain_file, problem_file, backend = parse_args()

    if backend == "symbolic"
        ok = run_symbolic_planner(domain_file, problem_file)
    elseif backend == "enhsp"
        ok = run_enhsp_planner(domain_file, problem_file)
    else
        error("Unknown planner backend '$(backend)'. Use 'symbolic' or 'enhsp'.")
    end

    ok || exit(1)
end

run_planner()
