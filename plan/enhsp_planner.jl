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

# Machine-readable output for experiment scripts; flushed so a killed run keeps
# the lines it already printed.
function emit_timing(stage, seconds)
    println("TIMING stage=", stage, " seconds=", round(seconds; digits=3))
    flush(stdout)
end

# ENHSP prints e.g. "Expanded Nodes:10" and "Planning Time (msec): 33".
function enhsp_stat(line, label)
    startswith(line, label * ":") || return nothing
    return tryparse(Int, strip(line[length(label)+2:end]))
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
    stats = Dict{String,Int}()
    started = time_ns()
    # Echo ENHSP's stdout line by line (so planner.out is unchanged) while
    # picking its statistics out of it. Throws on a non-zero exit, like run().
    open(command, "r") do io
        for line in eachline(io)
            println(line)
            flush(stdout)
            for label in ("Expanded Nodes", "Plan-Length", "Planning Time (msec)")
                value = enhsp_stat(line, label)
                value === nothing || (stats[label] = value)
            end
        end
    end
    process_seconds = (time_ns() - started) / 1e9

    # search = ENHSP's own planning time (heuristic setup + search, excluding JVM
    # start-up and PDDL parsing); falls back to the whole process if not printed.
    planning_ms = get(stats, "Planning Time (msec)", nothing)
    emit_timing("search", planning_ms === nothing ? process_seconds : planning_ms / 1000)
    emit_timing("enhsp_process", process_seconds)
    if haskey(stats, "Expanded Nodes") && haskey(stats, "Plan-Length")
        println("STATS expanded=", stats["Expanded Nodes"], " plan_length=", stats["Plan-Length"])
        flush(stdout)
    end

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
