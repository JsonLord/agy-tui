# AGY factory control plane

This phase imports plans and records approval, creates/reconciles Automaker jobs,
and aggregates live status. It launches no worker. It supersedes the older
`spec.md` runtime choice for this phase: the intended external Argus dependency
is **argus-skill 0.1.1, lbx154/Argus**, with `argus-plugin-server` over stdio.
Its installed tool contract has not been live-tested here. No Argus code is
vendored or executed, and no Microsoft Argus runtime is implemented.

```text
AGY
 │
 ├─ Argus MCP
 │     planner/reviewer
 │
 ├─ approval boundary
 │
 └─ Factory Control Plane
        │
        ▼
    Automaker
        │
        ▼
    Foreman [future]
        │
        ▼
    OpenCode [future]
```

## State ownership

| Owner | Authoritative state |
| --- | --- |
| AGY | goal/progress aggregation and cross-system reference registry |
| Argus | planning, decomposition, independent review |
| approval layer | proposed/approved/rejected records for exact imported revision |
| Automaker | canonical features/jobs, execution state and scheduling |
| Foreman (future) | one assigned worker attempt and its supervision |
| OpenCode (future) | local checklist/session/model/provider |

`app/factory_local` never schedules jobs, polls in a daemon, starts auto-mode,
reads external runtime files, copies execution logs or adopts sessions.
Only the approved import scope is retained along with reference mappings and
approval audit records. Titles/descriptions in that immutable envelope are
necessary to bind the exact approval and reconcile creates, not a synchronized
copy of Automaker cards. Live card contents/status remain in Automaker.

## Import contract and identities

`FactoryPlan` is **AGY's interchange schema**, not claimed official Argus output.
A later adapter can translate Argus Manager/Planner output into this format.
`argus_message` is the intended external Manager front door, not an AGY tool.
No private Argus state parsing or Manager/Reviewer implementation exists here.

A plan has an immutable `plan_id`, goal ref, up to 100 tasks and 100 dependency
edges. Each task carries separate task/Argus IDs, workspace ID, repository
identity and Automaker project path. Paths are opaque portable strings and are
matched exactly against Automaker's registered projects before materialization;
AGY does not open them or assume they are OpenCode-registered workspaces.
Duplicate IDs, missing/self/duplicate edges and cycles are rejected. Cross-project
edges are currently rejected because native feature dependencies are project-local.
Edited scope requires a **new plan ID and new approval**, never an in-place rewrite.

```json
{
  "plan_id": "report-v1",
  "goal": {"goal_id": "monthly-report", "argus_project_id": "external-project-ref"},
  "tasks": [
    {"task_id": "extract", "argus_task_id": "external-task-ref", "title": "Prepare synthetic reporting data", "workspace_id": "reports", "repository": "owner/report", "project_path": "C:/Work/Reports"},
    {"task_id": "validate", "title": "Validate synthetic report", "workspace_id": "reports", "repository": "owner/report", "project_path": "C:/Work/Reports"}
  ],
  "dependencies": [{"task_id": "validate", "requires_task_id": "extract"}]
}
```

`FactoryJobRef`, worker/attempt/evidence/completion/blocker types define future
references without requiring external IDs at plan creation. The materialization
mapping adds distinct Automaker IDs, approval refs and SHA-256 idempotency keys.
No system ID is reused as another system's ID. Evidence schema exists; collection
and independent review are not connected (`evidence_status=NOT_CONNECTED`).

## Approval versus execution

Approval is `PROPOSED`, `APPROVED` or `REJECTED`. A record contains approval ID,
plan ID/revision hash, source, timezone-aware timestamp and actor ref. Manual
source is the only accepted source today. `clickup-future` is deliberately not
accepted as a verified source until an adapter can establish actor/revision
provenance. ClickUp credentials/API are absent.

These are **trusted operator assertions**, not identity authentication or proof
that Gemini's caller is a human. The MCP client must restrict approval CONTROL
tool access to the operator. Never advertise an agent-supplied actor string as
human verification. Live autonomous execution remains disabled/out of scope.
Records are immutable and the latest record governs before materialization;
record updates after any create intent are refused. No retroactive revocation of
external jobs is implemented. Partial approvals can use independently approved
plan envelopes; task-level partial approval within one envelope is future work.

Example approval, using the exact returned revision:

```json
{"approval_id":"operator-approval-1","plan_id":"report-v1","plan_revision":"<64-character returned revision hash>","source":"manual","status":"APPROVED","timestamp":"2026-10-14T12:00:00Z","actor_ref":"local-operator-reference"}
```

Execution is normalized separately: pending→PENDING, running→RUNNING,
completed→VERIFYING, verified→COMPLETED, failed→FAILED. Unknown values→BLOCKED,
with bounded raw status retained. `completed` is not an independent review
claim. NOT_MATERIALIZED/READY/ESCALATED vocabulary is available for future
boundaries; this phase never sets execution status in Automaker or dispatches
READY work. Dependency readiness is calculated metadata only, requiring
prerequisites' native `verified` status.

## Automaker boundary and recovery

Uses unchanged `AutomakerClient` and `JobCreateInput` from `automaker-local`:
fixed endpoints, operator environment credentials, validated loopback URL,
no generic HTTP or raw data access. No changes to hybrid-local/automaker-local.
Source inspected at AutoMaker-Org/automaker
`5888d2e6f3618e06762803687ea7b181c10edcbd`:
`libs/types/src/feature.ts` declares `dependencies?: string[]`;
`apps/server/src/routes/features/routes/create.ts` forwards feature fields;
`apps/server/src/services/feature-loader.ts` create spreads fields into the
stored feature. Dependencies therefore use actual returned feature IDs in
plan topological order. This is materialization order, not an execution scheduler.

Create's supplied ID could overwrite an existing feature, and the current
restricted adapter intentionally exposes no ID override. Instead each description
gets `[agy-factory-v1:<sha256(plan ID, revision, task ID)>]`. Registry SQLite WAL
with FULL synchronous writes commits an INTENT **before** external create.
Conditional insert/approval recheck serializes competing claims. Successful
responses are checked for ID, title, description and dependencies before mapping.
Retries validate existing mappings, or reconcile an unresolved intent through
feature-list marker matching. Exactly one unchanged match is mapped. Zero or
multiple matches, edited scope, wrong IDs or malformed responses block; no
blind mutation retry. A process crash between intent and request may therefore
require operator investigation even if no job was created. The phase provides
no unsafe reset/delete/recreate escape tool; after resolving externally, matching
jobs reconcile on retry. Otherwise stop and use a separately approved replacement
plan only after confirming old jobs cannot execute. Deleted mapped jobs are not
recreated automatically.

Materialization refuses unregistered projects and non-idle/unknown auto-mode.
**The external API has no atomic create-inert-and-reserve operation**: an operator
must keep Automaker auto-mode off during materialization. The observation cannot
prevent a concurrent external operator from enabling auto-mode. This phase calls
no worker/queue/auto-mode mutation endpoint and cannot claim a global execution
lock against independent Automaker clients.

## MCP and portable use

Seven stdio-only tools:

| Class | Tools |
| --- | --- |
| READ | factory_status, factory_plan_status, factory_job_map, factory_blockers |
| CONTROL | factory_register_plan, factory_record_approval, factory_materialize_approved_plan |

No listener, arbitrary URL/shell, generic Argus command or execution escape exists.
Errors are bounded stable codes with correlation IDs, excluding server/private
exception details. Status reads live Automaker truth, so external failure returns
BLOCKED rather than stale success. Job-map reads require no external credentials.

Use the existing isolated AGY Python environment (Python 3.11–3.13, locked MCP/
Pydantic dependencies). Run `scripts/factory-local.py` by absolute path. This phase
uses the checkout launcher; the older Windows hybrid installer source allowlist
is intentionally unchanged and does not bundle factory-local yet.
Manual registration fragment (merge yourself, preserve existing servers):

```json
{"mcpServers":{"factory-local":{"command":"<absolute AGY venv python>","args":["<absolute checkout>/scripts/factory-local.py"],"env":{"FACTORY_STATE_PATH":"<absolute user-local path>/factory.sqlite","AUTOMAKER_BASE_URL":"http://127.0.0.1:3008","AUTOMAKER_API_KEY":"<set locally, never commit>"}}}}
```

Missing/nonabsolute FACTORY_STATE_PATH fails closed. Keep this DB untracked,
user-local and access-restricted. Approval refs and plans must be sanitized by
operators: the schema does not detect secrets embedded in free text. No secret
store is read or copied; API keys stay in memory/inherited environment.
No MCP/client/OpenCode config is edited automatically.

## Future Foreman and ClickUp seams

`FactoryExecutor` describes `start(job, workspace)`, `status(attempt)` and
`cancel(attempt)`. Only a test fake implements it. No Foreman client, dependency,
repository changes or invocation exists. Automaker assigns the job; Foreman will
supervise that one attempt, with future run/evidence refs retained separately.
Laya plugs into that future repository/runtime, not this registry. The interface
is ready for adapter implementation, not evidence of live compatibility.

ClickUp will produce revision-bound approval records after verified provenance;
Argus will supply import envelopes and consume evidence for review. Neither is
connected now. Production AGY, Windows, Argus, Automaker, ClickUp, Foreman and
OpenCode live integration remain unvalidated by this synthetic phase.

## Tests

```bash
.venv-hybrid/bin/python -m pytest -q tests/test_factory_local.py tests/test_automaker_local.py tests/test_hybrid_local.py tests/test_opencode_capabilities.py tests/test_opencode_resolution.py tests/test_owned_process_shutdown.py
```

Tests include actual stdio process restart with a synthetic loopback HTTP service,
space/non-ASCII state paths, canonical API mapping and live status changes, plus
concurrent claims, approval guards, DAG validation, lost-response reconciliation,
conflicts, transaction rollback, bounded errors and fake executor seam. No local
OpenCode executable or real external service is required or invoked.
