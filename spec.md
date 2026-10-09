# AGY Argus Hybrid — Implementation Specification

**Status:** Proposed implementation contract (not a description of a deployed system)  
**Target repository:** `JsonLord/agy-argus-hybrid` (recommended; create privately)  
**Seed source:** `/home/leon/antigravity-hybrid` on Debian-Devil  
**Target runtime:** Windows work computer with existing OpenCode Desktop, plus optional Linux host  
**Specification date:** 2026-10-09

## 0. Executive decision

Build a **portable local MCP control plane**, not a replacement AGY web app. The official Antigravity/AGY cloud Gemini experience stays the user interface. Existing `hybrid-local` MCP functionality controls local OpenCode. A dedicated **Argus MCP adapter** handles goal decomposition, ClickUp review, approved child projects, and integration. Argus's maintained runtime is reused as a pinned external dependency or upstream fork; do not reimplement its Manager/Planner/Engineer/Reviewer loop.

**Non-negotiable:** OpenCode owns project model/provider/agent selection and credentials. No fixed Nemotron, Gemma, Qwen, or other worker model is configured by this system. The cloud Gemini model is the **external conversation/planning caller** and is not substituted for the local OpenCode-selected worker.

## 1. Desired user workflow

1. User selects a registered local workspace in AGY, then writes `/goal Improve the monthly report of employee hours per month` (or normal-language equivalent).
2. Gemini sees the `argus_*` MCP tools and calls `argus_create_goal` (subject to actual AGY client support; see compatibility gate).
3. Argus inspects the authorized workspace and available read-only business-system tools, writes a **proposed** goal plan, and creates one parent task plus proposed child tasks in the already-connected ClickUp workspace. No implementation yet.
4. User reviews, edits, approves, or rejects individual ClickUp tasks.
5. Approval sync identifies **who approved what**, and freezes the approved revision, scope, workspace and acceptance criteria. Only approved tasks enter the queue.
6. Each approved child task becomes its own bounded Argus project (durable state, engineer session, reviewer, logs, tests, results). Scheduler respects dependencies and per-workspace resource limits.
7. The OpenCode worker runs **inside its registered project directory**, using existing OpenCode configuration, credentials, agents, and the model actually selected for the workspace/session. No `--model` or global provider override is injected for inherited-model tasks.
8. If a task fails, Argus retries within limits; the `hybrid-local` supervisor can inspect and assist using a separate workspace-bound OpenCode session. Ambiguous, unauthorized, irrecoverable, or high-risk cases escalate to the human/cloud Gemini at next interaction.
9. Independently reviewed outputs are integrated in dependency order, then reviewed **again as a whole**. The original project/report is not overwritten without the relevant approval.
10. Gemini can call MCP tools to get current status, events, problems, and final artifacts. Do not promise unsolicited cloud-model updates unless the actual AGY client supports a verified notification transport.

## 2. Architecture

```text
Official AGY client / Web UI (cloud Gemini)
                 |
       local MCP registration
                 |
       +---------+----------+
       |                    |
 hybrid-local MCP       argus MCP adapter
 (execution,           (goals, proposals,
  supervision,           approvals, campaigns,
  diagnostics)           status, artifacts)
       |                    |
       +----------+---------+
                  |
       Workspace registry + policy gate
                  |
       Argus durable campaign runtime
       Manager / Planner / Engineer / Reviewer
                  |
       OpenCode workspace-aware adapter
                  |
    +-------------+-----------------------+
    |                                     |
 Finance workspace                       App workspace
 configured model/agents                 configured model/agents
 own files and sessions                  own files and sessions
```

- **Gemini**: user-facing interaction, tool orchestration, explanations and escalations. It should not be an always-running unattended campaign daemon.
- **Argus**: durable job lifecycle, planning, independent review, child project execution, scheduling and resource budgets.
- **hybrid-local**: workspace-aware local execution, diagnostic escalation, bounded repair and state access. Legacy fixed-model helpers must be decoupled from primary execution.
- **OpenCode**: the source of truth for provider credentials, model selection, agent/skill settings, project context and code execution.
- **ClickUp**: the **human-facing proposal and approval board**; not the authoritative daemon execution log.
- **Local database**: authoritative run/approval ledger, leases, checkpoints, events and evidence pointers. SQLite with WAL is acceptable for one local workstation; one process owns mutations.

## 3. Repository and dependency policy

### 3.1 Starting point

- Export/sanitize portable source from `/home/leon/antigravity-hybrid` into a **new checkout** named `agy-argus-hybrid`. Do not destructively rename or rewrite the running original.
- Preserve useful current `hybrid_run`, `hybrid_status`, supervision and diagnostics semantics **after inventorying real code**. Document any compatibility differences; do not blindly assert that remembered tool names still exist.
- Do **not** use `agy-modes-current` as the runtime foundation. It is a separate custom OpenWebUI/SDK gateway and is outside scope.
- Reference the upstream-maintained Argus runtime, starting from `JsonLord/microsoft-argus`; pin an exact commit/tag and record license, compatibility and version. Avoid copying its full sources except for a justified, license-compliant fork.
- Integrate the current ClickUp MCP connection rather than creating another credentials store or routing company data through another service.

### 3.2 Proposed project layout (adjust after source inventory)

```text
agy-argus-hybrid/
├── spec.md
├── README.md
├── LICENSE-or-NOTICE.md
├── .gitignore
├── .env.example
├── src/
│   ├── mcp/hybrid_local/
│   ├── mcp/argus/
│   ├── adapters/opencode/
│   ├── adapters/argus/
│   ├── adapters/clickup/
│   ├── config/workspaces/
│   ├── policy/
│   ├── campaigns/
│   ├── supervisor/
│   └── persistence/
├── tests/
│   ├── unit/
│   ├── contract/
│   └── integration/
├── scripts/
│   ├── doctor/
│   └── packaging/
└── docs/
    ├── architecture.md
    ├── workstation-install.md
    ├── agy-mcp-registration.md
    ├── opencode-model-inheritance.md
    ├── clickup-approval.md
    ├── threat-model.md
    └── migration-and-rollback.md
```

Do not impose a new programming language or framework unnecessarily. Retain the existing server's supported MCP transport and packaging where practical. Do not introduce a public listener.

## 4. Official AGY compatibility gate (P0)

Verify, rather than assume:

- Is the **specific official AGY Web conversation** able to invoke user-registered **local** MCP tools via the installed AGY client? If yes, provide exact documented registration and a successful live `ping/status` tool trace.
- If not, **do not fork the AGY Web UI** and do not claim `/goal` interception works. Support the closest officially available AGY client/Remote Control/CLI surface that can call the MCP tools, and report the gap clearly.
- `/goal` is an **instruction convention** unless a native AGY slash-command/dispatcher is actually verified. Supply AGY custom/system instructions mapping `/goal ...` to `argus_create_goal` when tool calling is supported.
- MCP must run under explicitly registered local process or existing authenticated private transport. Do not expose new public ports or unauthenticated HTTP endpoints.
- Document behavior when the AGY session closes: background Argus continues locally; Gemini learns about progress only through later MCP status/event calls unless an independently verified notification mechanism exists.

### Proposed AGY instruction template

```text
When the user message begins with /goal, identify the selected, registered workspace and call argus_create_goal with the user's objective. First request planning/proposals only; never execute tasks merely because they were proposed. Tell the user where to review the ClickUp proposals. For approved work, call argus_sync_approvals and argus_start_approved only after authorization checks succeed. Use argus_get_goal / argus_get_events for progress. For ordinary local implementation tasks, use hybrid-local. Never choose an OpenCode model/provider: OpenCode inherits that from its workspace or resumed session.
```

## 5. Workspace isolation and model inheritance

The local workstation owns an explicit **allowlisted workspace registry**. Each workspace has an immutable internal ID, canonical local root path, approved tool capabilities, worktree policy and concurrency budget. Do not accept an arbitrary path from an LLM in place of a registered workspace.

Example **application-defined** registry (not native OpenCode/Argus configuration):

```yaml
workspaces:
  finance-report:
    root: "C:/Work/FinanceReport"
    execution_backend: opencode
    model_policy: opencode_native
    agent_policy: inherit
    max_writers: 1
    git_isolation: worktree_when_supported
    capabilities: [read_report_sources, edit_report_project, run_tests]

  application:
    root: "C:/Work/Application"
    execution_backend: opencode
    model_policy: opencode_native
    agent_policy: inherit
    max_writers: 2
    git_isolation: required_for_parallel_writes
    capabilities: [read_repo, edit_code, run_tests]
```

**Priority and implementation rules:**

1. If resuming a specific OpenCode session, preserve its verified selected provider/model/agent and **same absolute `cwd`**.
2. For a new task, start in the chosen workspace's canonical root, letting OpenCode load its project-local configuration, agents, skills and native model choice.
3. If OpenCode has no project-pinned model, its native fallback/user preference may apply; **do not present this as a guaranteed workspace-specific model**. Report the effective model and origin where obtainable. Support an optional user-approved workspace-specific model pin **in OpenCode itself**, not in Argus.
4. Never silently inject `--model`, a provider ID, `ARGUS_SKILL_MODEL`, `ARGUS_SKILL_<ROLE>_MODEL`, `ARGUS_SKILL_OPENCODE_PROVIDER`, `OPENAI_API_KEY`, or model-specific URLs to bypass inherited configuration. Detect and isolate inherited Argus model/provider overrides; use a clean, per-run environment when necessary.
5. For Argus's Manager/Planner/Engineer/Reviewer roles, default to **OpenCode backend with workspace-native model choice**. Separate sessions, instructions and tool permissions provide role separation. If Argus's current stock CLI cannot guarantee this, implement a narrow, tested adapter or fail with a clear unsupported-mode result. Never falsely report inheritance.
6. OpenCode credentials remain inside OpenCode's normal credential store and existing environment. Do not copy or synchronize them into AGY, Argus, ClickUp, GitHub or campaign logs.
7. When a requested model/provider is unavailable, stop the affected task with a recoverable diagnostic; no covert fallback to another provider.
8. The remote cloud Gemini model is independent and never dictates the local workspace model.

**Transport selection:** Prefer a documented OpenCode ACP session (`cwd` absolute) for new isolated agent sessions. Where useful, support the authenticated local OpenCode server/SDK. Confirm which transport actually interacts with the desktop's current sessions; a new ACP process is **not assumed** to share the user's current GUI session. Preserve user-owned sessions and do not interrupt them. Refer to the installed version's CLI `--help` rather than guessing flags.

**Proof:** tests with **two workspaces configured with distinct models** must show the actual provider/model each task invoked, without override, and with no cross-workspace file writes. If the actual model cannot be observed safely, mark the result *unverified*, not passed.

## 6. MCP tools: initial proposed contract

Tool names and schemas are **to be implemented**, not claimed as existing Argus tools. Use namespaced, discoverable, narrow tools:

| Tool | Role | Required behavior |
|---|---|---|
| `argus_create_goal` | plan | `{workspace_id, objective, ...}`; create draft goal with idempotency key; **no implementation** |
| `argus_get_goal` | status | Read current campaign state, proposal IDs, approvals, budgets |
| `argus_list_proposals` | review | Read task proposals and evidence references |
| `argus_sync_approvals` | authorization | Revalidate ClickUp approval actors, revision hashes, statuses |
| `argus_start_approved` | execution | Dispatch **only approved current-revision** tasks within budget |
| `argus_pause_goal` | control | Stop taking new work; bounded pause/cancel active operations |
| `argus_resume_goal` | control | Resume only valid approved tasks after lease/state checks |
| `argus_get_events` | reporting | Cursor-based event stream; no secrets or raw employee data |
| `argus_get_artifacts` | evidence | Versioned artifact references, hashes, test/review results |
| `hybrid_workspace_status` | diagnostics | Registered workspace, provider/model resolution status, worker health |
| `hybrid_run` | compatibility | Existing bounded local task behavior, made workspace-aware |
| `hybrid_status` | compatibility | Existing status semantics, with explicit migration docs |

Where the existing hybrid tool interface differs, retain a backward-compatible wrapper rather than breaking current use without a deprecation plan. All mutating tools require local capability checks and audit events. Tool responses contain machine-readable error codes and correlation IDs.

**Representative create input:**

```json
{
  "workspace_id": "finance-report",
  "objective": "Improve monthly employee time reporting",
  "planning_only": true,
  "idempotency_key": "client-generated-uuid"
}
```

## 7. ClickUp proposal and approval protocol

- Integrate with the **already-configured ClickUp MCP**. Discover and verify its actual tool set, permission scope and user identity before coding to it. Do not embed access tokens.
- Each goal maps to one parent ClickUp task; proposals map to child tasks, linked to a stable local campaign ID and child ID.
- Proposal content: problem statement with inspected evidence, expected benefit, affected files/data sources, dependencies, tools/skills required, effort estimate, implementation scope, objective acceptance tests, and risks.
- **Proposed is not Approved**. No worker modification before verified approval.
- Approval status is separate from normal task progress. Require an authorized human actor, exact approved proposal **revision/hash**, and immutable approval audit record. If available ClickUp MCP/API cannot establish approval actor reliably, require a signed/local human approval action; **fail closed**.
- On edits after approval, invalidate that task's approval until the changed scope is reapproved.
- Partial approvals are first class. Rejected tasks remain visible but never execute. Dependencies on rejected/unapproved tasks block downstream jobs until replanned and reapproved.
- Make create/update and approval sync **idempotent**; use task identifiers, revision hashes and stored idempotency keys to prevent duplicates when MCP retries/timeouts happen.
- ClickUp is not permitted to hold raw time entries, personal employee data, credentials, query result extracts or internal secrets. Store sanitized summary and local artifact references only.
- Initial version polls ClickUp locally with rate limits/backoff; no public webhook is necessary.

## 8. Child-project execution, scheduler, and integration

Each approved proposal produces an Argus child project with its own `{goal_id, task_id, workspace_id, revision, budget, state_path, opencode_session_ref, artifacts, acceptance_contract}`. An allowed parallel job **is a project**, not merely a prompt.

State machine:

```text
DRAFT -> PROPOSED -> (APPROVED | REJECTED | CHANGES_REQUESTED)
APPROVED -> QUEUED -> RUNNING -> REVIEWING
REVIEWING -> (ACCEPTED | REWORK | BLOCKED)
REWORK -> QUEUED (bounded attempts)
ACCEPTED -> INTEGRATION_READY -> INTEGRATED
INTEGRATED -> CAMPAIGN_VERIFIED
```

Enforce monotonic/audited transitions and explicit failure states. Persist transitions *before* side effects when necessary and reconcile on restart. Lease and heartbeat each active worker; prevent overlapping writes to same files or report artifacts. Treat non-Git deliverables with separate versions/copies.

**Limits**: configurable maximum campaign wall clock (default/example 8 hours after approvals, not before), max attempts per task, max concurrent writers per workspace, cost/token budget if observable, and idle/stall detection. The campaign ends early on verified completion. Human credentials, external publication, production data mutation, permissions changes and other irreversible operations remain separate approval gates.

**Supervisor chain:** retain failure diagnosis/replanning/escalation concepts of `hybrid-local`, but execute supervisor and repair prompts via separate workspace-aware OpenCode session/agent, not fixed Nemotron/Gemma endpoints. Argus Reviewer remains read-only and distinct from the engineer session. If authorization, tool permissions or verified reviewer independence cannot be enforced, halt the child task. Avoid recursive `Argus -> hybrid -> Argus` control loops: define one owner for transitions and a bounded assistance API.

**Integration:** parent integration job accepts only independently approved artifacts, verifies dependency contract and provenance, merges compatible Git branches/patches or non-Git artifact versions in order, executes end-to-end checks, and creates a final verification record. Individual child acceptance is necessary but insufficient for parent completion.

## 9. Evidence and business-data validation

For the employee/month report example:

- Establish baseline report and approved semantics for employee ID, reporting month, time zone, holidays/leave, corrections, paid vs worked hours, rounding, permissions and source-of-truth queries.
- Data source access starts read-only and within corporate policy. Never dump confidential Azure SQL/ClickUp data into a general cloud model, ClickUp tasks, GitHub issues or diagnostic logs.
- Test reconciliation by employee and month, duplicate/missing entries, date boundaries, measure correctness, authorization/row-level security, refresh behavior and report consistency.
- Every child output carries stable artifact hash/version, prompt/task revision, worker/session reference (redacted), actual provider/model if safely observable, tests performed, independent verdict, limitations and timestamps.
- The parent integrates only compatible, current, accepted artifact versions, and records any blocked or incomplete items.

Optional **Loki** provides a second verification layer where applicable; it never overrules deterministic tests or Argus's independent reviewer. Check its license before vendoring or redistributing.

## 10. Optional small-model routing (post-MVP)

Laya may cheaply choose among **preapproved, planner-generated choices** (e.g. which ready job to dispatch, retry category, eligible skill). Needle3 may rank or select a small set of permitted tool calls. Neither grants approval, permissions, workspace access, or a success verdict. Both are disabled by default and require evaluation against a large-model/deterministic baseline, with fail-closed or escalation paths. Never send employee-level or corporate-sensitive inputs to hosted inference without explicit authorization.

## 11. Security and GitHub publishing

- Default GitHub repository **private**. Never assume a public repository is suitable for corporate or user-specific source and configuration.
- Work in a new checkout; **leave the original Debian service running and unchanged**.
- Secret scans of both copied file contents and Git history before creating a remote. If source history includes leaked credentials, do not copy `.git` history; create a clean initial commit from audited source.
- Exclude `.env`, `auth.json`, token stores, `.gemini`/profile private state, `.opencode` user state, secrets, keys, generated reports, local databases, logs, backups, volumes, checkpoints, recordings and corporate source-data extracts.
- Keep credentials OS/user-local. Config templates must use placeholders and separate, untracked local files. Ensure logs redact authorization headers, tokens, private paths where needed, and business records.
- No public ports; use localhost, authenticated OS-local IPC/stdio or an explicitly approved private transport. Restrict path traversal, symlinks and shell injection; all workspace paths are canonical and allowlisted.
- Before push, print sanitized file inventory, test results, dependency licenses, `git status`, exact remote URL and commit SHA. Ask for human approval for creating a remote, pushing or changing the original deployment.

## 12. Milestones and acceptance gates

### P0 — Inventory and portable clean export

- [ ] Identify actual source entrypoints, tool registrations, supervisor behavior, runtime dependencies, host-specific assumptions and running services.
- [ ] Prepare a **new clean repo** with safe `.gitignore`, license notices, README, pinned dependencies, spec and source map; no secrets or volatile state.
- [ ] Run offline/static checks and prove the original deployment unchanged.
- [ ] Verify or document blocker for official AGY client access to local MCP from the targeted UI.

### P1 — Workspace-aware hybrid-local

- [ ] Configure two allowlisted workspaces; demonstrate existing OpenCode auth/config discovery without exposing credentials.
- [ ] Execute/read-only smoke runs in two project contexts using native configured models, recording actual model/provider (or correctly reporting unverified).
- [ ] Verify absolute `cwd`, separate sessions, per-workspace file protection, no implicit provider override.
- [ ] Replace fixed-model supervisor/repair dependency and preserve compatible `hybrid_run`/`hybrid_status` contracts where present.

### P2 — Argus MCP + ClickUp approval

- [ ] Add durable goal, proposal, approval, event and artifact data model.
- [ ] Implement tool calls for `argus_create_goal`, `argus_get_goal`, `argus_list_proposals`, `argus_sync_approvals` and `argus_start_approved`.
- [ ] Demonstrate a ClickUp parent and at least three *proposed* child tasks from a synthetic, non-sensitive report; no execution before approval.
- [ ] Approve two and reject one; verify actor, frozen revisions, idempotency, dependency blocking and zero execution for rejected/changed tasks.

### P3 — Autonomous child projects + recovery

- [ ] Start two approved tasks as independently recoverable Argus child projects, each bound to the correct workspace/OpenCode model and session.
- [ ] Show independent review, bounded retry, supervisor assistance, worker lease recovery, pause/resume and a wall-clock campaign budget.
- [ ] Produce one integration run, full-report verification, final evidence, and MCP-readable status/events after restart.

### P4 — Optional specialists and optimization

- [ ] Add OpenDesign, enterprise-data/Power BI capability adapters after permission review and sandbox tests.
- [ ] Optionally evaluate Laya/Needle3 routing and Loki verification without granting them authorization authority.

## 13. Mandatory automated test matrix

| Test | Required result |
|---|---|
| Unknown workspace/path traversal | Reject before starting process |
| Two different OpenCode project models | Effective models independently observed; no forced global override |
| Resumed OpenCode session in wrong cwd | Reject without mutation |
| Global Argus model env overrides present | Sanitize/override according to inheritance policy; assert effective result |
| ClickUp proposal created | No task execution before valid human approval |
| ClickUp status changed by bot | Not sufficient for human approval |
| Approved task edited afterward | Approval invalidated until reapproved |
| Duplicate approval/polling/event | One child project only |
| Dependency rejected | Downstream task blocked or explicit replanning approval required |
| Child job crash/restart | Lease recovered; no unsafe duplicate writes |
| Reviewer attempts to edit | Denied, audited |
| Two concurrent writers same nonisolated workspace | Serialized/rejected |
| Model unavailable | Task blocked; no silent alternate provider |
| Gemini conversation disconnected | Local campaign persists; later MCP status accurately reflects progress |
| Secrets in staged files or history | CI gate fails; no push |
| Optional local MCP transport unsupported in target AGY UI | Explicit documented blocker; no false claim of success |

## 14. Deliverables and definition of done

Deliver code plus `README.md`, `spec.md`, `.gitignore`, `.env.example`, setup/doctor scripts, Windows work-PC install guide, AGY MCP setup guide, model inheritance guide, ClickUp approval guide, threat model, migration/rollback guide, and automated test output.

Produce `docs/IMPLEMENTATION-REPORT.md` with: (1) actual source inventory; (2) implemented vs deferred spec sections; (3) exact checked commit; (4) successful live MCP calls and provider-inheritance traces; (5) ClickUp approval evidence with personal data redacted; (6) remaining blockers; (7) reproducible install and uninstall/rollback commands.

**MVP done means:** one `/goal`-equivalent invocation through supported AGY MCP tooling -> ClickUp proposals -> explicit user approval of a subset -> two independent Argus/OpenCode workspace-aware child executions -> independent verification -> integrated artifact -> restart-safe MCP status. The command itself and the AGY Web routing are considered proven only if the *actual official client* demonstrates them.

## 15. References to validate against installed versions

- OpenCode official documentation: https://opencode.ai/v2/docs/cli/acp/ and https://docs.opencode.ai/docs/server/ and https://docs.opencode.ai/docs/cli/
- Argus runtime/fork: https://github.com/JsonLord/microsoft-argus and upstream https://github.com/microsoft/ArgusAgent
- Argus provider inheritance details: `docs/backend-providers.md` in pinned Argus revision
- ClickUp API tasks/relationships: https://developer.clickup.com/docs/tasks

All examples in this specification are proposed contracts, not evidence of completed integration or deployed endpoints. Read the actual installed tool versions and local repository before implementation.
