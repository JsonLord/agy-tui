#!/usr/bin/env bash
# P20-C — AGY Hybrid container supervisor (entrypoint).
#
# A container CANNOT rely on user systemd. This is the single
# container-friendly entrypoint that starts and supervises the resident
# processes the AGY runtime needs:
#
#   1. OpenCode server        (coding-worker backend, :4096)
#   2. AGY health endpoint    (release/project-supervisor reachability,
#                              P20-D — exposes /health)
#   3. deterministic sentinel (process observer, event-driven — the P7
#                              sentinel; NOT a model-owned poller)
#
# It deliberately does NOT start a bespoke init system and does NOT run
# any model wait-loop. The MCP gateway is stdio (launched per-connection
# by the MCP client via agy-hybrid-mcp) — here we only VERIFY it loads.
#
# Everything is env-driven (P20-A): AGY_STATE_ROOT, OPENCODE_SERVER_URL,
# AGY_RELEASE_ROOT, AGY_HEALTH_PORT, etc. No host path is hard-required.
set -euo pipefail

RELEASE_ROOT="${AGY_RELEASE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$RELEASE_ROOT"
PY="${AGY_PYTHON:-python3}"
STATE_ROOT="${AGY_STATE_ROOT:-$RELEASE_ROOT/state}"
HEALTH_PORT="${AGY_HEALTH_PORT:-8080}"
OPENCODE_URL="${OPENCODE_SERVER_URL:-http://127.0.0.1:4096}"
HOSTNAME_BIND="${OPENCODE_HOSTNAME:-127.0.0.1}"
PORT="${OPENCODE_PORT:-4096}"

mkdir -p "$STATE_ROOT"

# P20-E: fail clearly if the state root is not writable (durable state must
# live on a mounted volume, not the disposable container fs).
if ! ( : > "$STATE_ROOT/.write-probe" ) 2>/dev/null; then
  echo "FATAL: state root $STATE_ROOT is not writable" >&2
  exit 1
fi
rm -f "$STATE_ROOT/.write-probe"
echo "state root: $STATE_ROOT"
echo "release root: $RELEASE_ROOT"

# P19-D: no manual `pip --break-system-packages` as deployment — the image
# bakes the runtime into the venv; verify the required imports exist.
for mod in pydantic mcp google.genai; do
  if ! "$PY" -c "import $mod" >/dev/null 2>&1; then
    echo "FATAL: required runtime module missing: $mod" >&2
    exit 1
  fi
done
echo "runtime deps present"

# --- OpenCode worker backend ---------------------------------------------
OPENCODE_BIN="${OPENCODE_BIN:-/usr/local/bin/opencode}"
OPENCODE_CONFIG_DIR="${OPENCODE_CONFIG_DIR:-/opt/agy/opencode-config}"
export OPENCODE_SERVER_URL="${OPENCODE_SERVER_URL:-http://127.0.0.1:${PORT}}"

# P20-D: mcp_gateway requires the legacy agentic provider env at IMPORT
# time (Settings.from_env). In a container the real values come from the
# deployment env; when absent, set explicit UNSET placeholders so the
# gateway imports and those specific tools fail clearly AT CALL TIME —
# no fake endpoints, no secrets baked in. (Placed here so OPENCODE_SERVER_URL
# is already exported for WORKER_BASE_URL's default.)
: "${PLANNER_BASE_URL:=UNSET}"; export PLANNER_BASE_URL
: "${PLANNER_MODEL:=UNSET}"; export PLANNER_MODEL
: "${WORKER_BASE_URL:=$OPENCODE_SERVER_URL}"; export WORKER_BASE_URL
: "${WORKER_MODEL:=UNSET}"; export WORKER_MODEL
OPENCODE_PID=""
SENTINEL_PID=""

# P20-E: worker-backend credentials are env-injected, NEVER in the image.
# Materialize the REDACTED opencode config template with secrets from the
# environment. A provider whose key is absent is disabled (not faked) — the
# container reports the worker provider unavailable rather than lying.
if [ -f "$RELEASE_ROOT/container/opencode.template.jsonc" ]; then
  mkdir -p "$OPENCODE_CONFIG_DIR"
  sed -e "s/__APIKEY_ENV__/${HELMHOLTZ_API_KEY:-__ABSENT__}/g" \
      "$RELEASE_ROOT/container/opencode.template.jsonc" \
      > "$OPENCODE_CONFIG_DIR/opencode.jsonc"
  if [ -z "${HELMHOLTZ_API_KEY:-}" ]; then
    echo "WARN: HELMHOLTZ_API_KEY not set — helmholtz worker provider disabled (no fake credentials)"
    # flip the helmholtz provider to disabled so opencode starts without it
    python3 - "$OPENCODE_CONFIG_DIR/opencode.jsonc" <<'PYFIX'
import json, re, sys
p = sys.argv[1]
t = open(p).read()
t = re.sub(r'^\s*//.*$', '', t, flags=re.M)
t = re.sub(r',\s*([}\]])', r'\1', t)
d = json.loads(t)
h = d.get('provider', {}).get('helmholtz')
if h is not None:
    h['enabled'] = False
    if h.get('apiKey') in ('', '__ABSENT__'):
        h['apiKey'] = ''
open(p, 'w').write(json.dumps(d, indent=2))
PYFIX
  fi
  export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-/opt/agy/xdg-config}"
  mkdir -p "$XDG_CONFIG_HOME/opencode"
  cp "$OPENCODE_CONFIG_DIR/opencode.jsonc" "$XDG_CONFIG_HOME/opencode/opencode.jsonc"
  # The config references prompt files relatively (./prompts/…) — ship them
  # next to BOTH copies (the served one uses XDG_CONFIG_HOME).
  if [ -d "$RELEASE_ROOT/container/opencode-prompts" ]; then
    for d in "$OPENCODE_CONFIG_DIR" "$XDG_CONFIG_HOME/opencode"; do
      mkdir -p "$d/prompts"
      cp "$RELEASE_ROOT/container/opencode-prompts/"*.txt "$d/prompts/" 2>/dev/null || true
    done
  fi
fi

# Start the worker backend (skipped, with a clear warning, if unavailable).
if [ ! -x "$OPENCODE_BIN" ]; then
  echo "WARN: opencode binary not found at $OPENCODE_BIN (worker backend unavailable)"
else
  echo "starting opencode server on $HOSTNAME_BIND:$PORT"
  "$OPENCODE_BIN" serve --hostname "$HOSTNAME_BIND" --port "$PORT" \
    & OPENCODE_PID=$!
fi

# P20-E: containers are never GCE — the google.auth ADC fallback probe
# would hang on the metadata server. Skip it; env-var credential checks
# remain authoritative (Helmholtz/Gemini keys come from the deployment).
export AGY_SKIP_ADC_PROBE=1

# --- AGY health endpoint (release / project-supervisor) -------------------
echo "starting AGY health on 0.0.0.0:$HEALTH_PORT"
AGY_HEALTH_PORT="$HEALTH_PORT" AGY_STATE_ROOT="$STATE_ROOT" \
  "$PY" -m app.health --host 0.0.0.0 --port "$HEALTH_PORT" \
  & HEALTH_PID=$!

# --- deterministic sentinel (process observer) ---------------------------
# The P7 sentinel is event-driven and local; it owns process observation.
# It is NOT a model wait-loop. Skip if opencode is unavailable (nothing to
# observe) or if the sentinel package is absent.
if [ -n "${OPENCODE_PID:-}" ] && "$PY" -c "import app.orchestration.sentinel" >/dev/null 2>&1; then
  echo "starting sentinel observer"
  SENTINEL_STATE_ROOT="${SENTINEL_STATE_ROOT:-$STATE_ROOT/sentinel}"
  "$PY" -m app.orchestration.sentinel daemon --state-root "$SENTINEL_STATE_ROOT" \
    & SENTINEL_PID=$!
fi

# --- MCP gateway reachability (verify only; stdio is per-connection) ------
if "$PY" -c "import mcp_gateway" >/dev/null 2>&1; then
  echo "mcp_gateway loads (stdio — launched per-connection by the MCP client)"
else
  echo "WARN: mcp_gateway failed to import"
fi

echo "supervisor ready (opencode=${OPENCODE_PID:-none} health=$HEALTH_PID sentinel=${SENTINEL_PID:-none})"

# --- trap: tear down children on stop ------------------------------------
cleanup() {
  echo "supervisor: shutting down"
  for pid in "${OPENCODE_PID:-}" "$HEALTH_PID" "${SENTINEL_PID:-}"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup TERM INT

# Keep the entrypoint alive; exit non-zero if the health endpoint dies.
while true; do
  if ! kill -0 "$HEALTH_PID" 2>/dev/null; then
    echo "health endpoint exited — shutting down" >&2
    cleanup
    exit 1
  fi
  sleep 5
done
