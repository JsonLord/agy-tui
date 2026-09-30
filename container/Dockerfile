# P20-F — AGY Hybrid production image.
#
# CPU CONTROLLER ONLY. No local LLM weights are bundled — the heavy models
# (Gemini/cloud planner, Helmholz, other providers) stay REMOTE and are
# reached over their APIs with credentials injected at runtime (P20-E).
#
# Base: python:3.13-slim-bookworm — glibc, matches the glibc-linked
# opencode worker binary and the host (Debian) exactly.
FROM python:3.13-slim-bookworm

# Runtime tools the supervisor + worker backend need. No compilers:
# requirements.txt installs from wheels only (CPU, no source builds).
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates git procps \
 && rm -rf /var/lib/apt/lists/*

# P19-D: explicit, pinned dependencies — installed into the image's
# managed python (NEVER `pip --break-system-packages`).
WORKDIR /opt/agy
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The accepted release tree (application code). No .git, no state, no venv.
COPY app/ app/
COPY mcp_gateway.py .
COPY scripts/ scripts/
COPY container/ container/
# opencode prompt files (the config references ./prompts/<name>.txt); the
# entrypoint materializes them next to the served config at startup.
COPY container/opencode-prompts/ container/opencode-prompts/
ARG AGY_RELEASE_SHA=""
RUN chmod +x scripts/entrypoint.sh scripts/agy-hybrid-release
# P19/P20: the image ships WITHOUT .git, so the accepted release SHA is
# baked as a marker file the runtime reports (release_info falls back to
# it). An empty build-arg leaves it absent → release reports "unknown"
# (never a fake SHA).
RUN if [ -n "$AGY_RELEASE_SHA" ]; then printf '%s' "$AGY_RELEASE_SHA" > release-sha; fi

# Worker backend: the versioned OpenCode binary (CPU-only, glibc-linked).
# It is a build-input artifact (copied in before build), NOT source.
COPY container/opencode /usr/local/bin/opencode
RUN chmod +x /usr/local/bin/opencode

# P20-A/B: all runtime paths are env-configurable; the container defaults
# keep durable state on a VOLUME (/data/agy), never the disposable fs.
ENV AGY_RELEASE_ROOT=/opt/agy \
    AGY_PYTHON=/usr/local/bin/python3 \
    AGY_STATE_ROOT=/data/agy \
    AGY_HEALTH_PORT=8080 \
    OPENCODE_HOSTNAME=127.0.0.1 \
    OPENCODE_PORT=4096

# Durable state volume: projects, plans, runs, human decisions, planner
# requests, continuations, batch state, events.
VOLUME ["/data/agy"]

# The health endpoint is the container's HTTP surface (P20-D). The MCP
# gateway is stdio (per-connection); the project supervisor is on-demand
# CLI (advance_project) — no resident model wait-loop.
EXPOSE 8080

ENTRYPOINT ["bash", "/opt/agy/scripts/entrypoint.sh"]
