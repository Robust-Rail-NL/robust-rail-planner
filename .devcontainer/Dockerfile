FROM mcr.microsoft.com/devcontainers/base:ubuntu-22.04

ARG JULIA_VERSION=1.10.5
ARG NODE_MAJOR=20

# --- System deps: JDK for ENHSP, Python for main.py, build tools ---
RUN apt-get update && apt-get install -y --no-install-recommends \
        openjdk-17-jdk-headless \
        python3 python3-pip python3-venv \
        build-essential \
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

ENV JULIA_DEPOT_PATH=/opt/julia-depot
RUN mkdir -p /opt/julia-depot \
    && julia -e 'using Pkg; Pkg.add(["PDDL", "SymbolicPlanners"])' \
    && chown -R vscode:vscode /opt/julia-depot

# --- ENHSP (build the jar from source) ---
RUN git clone --depth 1 --branch enhsp-20 https://github.com/hstairs/enhsp.git /tmp/enhsp-src \
    && cd /tmp/enhsp-src \
    && ./compile \
    && mkdir -p /opt/enhsp \
    && cp -r enhsp-dist/. /opt/enhsp \
    && rm -rf /tmp/enhsp-src
ENV ENHSP_JAR=/opt/enhsp/enhsp.jar

# --- Python deps ---
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

USER vscode
