FROM ubuntu:22.04

ARG JULIA_VERSION=1.10.5

ARG VERSION=0.0.0
LABEL org.opencontainers.image.source="https://github.com/Robust-Rail-NL/planning-approach" \
      org.opencontainers.image.description="Robust Rail PDDL planner" \
      org.opencontainers.image.version="${VERSION}"

ENV DEBIAN_FRONTEND=noninteractive

# --- System deps: JDK for ENHSP, Python for main.py, build tools ---
RUN apt-get update && apt-get install -y --no-install-recommends \
        openjdk-17-jdk-headless \
        python3 python3-pip \
        ca-certificates curl git build-essential \
    && rm -rf /var/lib/apt/lists/*

# Stable JAVA_HOME symlink, independent of host architecture (amd64/arm64)
RUN JAVA_BIN=$(readlink -f "$(which java)") \
    && ln -sfn "$(dirname "$(dirname "$JAVA_BIN")")" /opt/java-home
ENV JAVA_HOME=/opt/java-home

# --- Julia ---
RUN ARCH="$(dpkg --print-architecture)" \
    && if [ "$ARCH" = "amd64" ]; then JULIA_DIR_ARCH=x64; JULIA_FILE_ARCH=x86_64; \
       elif [ "$ARCH" = "arm64" ]; then JULIA_DIR_ARCH=aarch64; JULIA_FILE_ARCH=aarch64; \
       else echo "Unsupported architecture: $ARCH" >&2; exit 1; fi \
    && MINOR="${JULIA_VERSION%.*}" \
    && curl -fsSL "https://julialang-s3.julialang.org/bin/linux/${JULIA_DIR_ARCH}/${MINOR}/julia-${JULIA_VERSION}-linux-${JULIA_FILE_ARCH}.tar.gz" -o /tmp/julia.tar.gz \
    && mkdir -p /opt/julia \
    && tar -xzf /tmp/julia.tar.gz -C /opt/julia --strip-components=1 \
    && ln -s /opt/julia/bin/julia /usr/local/bin/julia \
    && rm /tmp/julia.tar.gz

# --- ENHSP (build the jar from source) ---
RUN git clone --depth 1 --branch enhsp-20 https://github.com/hstairs/enhsp.git /tmp/enhsp-src \
    && cd /tmp/enhsp-src \
    && ./compile \
    && mkdir -p /opt/enhsp \
    && cp -r enhsp-dist/. /opt/enhsp \
    && rm -rf /tmp/enhsp-src
ENV ENHSP_JAR=/opt/enhsp/enhsp.jar

WORKDIR /app

# --- Julia deps (cached separately from app code) ---
ENV JULIA_DEPOT_PATH=/opt/julia-depot
COPY plan/Project.toml plan/Manifest.toml plan/
RUN julia --project=plan -e 'using Pkg; Pkg.instantiate()'

# --- Python deps ---
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# --- App code ---
COPY . .

# Cheap insurance: COPY preserves the host's mode bit, so this only matters when
# the file arrives without it (a checkout that dropped it, a Windows clone).
RUN chmod +x /app/docker-entrypoint.sh

# The visualizer's default port, for `docker run -p 8767:8767 ... visualizer`.
# Documentation only — EXPOSE publishes nothing by itself.
EXPOSE 8767

# A dispatcher rather than main.py directly, so the image can also serve the
# visualizer. Argument lists that start with a flag still reach main.py
# unchanged, which is what run_planner.py sends.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
