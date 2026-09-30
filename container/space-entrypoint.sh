#!/usr/bin/env bash
# P21 — Hugging Face Space entrypoint.
#
# Layers the auth-gated project-supervisor frontend (the ONLY externally
# exposed surface, bound to 0.0.0.0:7860) on top of the generic P20
# container runtime. OpenCode stays loopback-only (never exposed publicly).
#
# Persistence (P21-B): if /data is present + writable it is the durable
# state root; otherwise the runtime starts in EXPLICIT demo mode (and
# refuses durable-project creation) — it never silently pretends ephemeral
# state is durable.
set -u

REPO_ROOT="/opt/agy"
PY="${AGY_PYTHON:-/usr/local/bin/python3}"
AGY_BIND="${AGY_BIND:-0.0.0.0}"
AGY_PORT="${AGY_PORT:-7860}"
OPENCODE_BIND="${OPENCODE_BIND:-127.0.0.1}"
OPENCODE_PORT="${OPENCODE_PORT:-4096}"

export OPENCODE_SERVER_URL="http://127.0.0.1:${OPENCODE_PORT}"
# Non-GCE runtime: skip the google.auth ADC metadata probe.
export AGY_SKIP_ADC_PROBE=1

# --- P21-B: resolve persistence, set AGY_STATE_ROOT ---------------------
DATA_DIR="${AGY_DATA_DIR:-/data}"
if [ -d "$DATA_DIR" ] && [ -w "$DATA_DIR" ]; then
  export AGY_STATE_ROOT="${AGY_STATE_ROOT:-$DATA_DIR/agy}"
  [ -n "${SPACE_DEMO_MODE:-}" ] || echo "persistence: DURABLE at $AGY_STATE_ROOT"
else
  if [ -n "${SPACE_DEMO_MODE:-}" ]; then
    export AGY_STATE_ROOT="${AGY_STATE_ROOT:-/tmp/agy-state}"
    echo "persistence: EPHEMERAL demo mode (no /data; not durable)"
  else
    echo "FATAL: no persistent storage at $DATA_DIR and not in demo mode."
    echo "  Mount a persistent volume at /data, or start with"
    echo "  SPACE_DEMO_MODE=1 for a disposable (non-durable) session." >&2
    exit 1
  fi
fi
mkdir -p "$AGY_STATE_ROOT"

# --- OpenCode worker backend (loopback only) ----------------------------
OPENCODE_BIN="${OPENCODE_BIN:-/usr/local/bin/opencode}"
if [ -x "$OPENCODE_BIN" ]; then
  export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-/opt/agy/xdg-config}"
  mkdir -p "$XDG_CONFIG_HOME/opencode"
  cp "$REPO_ROOT/container/opencode.jsonc" "$XDG_CONFIG_HOME/opencode/opencode.jsonc"
  cp -r "$REPO_ROOT/container/opencode-prompts" "$XDG_CONFIG_HOME/opencode/prompts"
  "$OPENCODE_BIN" serve --hostname "$OPENCODE_BIND" --port "$OPENCODE_PORT" &
  OPENCODE_PID=$!
  echo "opencode on $OPENCODE_BIND:$OPENCODE_PORT (pid $OPENCODE_PID, loopback-only)"
else
  echo "WARN: opencode binary absent — worker backend unavailable"
  OPENCODE_PID=""
fi

# --- Auth (P21-C): the project actions are protected ---------------------
if [ -z "${SPACE_API_KEY:-}" ]; then
  echo "WARN: SPACE_API_KEY not set — the frontend will require it before any"
  echo "      project action; set it as a Space secret for a usable deployment."
fi

# --- The ONE external surface: auth-gated project supervisor ------------
# API key: SPACE_API_KEY (the deployment/Space secret name) → the
# frontend's AGY_SPACE_API_KEY.
export AGY_SPACE_API_KEY="${SPACE_API_KEY:-}"
export AGY_SPACE_HOST="$AGY_BIND"
export AGY_SPACE_PORT="$AGY_PORT"
if [ -z "${SPACE_API_KEY:-}" ]; then
  echo "WARN: SPACE_API_KEY not set — the frontend will require it before any"
  echo "      project action; set it as a Space secret for a usable deployment."
fi
echo "starting project-supervisor frontend on ${AGY_BIND}:${AGY_PORT}"
AGY_BIND="$AGY_BIND" AGY_PORT="$AGY_PORT" AGY_STATE_ROOT="$AGY_STATE_ROOT" \
  SPACE_DEMO_MODE="${SPACE_DEMO_MODE:-}" \
  "$PY" -m app.space_server &
FRONT_PID=$!

# --- Deterministic P7 sentinel observer (event-driven, not a model loop) -
SENTINEL_PID=""
if [ -n "${OPENCODE_PID:-}" ] && "$PY" -c "import app.orchestration.sentinel" >/dev/null 2>&1; then
  SENTINEL_STATE_ROOT="${SENTINEL_STATE_ROOT:-$AGY_STATE_ROOT/sentinel}"
  "$PY" -m app.orchestration.sentinel daemon --state-root "$SENTINEL_STATE_ROOT" &
  SENTINEL_PID=$!
fi
echo "supervisor ready: openCode=$OPENCODE_PID front=$FRONT_PID sentinel=$SENTINEL_PID"

cleanup() {
  for pid in "${OPENCODE_PID:-}" "$FRONT_PID" "$SENTINEL_PID"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null
  done
}
trap cleanup INT TERM

while true; do
  if ! kill -0 "$FRONT_PID" 2>/dev/null; then
    echo "frontend exited — shutting down" >&2
    cleanup
    exit 1
  fi
  sleep 5
done
