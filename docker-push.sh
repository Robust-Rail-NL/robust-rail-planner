#!/usr/bin/env bash
# Build and push the planner image to ghcr.io.
#
# The version is read from the VERSION file (the single source of truth — bump
# it there) and passed into the image as a build-arg, so the Dockerfile LABEL
# never needs a separate edit. The sibling repos read theirs from pyproject.toml
# (generator), HIP.csproj (solver) and CMakeLists.txt (evaluator); this repo has
# no equivalent manifest since the move away from packaging, hence a plain file.
#
# The version is this repo's own, deliberately not 2.0.0. The generator, solver
# and evaluator share that number because they share an interchange format;
# planning-approach is a consumer of it on its own release line.
#
# The :latest tag is only applied to final releases (no prerelease suffix), so a
# prerelease never shadows the current stable image.
#
# ARCHITECTURE: amd64 only, for now — unlike the other three, which ship
# linux/amd64,linux/arm64. This image builds ENHSP from source and instantiates
# Julia's depot, and both would run under QEMU emulation for arm64 on an amd64
# host.
#
# As of 2026-08-10 the shared builder cannot do it at all: `docker buildx
# inspect robust-rail-builder` lists only linux/amd64 variants and linux/386, so
# no arm64 binfmt handler is registered on this machine. Registering one is a
# privileged, host-wide change:
#
#     docker run --privileged --rm tonistiigi/binfmt --install arm64
#
# After that, time a build before committing to shipping it:
#
#     time docker buildx build --builder "$BUILDER_NAME" --platform linux/arm64 .
#
# If that is too slow, the first thing to try is a cross-compiled ENHSP stage:
# `FROM --platform=$BUILDPLATFORM` for the builder, then COPY the jar into the
# target-arch image. ENHSP compiles to Java bytecode, which is
# architecture-independent, so emulating that step buys nothing — check that
# enhsp-dist/ ships no native .so first. Julia's Pkg.instantiate() does
# precompile native code and has to stay in the target-arch stage.
#
# Requires a buildx builder using the "docker-container" driver with
# network=host. The default driver runs the BuildKit container in an isolated
# network namespace whose DNS resolution can fail to reach private/LAN DNS
# servers (seen as: "docker build" works, "docker buildx build" times out
# resolving a private host). network=host makes the builder share the host's
# network stack, avoiding that failure mode.
#
# BUILDER_NAME is shared with the sibling Robust-Rail-NL projects that need the
# same setup — a buildx builder isn't tied to a specific repo or Dockerfile.
set -euo pipefail

IMAGE="ghcr.io/robust-rail-nl/planner"
BUILDER_NAME="robust-rail-builder"
PLATFORMS="linux/amd64"

VERSION=$(tr -d '[:space:]' < VERSION)
[[ -n "$VERSION" ]] || { echo "Could not read a version from VERSION" >&2; exit 1; }

TAGS=(-t "$IMAGE:$VERSION")
# Only a plain X.Y.Z gets :latest; anything with a suffix is a prerelease.
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] && TAGS+=(-t "$IMAGE:latest")

if ! docker buildx inspect "$BUILDER_NAME" >/dev/null 2>&1; then
    docker buildx create --name "$BUILDER_NAME" --driver docker-container --driver-opt network=host
fi

docker buildx build \
    --builder "$BUILDER_NAME" \
    --platform "$PLATFORMS" \
    --build-arg "VERSION=$VERSION" \
    "${TAGS[@]}" \
    --push \
    .
