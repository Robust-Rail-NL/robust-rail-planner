using PDDL
using SymbolicPlanners

import SymbolicPlanners: Heuristic, compute, is_precomputed, precompute!

# Build a PDDL fact with constant arguments.
fact(name::Symbol, args::Const...) = Compound(name, Term[args...])

# Check whether a boolean fact is true in the current state.
has_fact(state, name::Symbol, args::Const...) = fact(name, args...) in state.facts

# Return all facts with the requested predicate name.
function facts_named(state, name::Symbol)
    return (
        term for term in state.facts
        if term isa Compound && term.name == name
    )
end

# Read the first argument from each matching fact without duplicates.
function first_arguments(state, name::Symbol)
    return unique(Const(term.args[1].name) for term in facts_named(state, name))
end

# Read one numeric fluent value, using zero when it is absent.
function numeric_fluent(state, name::Symbol, object::Const)
    values = get(state.values, name, nothing)
    isnothing(values) && return 0
    return get(values, (object.name,), 0)
end

# Cache scenario facts that remain fixed throughout the search.
struct RailProgressContext
    arrival_sus::Vector{Const}
    service_sus::Vector{Const}
    parking_slots::Vector{Const}
    request_sus::Vector{Tuple{Const,Const}}
    request_departure_goals::Vector{Const}
    su_departure_goals::Vector{Const}
    arrival_tracks::Dict{Const,Const}
    unit_targets::Dict{Const,Const}
    coupling_tracks::Dict{Const,Vector{Const}}
    service_tracks::Dict{Const,Vector{Const}}
    required_facilities::Dict{Const,Vector{Const}}
    parking_targets::Dict{Const,Vector{Tuple{Const,Const}}}
    exit_tracks::Vector{Const}
    route_distances::Dict{Tuple{Const,Const},Int}
end

# Extract single-object goals for a given predicate.
function goal_arguments(spec, name::Symbol)
    objects = Const[]
    for term in SymbolicPlanners.get_goal_terms(spec)
        if term isa Compound && term.name == name && length(term.args) == 1
            push!(objects, Const(term.args[1].name))
        end
    end
    return unique(objects)
end

# Precompute shortest directed routes through the compiled track corridor.
function route_distances(state)
    adjacency = Dict{Const,Vector{Const}}()
    for term in facts_named(state, :compiled_route_edge)
        from = Const(term.args[1].name)
        to = Const(term.args[2].name)
        push!(get!(adjacency, from, Const[]), to)
    end

    distances = Dict{Tuple{Const,Const},Int}()
    for start in keys(adjacency)
        queue = Const[start]
        seen = Dict(start => 0)
        index = 1
        while index <= length(queue)
            current = queue[index]
            index += 1
            for neighbor in get(adjacency, current, Const[])
                haskey(seen, neighbor) && continue
                seen[neighbor] = seen[current] + 1
                push!(queue, neighbor)
            end
        end
        for (target, distance) in seen
            distances[(start, target)] = distance
        end
    end
    return distances
end

# Collect service, parking, coupling and departure targets from the initial state.
function RailProgressContext(state, spec)
    request_sus = Tuple{Const,Const}[]
    for term in facts_named(state, :request_su_for_request)
        push!(request_sus, (
            Const(term.args[1].name),
            Const(term.args[2].name),
        ))
    end

    arrival_tracks = Dict{Const,Const}()
    for term in facts_named(state, :su_arrival_track)
        arrival_tracks[Const(term.args[1].name)] = Const(term.args[2].name)
    end

    unit_targets = Dict{Const,Const}()
    for term in facts_named(state, :compiled_target_request_su)
        unit_targets[Const(term.args[1].name)] = Const(term.args[2].name)
    end

    coupling_tracks = Dict{Const,Vector{Const}}()
    for term in facts_named(state, :compiled_coupling_track)
        request_su = Const(term.args[1].name)
        push!(
            get!(coupling_tracks, request_su, Const[]),
            Const(term.args[2].name),
        )
    end

    facility_tracks = Dict{Const,Vector{Const}}()
    for term in facts_named(state, :facility_type)
        track = Const(term.args[1].name)
        facility = Const(term.args[2].name)
        push!(get!(facility_tracks, facility, Const[]), track)
    end

    required_facilities = Dict{Const,Vector{Const}}()
    for term in facts_named(state, :requires_facility)
        su = Const(term.args[1].name)
        facility = Const(term.args[2].name)
        push!(get!(required_facilities, su, Const[]), facility)
    end

    service_tracks = Dict{Const,Vector{Const}}()
    for (su, facilities) in required_facilities
        tracks = Const[]
        for facility in facilities
            append!(tracks, get(facility_tracks, facility, Const[]))
        end
        service_tracks[su] = unique(tracks)
    end

    slot_tracks = Dict{Const,Vector{Const}}()
    for term in facts_named(state, :parking_slot_track)
        slot = Const(term.args[1].name)
        push!(get!(slot_tracks, slot, Const[]), Const(term.args[2].name))
    end
    parking_targets = Dict{Const,Vector{Tuple{Const,Const}}}()
    for term in facts_named(state, :parking_compatible)
        unit = Const(term.args[1].name)
        slot = Const(term.args[2].name)
        for track in get(slot_tracks, slot, Const[])
            push!(get!(parking_targets, unit, Tuple{Const,Const}[]), (slot, track))
        end
    end

    exit_tracks = unique(vcat(
        collect(first_arguments(state, :departure_exit_a)),
        collect(first_arguments(state, :departure_exit_b)),
    ))

    return RailProgressContext(
        collect(keys(arrival_tracks)),
        collect(keys(required_facilities)),
        collect(PDDL.get_objects(state, :parkingslot)),
        request_sus,
        goal_arguments(spec, :request_departed),
        goal_arguments(spec, :departed_su),
        arrival_tracks,
        unit_targets,
        coupling_tracks,
        service_tracks,
        required_facilities,
        parking_targets,
        exit_tracks,
        route_distances(state),
    )
end

# Map each train unit to the active shunting unit that contains it.
function active_sources(state)
    active = Set(first_arguments(state, :active_su))
    sources = Dict{Const,Vector{Const}}()
    for term in facts_named(state, :contains_su)
        su = Const(term.args[1].name)
        su in active || continue
        unit = Const(term.args[2].name)
        push!(get!(sources, unit, Const[]), su)
    end
    return sources
end

# Record the current track of each shunting unit in the yard.
function su_tracks(context, state)
    tracks = Dict{Const,Const}()
    for term in facts_named(state, :at_su)
        su = Const(term.args[1].name)
        track = Const(term.args[2].name)
        track == Const(:phantom) || (tracks[su] = track)
    end
    for (su, track) in context.arrival_tracks
        haskey(tracks, su) || (tracks[su] = track)
    end
    return tracks
end

# Find the shortest known route from one track to any target track.
function distance_to_any(context, from, targets)
    isempty(targets) && return 0
    distances = (
        get(context.route_distances, (from, target), typemax(Int))
        for target in targets
    )
    best = minimum(distances)
    return best == typemax(Int) ? 10 : best
end

# Estimate the remaining travel needed for unfinished physical operations.
function physical_distance(context, state)
    sources = active_sources(state)
    tracks = su_tracks(context, state)
    total = 0

    # Route unserviced units towards a suitable service track.
    for su in context.service_sus
        has_fact(state, :serviced, su) && continue
        haskey(tracks, su) || continue
        total += distance_to_any(
            context,
            tracks[su],
            get(context.service_tracks, su, Const[]),
        )
    end

    # Route unmatched request material towards its coupling track.
    for (unit, request_su) in context.unit_targets
        has_fact(state, :contains_su, request_su, unit) && continue
        candidates = get(sources, unit, Const[])
        isempty(candidates) && continue
        distances = Int[]
        for source_su in candidates
            haskey(tracks, source_su) || continue
            push!(distances, distance_to_any(
                context,
                tracks[source_su],
                get(context.coupling_tracks, request_su, Const[]),
            ))
        end
        isempty(distances) || (total += minimum(distances))
    end

    # Route units needed by open parking slots towards those tracks.
    for (unit, targets) in context.parking_targets
        pending = filter(target -> !has_fact(state, :parking_slot_fulfilled, target[1]), targets)
        isempty(pending) && continue
        candidates = get(sources, unit, Const[])
        isempty(candidates) && continue
        distances = Int[]
        for source_su in candidates
            haskey(tracks, source_su) || continue
            push!(distances, distance_to_any(
                context,
                tracks[source_su],
                Const[target[2] for target in pending],
            ))
        end
        isempty(distances) || (total += minimum(distances))
    end

    # Route completed departure compositions towards an exit.
    for su in first_arguments(state, :must_depart_su)
        haskey(tracks, su) || continue
        total += distance_to_any(context, tracks[su], context.exit_tracks)
    end
    return total
end

# Lifecycle progress is weighted above route distance to keep goals dominant.
Base.@kwdef struct RailProgressWeights
    unsatisfied_goals::Float32 = 100.0f0
    undeparted::Float32 = 20.0f0
    unassembled::Float32 = 12.0f0
    missing_units::Float32 = 4.0f0
    unserviced::Float32 = 6.0f0
    unparked::Float32 = 4.0f0
    unarrived::Float32 = 2.0f0
    distance::Float32 = 0.75f0
end

mutable struct RailProgressHeuristic <: Heuristic
    context::Union{Nothing,RailProgressContext}
    weights::RailProgressWeights
end

# Create the heuristic with an empty context that is filled before search.
RailProgressHeuristic(weights=RailProgressWeights()) = RailProgressHeuristic(
    nothing,
    weights,
)

# Report whether the static scenario context has been prepared.
is_precomputed(h::RailProgressHeuristic) = !isnothing(h.context)

# Build the static context once before evaluating search states.
function precompute!(
    h::RailProgressHeuristic,
    domain::PDDL.Domain,
    state::PDDL.State,
    spec::SymbolicPlanners.Specification,
)
    h.context = RailProgressContext(state, spec)
    return h
end

# Score one search state using unfinished work and route distance.
function compute(
    h::RailProgressHeuristic,
    domain::PDDL.Domain,
    state::PDDL.State,
    spec::SymbolicPlanners.Specification,
)
    context = something(h.context)

    # Count unfinished work in the current state before applying the weights.
    unsatisfied_goals = count(
        goal -> !PDDL.satisfy(domain, state, goal),
        SymbolicPlanners.get_goal_terms(spec),
    )
    # Count unfinished arrival, service, parking and assembly work.
    unarrived = count(
        su -> !has_fact(state, :su_has_arrived, su),
        context.arrival_sus,
    )
    unserviced = count(
        su -> !has_fact(state, :serviced, su),
        context.service_sus,
    )
    unparked = count(
        slot -> !has_fact(state, :parking_slot_fulfilled, slot),
        context.parking_slots,
    )
    unassembled = count(
        pair -> !has_fact(state, :request_assembled, pair[2]),
        context.request_sus,
    )
    # Measure how many train units are still missing from request compositions.
    missing_units = sum((
        max(
            0,
            numeric_fluent(state, :request_size, request) -
            numeric_fluent(state, :su_unit_count, request_su),
        )
        for (request_su, request) in context.request_sus
    ); init=0)
    # Count departure goals that have not yet been reached.
    undeparted = count(
        request -> !has_fact(state, :request_departed, request),
        context.request_departure_goals,
    ) + count(
        su -> !has_fact(state, :departed_su, su),
        context.su_departure_goals,
    )

    # Combine lifecycle progress with the remaining physical route distance.
    weights = h.weights
    score = weights.unsatisfied_goals * unsatisfied_goals +
            weights.undeparted * undeparted +
            weights.unassembled * unassembled +
            weights.missing_units * missing_units +
            weights.unserviced * unserviced +
            weights.unparked * unparked +
            weights.unarrived * unarrived +
            weights.distance * physical_distance(context, state)
    return Float32(score)
end
