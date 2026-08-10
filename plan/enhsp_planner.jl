const TMP_DIR = joinpath(dirname(dirname(@__FILE__)), "tmp")

function default_java()
    if haskey(ENV, "JAVA_HOME")
        exe = Sys.iswindows() ? "java.exe" : "java"
        candidate = joinpath(ENV["JAVA_HOME"], "bin", exe)
        isfile(candidate) && return candidate
    end
    # Known install locations for the JRE/JDK we ask users/containers to set up.
    for path in [raw"C:\Program Files\Microsoft\jdk-17.0.18.8-hotspot\bin\java.exe",
                 "/opt/homebrew/opt/openjdk@17/bin/java",
                 "/usr/local/opt/openjdk@17/bin/java",
                 "/usr/lib/jvm/java-17-openjdk-amd64/bin/java"]
        isfile(path) && return path
    end
    return "java"
end

function parse_args()
    domain_file = length(ARGS) > 0 ? ARGS[1] : joinpath(TMP_DIR, "domain.pddl")
    problem_file = length(ARGS) > 1 ? ARGS[2] : joinpath(TMP_DIR, "problem.pddl")
    plan_file = length(ARGS) > 2 ? ARGS[3] : joinpath(TMP_DIR, "plan.plan")
    return domain_file, problem_file, plan_file
end

function run_enhsp_planner(domain_file, problem_file, plan_file)
    println("Planner backend: ENHSP via Julia subprocess")
    java = get(ENV, "JAVA_EXE", default_java())
    enhsp_jar = get(ENV, "ENHSP_JAR", "/opt/enhsp/enhsp.jar")

    if !isfile(enhsp_jar)
        error("ENHSP jar not found: $(enhsp_jar). Set ENHSP_JAR to override.")
    end

    mkpath(dirname(plan_file))
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

function main()
    domain_file, problem_file, plan_file = parse_args()
    ok = run_enhsp_planner(domain_file, problem_file, plan_file)
    ok || exit(1)
end

main()
