# External Argus factory bridge

AGY sends bounded planning and review requests to independently installed
`argus-skill 0.1.1` (`lbx154/Argus`) through **stdio MCP**, never private Argus
files, an embedded runtime or an HTTP daemon. No package is installed, bundled
or modified by AGY. No OpenCode/Foreman/Laya/ClickUp execution is implemented.

```text
AGY / Gemini
     │
     ▼
Argus
 planning/review authority
     │
     ▼
FactoryPlan
 AGY interchange
     │
     ▼
Approval boundary
     │
     ▼
Automaker
 execution authority
     │
     ▼
Foreman [separate branch / future convergence]
     │
     ▼
OpenCode
```

## Pinned source evidence and safe route

Source inspected, not executed: upstream tag `v0.1.1`, annotated tag object
`9e3f8d10cda39161c1d43bb390ed27c914ef59b1`, commit
`9202c80f1aa221e7583eee1052158b244cca442b`.
Source archive SHA-256:
`666b2547d8740344d9f520b9beacfcdf3e20b8eeab283f1ba36a72fc5353bd71`.
Downloaded outside this checkout solely for inspection; no upstream source is
vendored. Source order was MCP/service, Manager route, native status/artifacts,
then docs and upstream tests (inspected, not run).

| Upstream file | Verified contract / implication |
| --- | --- |
| `pyproject.toml` | `argus-plugin-server = argus_skill.plugin.mcp_server:main` |
| `argus_skill/plugin/mcp_server.py` | seven tools; message takes only project_id/text; no dedicated planning/reviewer tool |
| `argus_skill/plugin/service.py` | `{ok: true, ...}` JSON; plain message task/resolved-question can start daemon; artifact API lists metadata |
| `argus_skill/webapi/manager_bridge.py` | explicit `/ask` returns `kind=chat, reply=...` before task/control dispatch; failure stays inline |
| `argus_skill/manager/ask_intent.py` | `/ask` and `/chat` recognized explicitly; no guessing intent |
| `argus_skill/roles/prompts/manager.py` | quick reply is message-only/tool-free Manager conversation |
| `argus_skill/webapi/mission_items.py` | status is partial backlog/questions/journal/roles, not a complete AGY plan contract |
| `argus_skill/webapi/artifacts.py` | artifact rows are allowlisted refs/metadata, sometimes bounded previews; no guaranteed full task DAG export |
| `argus_skill/webapi/project_state.py` | project-list native identity is `id` with `workdir` |
| `tests/plugin/test_mcp_server.py`, `tests/plugin/test_service.py`, `tests/test_ask_intent.py` | tool signatures, dispatch behavior, ask-prefix behavior corroborated |

**Every AGY generative call adds `/ask ` itself**, before the structured-output
prompt. Ordinary natural-language `argus_message` must not be used as a planning
shortcut: source shows it can dispatch execution. No stop/create/doctor tool is
called by this bridge. Runtime tool schemas are checked at tools/list before use;
this does not prove installed package version or enforce safety on a different
implementation. Operator must independently install/verify the inspected target.
Read-only doctor reports schema compatibility, not version/live planning proof.

This safe route invokes **Manager only**, not a native Planner task/Engineer
pipeline. Manager supplies planning advice in the requested interchange format.
Review uses a separate message/prompt and verdict protocol on that same front
door: **REVIEW_ROLE_IS_LOGICAL_NOT_TRANSPORT_SEPARATE**. It is not a separately
permissioned Reviewer process and never certifies that a native Reviewer ran.
These are honest limits of 0.1.1's exposed MCP surface. Native Argus may record
its own messages/intake/learning state during normal API operation; AGY neither
reads nor copies that state nor changes Argus package/config files.

## Explicit client and process ownership

`ArgusMcpClient` exposes project_list(), status(), artifacts(), message() only.
Its internal fixed tool set is `argus_project_list`, `argus_status`,
`argus_artifacts`, `argus_message`; no generic tool passthrough is exposed to
factory tools. The executable is operator configuration, never an MCP tool input.
No arbitrary URL/shell/daemon options are accepted. Default command is the
existing `argus-plugin-server`; `ARGUS_MCP_EXECUTABLE` selects an installed absolute
executable. Native Windows requires an `.exe`, not a shell shim.

The client uses the repository's locked MCP ClientSession with an owned bounded
stdio transport: startup/compatibility default 10 seconds, read/generative call
120 seconds (hard configuration ceiling 600), cleanup waits bounded to 5 seconds
per stage. Stderr is discarded; JSON-RPC lines and JSON tool results are bounded.
POSIX process groups and Windows kill-on-close Job Objects contain only the
launched MCP process/its descendants. Native Windows behavior remains pending;
process tests here execute on Linux. Child stdin closes normally, followed by
bounded owned-tree cleanup and pipe EOF confirmation. Unconfirmed cleanup fails
closed. Separate Argus cockpit/daemon processes are never adopted or killed,
and `argus_stop` is never invoked. Runtime backend/model/provider/auth settings
are inherited, never selected or copied to a credential store by this adapter.

## Registered project resolution

Set `FACTORY_PROJECT_REGISTRY` to an **absolute path** to an operator-maintained
AGY JSON mapping (not an Argus/OpenCode/client config file). The file is read-only,
size bounded, strict JSON, and rejects duplicate keys/workspaces/projects and
private native-config locations. No tool auto-registers a project or workspace.
Example, using the **actual** existing Argus project ID and exact workdir spelling:

```json
{"projects":[{"workspace_id":"reports","repository":"owner/report","project_path":"C:/Work/Reports","argus_project_id":"existing-argus-project-id"}]}
```

Before a generation call, Argus project-list must contain exactly one matching
native `id` and exact `workdir`. Every imported task's workspace/repository/path
must match that mapping, and the plan's Argus project ref must match the request.
No local filesystem path is accepted from model output as a grant. Paths are
compared as registered strings (portable; no opening/traversing workspaces or
native credentials), so Windows slash/spelling differences require an accurate
operator mapping rather than silent canonicalization. Importing a supplied
payload validates against the operator registry without invoking Argus; its
source-ref provenance is an operator assertion, not cryptographic verification.

## Strict plan protocol and identity mapping

```text
Argus-native {ok, kind=chat, reply}
        ↓ strict sentinel + JSON boundary
AGY <AGY_FACTORY_PLAN>{FactoryPlan JSON}</AGY_FACTORY_PLAN>
        ↓ FactoryPlan and registered-mapping validation
PROPOSED imported plan
        ↓ explicit, unchanged revision-bound operator approval
Automaker materialization (separate existing tool)
```

No adequate native complete plan artifact contract was established in MCP. AGY
therefore requests its own envelope. The entire trimmed reply must consist of
exactly one sentinel payload. Prose/markdown fences/extra payloads, malformed or
duplicate-key/non-finite JSON, unknown fields, duplicate tasks/native refs,
empty tasks, missing dependency IDs, self/duplicate/cyclic edges and unsupported
cross-project dependencies fail closed. Nothing is heuristically repaired.

| Source | Factory mapping |
| --- | --- |
| registered native project ID | goal.argus_project_id, checked exactly |
| requested AGY goal/plan ID | goal.goal_id / plan_id, checked exactly |
| logical proposed task ID | task_id; not overloaded as an Argus ID |
| already-known native task ref | argus_task_id retained unchanged; duplicate non-null refs rejected |
| proposed title/description | existing bounded FactoryPlanTask fields |
| operator registry | task workspace_id/repository/project_path, checked exactly |
| logical prerequisite IDs | FactoryDependency; native Automaker feature mapping occurs only after approval |

`/ask` creates no native tasks. Missing native task IDs remain **null**, not fake
IDs. Non-null supplied refs are retained but cannot be independently verified
against all historical tasks through current MCP; do not advertise them as
native task creation evidence. Request/artifact reference identity is distinct
from plan/task identity and titles.

## MCP lifecycle and idempotency

New READ tools: `factory_argus_status`, `factory_argus_plan_preview`,
`factory_argus_reviews`.
New CONTROL tools: `factory_plan_goal`, `factory_import_argus_plan`,
`factory_review_completion`. Existing seven tools/approval semantics are retained.
No new listeners, worker controls or automatic approval/materialization exist.

Planning example:

```json
{"workspace_id":"reports","goal_id":"monthly-report","plan_id":"report-v1","objective":"Propose a bounded synthetic monthly-report improvement plan","request_id":"plan-request-1"}
```

Review/import never overwrite an approved plan. A revision requires a new plan
ID, new revision and new approval. Reimport of the same native project/source ref
and validated plan revision is idempotent; changed payload under that ref, or
trying to reuse an existing plan ID under a different ref, conflicts atomically.
For Manager replies, `request:<request_id>` is an **AGY request reference**, not
claimed an upstream message/artifact ID. Explicit imports can preserve actual
artifact refs where the operator obtains a complete permitted envelope.

Generation request IDs are global, kind-bound and hash-bound to input scope.
Known-project read validation precedes durable intent; a preflight failure can
be retried without a message. After intent is committed, a call is made once.
A completed duplicate returns the compact saved result without contacting Argus.
Incomplete intent—including a crash or a delivered but invalid envelope—blocks
replay with `ARGUS_OUTCOME_UNCERTAIN`; status exposes the request ID. Never
silently replay expensive turns. Inspect Argus through its MCP/status/artifact
surface and have the operator explicitly reconcile/import a recovered result or
choose a new request ID. Status cannot reconstruct missing reply text from
private Argus histories. Import and request-result commits are separate: a crash
between them preserves the imported plan but intentionally blocks message replay.
There is no unsafe reset/retry escape tool. Reads currently make one attempt;
mutations are never automatically retried.

## Evidence and review protocol

```text
FactoryCompletion + FactoryEvidenceRef + bounded sanitized summary
        ↓ separate Manager /ask review turn
<AGY_FACTORY_REVIEW>{FactoryReviewResult JSON}</AGY_FACTORY_REVIEW>
        ↓ strict scope/revision/evidence validation
compact review + normalized FactoryCompletion
```

Evidence input is at most 30 unique references and 4,000 summary characters.
The submission completion's evidence must exactly match the provided refs.
No repository, log history, artifact contents, credentials or private runtime
state is read/fetched automatically. References are sent as references; the
Manager is instructed not to claim artifact inspection from IDs alone. Operator
is responsible for sanitizing allowed text before sending it to their configured
Argus backend. Recognizable private keys, provider/GitHub token shapes and
credential-bearing URLs are rejected before send/import/review persistence with
ARGUS_SENSITIVE_CONTENT; pattern checks do not prove all business data is safe. Caller `completion.accepted` is ignored as a quality verdict.

Review fields: review_id, plan_id/revision, task_id, argus_project_id, explicit
status, bounded summary/gaps/followup, evidence IDs, replan recommendation and
timezone-aware reviewed_at. Accepted+gaps/replan, unknown evidence, duplicate
refs, scope mismatch, malformed output and unknown fields are rejected.

| Reported verdict | Normalized completion.accepted |
| --- | --- |
| ACCEPTED | true |
| MORE_WORK_REQUIRED | false |
| BLOCKED | false |
| ESCALATE | false |

This mapping preserves the externally reported judgment; AGY does not assess
implementation quality or mark Automaker jobs verified. The richer result is
kept separately. Replan flags/gaps/followup are advisory only and create no plan,
approval, job, worker or execution-state mutation.

AGY SQLite stores only its plan import/approval/mapping data, hashed generation
scope + request refs, and compact review results/evidence refs. It stores no
full prompts, replies, conversation histories, native journals, backend credentials,
worker logs or OpenCode sessions. Source envelopes are retained only as validated
AGY plans; raw response transcripts are discarded after validation. APIs return
stable error codes and correlation IDs, not raw MCP/subprocess/server exceptions.
Key errors include ARGUS_NOT_INSTALLED/START_FAILED/TIMEOUT/PROTOCOL_ERROR/
BAD_RESPONSE/PROJECT_NOT_FOUND/PROJECT_MISMATCH/PLAN_INVALID/REVIEW_INVALID/
IMPORT_CONFLICT/REQUEST_CONFLICT/OUTCOME_UNCERTAIN/PROCESS_STATE_UNCERTAIN.

## Manual installation, registration and diagnostics

Existing hybrid installer allowlist now includes factory-local, argus-local,
automaker-local, their launchers and this bridge doctor alongside hybrid-local.
No Argus wheel/runtime/dependency is bundled. Hybrid preflight prerequisites and
its manual-registration defaults are unchanged. For factory-only use, the
checkout launcher plus the existing AGY Python environment is sufficient; no
local OpenCode is required. Use the pinned `requirements-windows.lock` if an
isolated AGY environment needs dependencies (`pip install --require-hashes -r
requirements-windows.lock`); install Argus independently yourself.

Manual fragments only (absolute paths; keep existing servers):

```json
{
  "mcpServers": {
    "factory-local": {
      "command": "<absolute AGY venv python>",
      "args": ["<absolute checkout or release/source>/scripts/factory-local.py"],
      "env": {
        "FACTORY_STATE_PATH": "<absolute user-local path>/factory.sqlite",
        "FACTORY_PROJECT_REGISTRY": "<absolute user-local path>/factory-projects.json",
        "ARGUS_MCP_EXECUTABLE": "<absolute independent Argus environment>/Scripts/argus-plugin-server.exe",
        "AUTOMAKER_BASE_URL": "http://127.0.0.1:3008",
        "AUTOMAKER_API_KEY": "<set locally, never commit>"
      }
    },
    "argus": {
      "command": "<absolute independent Argus environment>/Scripts/argus-plugin-server.exe",
      "args": []
    }
  }
}
```

On Linux the independently installed Argus executable is typically
`<Argus venv>/bin/argus-plugin-server`. The separate `argus` entry is optional
for direct operator interaction; factory-local starts its own owned stdio child
and does not use/adopt another client's process. No real client config is edited.
Keep state/registry outside the checkout. Installed release paths are taken from
our ownership receipt; no discovery of private Argus installations is attempted.

Read-only diagnostic:

```powershell
<AGY-python> scripts/argus-factory-doctor.py --executable "<absolute existing argus-plugin-server.exe>"
```

This calls initialize/tools-list/project-list only, outputs schema compatibility
and project count, and closes its child. Missing executable reports
`ARGUS_NOT_INSTALLED`; it never installs Argus or sends a message. No version
claim is inferred from matching tool schemas.

## Verification and future work

Offline suite uses a fake MCP server/client, including actual stdio processes,
space/Unicode paths, unknown project rejection, missing exe/start errors,
compatibility checks, timeouts/crashes/cancellation, bounded output errors,
owned child cleanup and unrelated process preservation, strict plan/review parsing,
request/import idempotency, persistence, PROPOSED lifecycle and recommendation-only
replan. These are **synthetic tests**, not live Argus validation. Native Windows
and installed Argus backend behavior remain pending.

```bash
.venv-hybrid/bin/python -m pytest -q tests/test_argus_factory_bridge.py tests/test_factory_local.py tests/test_automaker_local.py tests/test_hybrid_local.py tests/test_opencode_capabilities.py tests/test_opencode_resolution.py tests/test_owned_process_shutdown.py tests/test_windows_readiness.py
```

Foreman/Laya branch convergence will supply one assigned execution attempt,
worker supervision and evidence production/handoff. ClickUp will supply verified
human approval provenance later. Neither is a dependency of this branch. Strong
Reviewer/Planner role isolation requires an upstream-supported external MCP path;
this bridge does not fabricate one or bypass the approval boundary to launch it.
