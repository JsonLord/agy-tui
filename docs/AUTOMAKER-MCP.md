# automaker-local MCP adapter (Phase 1)

Portable stdio-only MCP adapter that proxies a narrow, typed subset of the
local Automaker HTTP API. Automaker remains the single source of truth for
projects, jobs (features), worker state, auto-mode, worktrees, PRs and
execution scheduling. The adapter never touches Automaker source, its `data/`
directory, its database, its `.api-key` file, or its configuration, and it
binds no network listener of its own.

## Position in the target stack

```text
Automaker = canonical execution state (Kanban, jobs, scheduling)
AGY       = MCP caller / UI (this adapter is its Automaker gateway)
Argus     = future planner / decomposition / review
Foreman   = future worker supervisor
OpenCode  = future coding executor (workspace-native models/providers)
```

Target chain (later phases, not implemented here):

```text
Argus plan -> ClickUp approval -> Automaker job -> Foreman -> Laya + OpenCode
```

This adapter does NOT synchronize ClickUp. Future ClickUp approval will gate
the CONTROL tier; until then CONTROL calls are direct explicit
operator/agent actions. There is no second Kanban and no scheduler here.

## Architecture

```text
AGY / MCP client
      | stdio
automaker-local (stdio MCP server, no listener)
      | authenticated HTTP, fixed endpoint set, bounded responses
http://127.0.0.1:3008
      |
existing Automaker server (AutoMaker-Org/automaker @ 5888d2e6)
```

Every tool maps to exactly one verified Automaker endpoint. There is no
generic HTTP proxy, no caller-supplied URL/path/method, and no direct file or
database access. Classification lives in `app/automaker_local/policy.py`
(`TOOL_CLASSES`), never in HTTP verbs: `POST /api/auto-mode/status` and
`POST /api/features/list` are semantically READ.

## Tool surface (Phase 1, complete)

### READ

| MCP tool | Automaker endpoint | Notes |
| --- | --- | --- |
| `automaker_health` | `GET /api/health` | bounded status/version/timestamp |
| `automaker_projects` | `GET /api/projects/overview` | normalized project summaries |
| `automaker_jobs` | `POST /api/features/list` | POST verb, read semantics; project-scoped |
| `automaker_job_status` | `POST /api/features/get` | one bounded job incl. description/error |
| `automaker_running_agents` | `GET /api/running-agents` | active executor executions |
| `automaker_autonomy_status` | `POST /api/auto-mode/status` | POST verb, read semantics; global or per-project |
| `automaker_backlog_status` | `GET /api/backlog-plan/status` | is-running + saved-plan presence only |

### CONTROL (mutates Automaker state)

| MCP tool | Automaker endpoint | Notes |
| --- | --- | --- |
| `automaker_job_create` | `POST /api/features/create` | typed `JobCreateInput`; extra fields forbidden |
| `automaker_job_update` | `POST /api/features/update` | whitelisted fields; no delete/bulk |
| `automaker_worker_start` | `POST /api/agent/start` | sessionId (+ optional workingDirectory) only |
| `automaker_worker_stop` | `POST /api/agent/stop` | explicit sessionId required; never stop-all |

### Not exposed (Phase 1 hard exclusions)

feature delete, bulk update, bulk delete, `/api/agent/model`, queue
add/remove/clear, `/api/agent/send`, auto-mode start/stop/stop-feature,
backlog-plan apply/stop/clear, spec-regeneration mutations, session delete,
git/worktree merge, PR merge, arbitrary URLs, generic HTTP proxy. These may
return only behind stronger approval tiers (ClickUp-gated) in later phases.

`automaker_worker_start` deliberately has no model/provider/thinking/reasoning
fields: the target architecture executes through workspace-native OpenCode
models, so the adapter must not introduce hidden model selection.

## Environment contract

```text
AUTOMAKER_BASE_URL   optional, default http://127.0.0.1:3008
AUTOMAKER_API_KEY    required, supplied by the operator via env only
```

Base URL validation fails closed (`LOCAL_CONFIGURATION_REQUIRED`) unless:
scheme is `http`, host is exactly `127.0.0.1`, `localhost` or `[::1]`, no
credentials, no path/query/fragment. `0.0.0.0`, LAN IPs and public hostnames
are rejected. Remote/LAN mode is intentionally not implemented; Automaker's
own server default `HOST=0.0.0.0` must be overridden to `127.0.0.1` (see
validation below) so AGY never reaches a LAN-exposed execution system.

The key is never logged, never returned, and never embedded in error text
(all failures are stable codes). On the wire the adapter sends both
`X-API-Key` (the scheme verified in Automaker `lib/auth.ts` at SHA
`5888d2e6`) and `Authorization: Bearer` (the integration contract header;
current Automaker ignores it, future versions may honor it). It never reads
`automaker/data/.api-key`; the operator supplies the value.

## Failure codes

```text
AUTOMAKER_UNAVAILABLE  AUTOMAKER_AUTH_FAILED  AUTOMAKER_BAD_RESPONSE
AUTOMAKER_TIMEOUT      AUTOMAKER_NOT_FOUND    AUTOMAKER_CONFLICT
AUTOMAKER_REQUEST_REJECTED                   LOCAL_CONFIGURATION_REQUIRED
```

Responses are JSON-only with content-type validation, size-bounded, timeout
bounded. Reads may retry once on connect-level failure only; mutating calls
never retry. Raw server error bodies (stack traces) are discarded.

## Manual MCP registration

Never auto-edited by AGY: `~/.config/opencode/opencode.json`,
`~/.gemini/config/mcp_config.json` and any other client config stay
untouched. Insert this entry yourself into the client's `mcpServers` object,
keeping other servers:

```json
{
  "mcpServers": {
    "automaker-local": {
      "command": "<absolute python executable of the AGY venv>",
      "args": ["<absolute path>\\agy-tui\\scripts\\automaker-local.py"],
      "env": {
        "AUTOMAKER_BASE_URL": "http://127.0.0.1:3008",
        "AUTOMAKER_API_KEY": "<set locally, never commit>"
      }
    }
  }
}
```

The API key placeholder must be replaced only inside the user's own client
config. Never commit a real key.

## Native-Windows live validation (manual, read-only)

1. Start Automaker loopback-only yourself (AGY never starts it):

```powershell
cd C:\Users\LeonGründling\automaker
$env:HOST="127.0.0.1"
npm run dev:server
```

2. From the AGY checkout, run the read-only doctor (no mutation calls):

```powershell
$env:AUTOMAKER_BASE_URL="http://127.0.0.1:3008"
$env:AUTOMAKER_API_KEY="<from your local Automaker setup>"
C:\temp\agytests-venv\Scripts\python.exe scripts\automaker-doctor.py
# optional project-scoped check:
C:\temp\agytests-venv\Scripts\python.exe scripts\automaker-doctor.py --project-path "C:\path\to\project"
```

It reports one result per READ tool with stable codes and exit code 0 only
when every read check succeeds. It performs no CONTROL calls and never
touches Automaker files. Synthetic unit tests run against a fake localhost
server; they are not live-device validation. A run counts as live validation
only when performed against a real Automaker process.
