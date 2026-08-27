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
# robust-rail-planner is a consumer of it on its own release line.
#
# The :latest tag is only applied to final releases (no prerelease suffix), so a
# prerelease never shadows the current stable image.
#
# ARCHITECTURE: amd64 + arm64, like the other three. Measured 2026-08-11 on this
# builder: ~7m for arm64, ~2m for amd64.
#
# No host setup is needed for the arm64 half — no qemu-user-static, no
# `tonistiigi/binfmt --install`. The docker-container driver runs BuildKit inside
# a container whose image bundles QEMU emulators, and uses them for foreign-arch
# stages on its own. That is why the sibling repos have always built arm64 here
# without anyone installing anything.
#
# Do not read `docker buildx ls` as saying otherwise. Its PLATFORMS column lists
# what the *host* can execute — native architectures plus whatever is registered
# in binfmt_misc — and says nothing about BuildKit's bundled emulation. On this
# machine it shows only linux/amd64 and linux/386 while arm64 builds fine. Trust
# a build, not the column.
#
# What keeps the arm64 build tolerable is the cross-compiled ENHSP stage in the
# Dockerfile: it is Java bytecode, so FROM --platform=$BUILDPLATFORM keeps it
# native and byte-identical. Julia's Pkg.instantiate() genuinely does precompile
# native code and has to stay in the target-arch stage, which is most of what
# remains.
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
PLATFORMS="linux/amd64,linux/arm64"

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
