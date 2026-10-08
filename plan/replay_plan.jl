using PDDL, SymbolicPlanners

function usage()
    println("Usage: julia --project=plan plan/replay_plan.jl DOMAIN PROBLEM PLAN [--no-pause] [--all-actions]")
end

function parse_plan_action(line)
    text = strip(line)
    startswith(text, "(") && return parse_pddl(text)

    action = match(r"^([^\s(]+)\((.*)\)$", text)
    isnothing(action) && error("Unsupported plan line: $line")
    name, arguments = action.captures
    arguments = strip(arguments)
    normalized = isempty(arguments) ? "($name)" :
        "($name $(join(strip.(split(arguments, ',')), " ")))"
    return parse_pddl(normalized)
end

function numeric_values(state)
    result = Dict{String,Any}()
    for (name, values) in state.values
        if values isa AbstractDict
            for (arguments, value) in values
                label = "$(name)($(join(string.(arguments), ", ")))"
                result[label] = value
            end
        else
            result[string(name)] = values
        end
    end
    return result
end

function print_numeric_changes(before, after)
    before_values = numeric_values(before)
    after_values = numeric_values(after)
    names = sort!(collect(union(keys(before_values), keys(after_values))))

    for name in names
        old_value = get(before_values, name, nothing)
        new_value = get(after_values, name, nothing)
        old_value == new_value && continue
        println("  ~ ", name, ": ", old_value, " -> ", new_value)
    end
end

function action_parts(action)
    parsed = match(r"^([^\s(]+)\((.*)\)$", string(action))
    isnothing(parsed) && return "", String[]
    name, arguments = parsed.captures
    args = isempty(strip(arguments)) ? String[] : strip.(split(arguments, ','))
    return name, args
end

function movement_options(domain, state, su)
    return sort!([
        (args[3], string(action))
        for action in available(domain, state)
        for (name, args) in (action_parts(action),)
        if startswith(name, "move_") && length(args) >= 3 && args[1] == su
    ], by=first)
end

function print_post_coupling_tracks(domain, state, action)
    name, args = action_parts(action)
    request_su = if name in ("compiled_couple_front", "compiled_couple_back", "compiled_start_request")
        length(args) >= 3 ? args[3] : nothing
    elseif name == "compiled_adopt_composition"
        length(args) >= 2 ? args[2] : nothing
    else
        nothing
    end
    isnothing(request_su) && return

    probe = state
    options = movement_options(domain, probe, request_su)
    prep = String[]
    if isempty(options)
        choices = collect(available(domain, probe))
        complete = findfirst(choices) do candidate
            candidate_name, candidate_args = action_parts(candidate)
            candidate_name == "complete_request_composition" &&
                !isempty(candidate_args) && candidate_args[1] == request_su
        end
        if !isnothing(complete)
            candidate = choices[complete]
            probe = execute(domain, probe, candidate)
            push!(prep, string(candidate))
        end

        choices = collect(available(domain, probe))
        start = findfirst(choices) do candidate
            candidate_name, candidate_args = action_parts(candidate)
            candidate_name == "start_move_su" &&
                !isempty(candidate_args) && candidate_args[1] == request_su
        end
        if !isnothing(start)
            candidate = choices[start]
            probe = execute(domain, probe, candidate)
            push!(prep, string(candidate))
        end
        options = movement_options(domain, probe, request_su)
    end

    println("Tracks available after coupling for ", request_su, ":")
    !isempty(prep) && println("  after ", join(prep, " -> "))
    if isempty(options)
        println("  none in the current assembly state")
    else
        for (track, movement) in unique(options)
            println("  ", track, " via ", movement)
        end
    end
end

function replay(domain_file, problem_file, plan_file; pause=true, show_all=false)
    domain = load_domain(domain_file)
    problem = load_problem(problem_file)
    actions = [
        parse_plan_action(line)
        for line in readlines(plan_file)
        if !isempty(strip(line))
    ]
    state = initstate(domain, problem)

    println("Plan length: ", length(actions))
    println("Permitted coupling tracks:")
    coupling_facts = sort([
        string(fact)
        for fact in state.facts
        if occursin("compiled_coupling_track", string(fact))
    ])
    foreach(fact -> println("  ", fact), coupling_facts)

    for (step, action) in enumerate(actions)
        choices = collect(available(domain, state))
        applicable = action in choices

        println("\n", repeat("=", 72))
        println("Step ", step, ": ", action)
        println("Applicable actions: ", length(choices))
        println("Selected action is applicable: ", applicable)

        if show_all
            println("Available actions:")
            foreach(choice -> println("  ", choice), sort!(choices, by=string))
        else
            coupling_choices = sort!([
                choice
                for choice in choices
                if occursin("couple", string(choice)) ||
                   occursin("compiled_start_request", string(choice))
            ], by=string)
            if !isempty(coupling_choices)
                println("Available coupling actions:")
                foreach(choice -> println("  ", choice), coupling_choices)
            end
        end

        applicable || error("Plan action is not applicable at step $step")
        next_state = execute(domain, state, action)

        added = sort!(string.(collect(setdiff(next_state.facts, state.facts))))
        removed = sort!(string.(collect(setdiff(state.facts, next_state.facts))))

        println("Boolean changes:")
        foreach(fact -> println("  + ", fact), added)
        foreach(fact -> println("  - ", fact), removed)
        println("Numeric changes:")
        print_numeric_changes(state, next_state)
        print_post_coupling_tracks(domain, next_state, action)

        state = next_state
        if pause && step < length(actions)
            print("Press Enter for the next action...")
            readline()
        end
    end

    println("\nGoal satisfied: ", satisfy(domain, state, problem.goal))
end

if length(ARGS) < 3
    usage()
    exit(1)
end

replay(
    ARGS[1],
    ARGS[2],
    ARGS[3];
    pause=!("--no-pause" in ARGS[4:end]),
    show_all="--all-actions" in ARGS[4:end],
)
