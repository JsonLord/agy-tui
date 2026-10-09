# Antigravity control-plane implementation and handoff spec

Last updated: 2026-09-26 21:51 CEST. This is the living summary; update after every meaningful milestone. Overall goal is NOT complete. Primary development direction: commit-based development gatekeeping (see dedicated section below; planning complete 19:15 CEST, implementation pending).

## Goal and acceptance

Hermes acts as coordinator, not primary implementer. It decides when coding delegation is necessary, decomposes objectives into bounded WorkerTasks (normally <=3 declared files), delegates through supervised OpenCode, tracks exact sessions and process/model/tool/repository progress, detects ACTIVE_NO_REPO_PROGRESS, performs bounded task/subtree retry/replan without restarting the whole plan, runs independent deterministic validation, and produces structured results.

## AGY pivot — standalone Helmholtz harness (user direction 2026-09-26 21:00 CEST)

Verbatim user direction: "AGY large-development execution now targets Helmholtz MiMo Pro/MiMo Flash with Needle3 tool routing. Hermes becomes an independent Nemotron/Gemma harness. Gemma repair no longer blocks AGY."

### AGY role split and execution path
- Supervisor/gatekeeper: SUPERVISOR_MODEL=alias-mimo-v2.6-pro (Helmholtz OpenAI-compatible provider) — owns DevelopmentPlan generation, stage decomposition, acceptance criteria, commit gatekeeping, repair decisions, final plan acceptance.
- Dispatch worker: DISPATCH_MODEL=alias-mimo-v2.6-flash — bounded implementation of one stage at a time, local test execution, meaningful stage commits.
- Path: MiMo Pro -> plan/stage -> MiMo Flash -> Needle3 when useful -> tools -> commit -> cumulative deterministic tests -> MiMo Pro gate.

### Needle3 routing contract
- Endpoint https://leon4gr45-needle.hf.space, POST /v1/route. Outcomes: selected | abstain, plus validation errors for invalid tool sets.
- Candidate tool sets <=5. NO MiniCPM/tool-call repair in AGY — validation errors surface as ToolSetRejected, no repair loop.

### Hermes boundary
- Hermes is NOT part of the AGY runtime. AGY is a standalone large-development coding harness.
- Separate Hermes architecture, NOT to be implemented in the AGY milestone: Hermes -> Nemotron supervisor -> Gemma coding subagent -> Needle -> tools -> MiniCPM only as repair/fallback; no Jules runtime dependency in that Hermes path.
- Do not delete useful generic abstractions just because Hermes used them.

### Supervisor lifecycle (target)
- Keep the useful opencode-supervise process/session lifecycle behavior, model-neutral. The old 120s stall policy caused false stalls of legitimate slow generations.
- Target values: stall_seconds=900, repeat_limit=3, max_restarts=1 — configurable via the existing config mechanism (AgySettings.SupervisorLifecycle, env-overridable), not hard-coded.

### Staged development target flow
DevelopmentPlan -> Stage -> dispatch -> stage commit -> cumulative gate -> supervisor ACCEPT/REPAIR -> next stage -> final plan gate.
Each stage carries: stage_id; objective; dependencies; allowed scope/files; implementation/use-case context; acceptance commands; acceptance conditions; cumulative invariants. Worker/model prose is NOT acceptance evidence.
Commit gate decision: ACCEPT | REPAIR | REJECT. Simple local failures become bounded repair work.
Jules is NOT integrated into the first AGY pivot milestone.

### Milestone 1 — COMPLETE
- Exact SHA 59f83a2be757905f90ac5dd295c12cdd72c70c22 on main, 2026-09-26 21:33 CEST (unpushed).
- AgySettings (app/config.py) binds Helmholtz models + NeedleSettings + SupervisorLifecycle, all env-configurable.
- app/providers.py: model-neutral OpenAI-compatible client with named roles (SUPERVISOR/DISPATCH) and injectable HTTP transport; no Hermes/Gemma/MiniCPM assumptions.
- app/integrations/needle.py: Needle3 client behind optional ToolRouter seam (route_or_none), <=5 candidate tools enforced, ToolSetRejected for validation errors, no repair.
- app/orchestration/agy_supervisor.py: run_agy_supervised invokes opencode-supervise with explicit lifecycle values and a required explicit agent (no gemma-local default).
- app/integrations/external_stack.py: run_supervised gained optional pre-launch-validated session binding (default argv unchanged).
- Tests: 39 new deterministic tests (mocked HTTP / faked subprocess) in tests/test_agy_config.py, test_providers.py, test_needle_routing.py, test_agy_supervisor.py. 1498 insertions / 17 deletions across 9 files. Legacy Settings and all pre-existing tests untouched and green. 39 tests green under system python3 and .venv, verified 2026-09-26 21:51 CEST.
- No live large-model run required for milestone 1 (none performed).

### Parallel Gemma issue (record only; not AGY scope)
Gemma is currently UNHEALTHY/UNPROVEN. A separate diagnostic lane isolates the failing boundary of the chain: vLLM -> Headroom :8787 -> tool repair :8790 -> OpenCode gemma-local. Gemma does not gate AGY progress.

## Two distinct Jules roles — user direction 2026-09-26 18:07/18:20 CEST

Do not merge these roles.

### Role A — exact-SHA promotion authority (mandatory, project-level)

feature implementation -> deterministic local gates -> commit/push exact SHA -> Jules independent validation -> FINAL PASS -> eligible for integration. FINAL PASS requires verdict=PASS AND verdict_finalized=true AND terminal=true AND jules_state=COMPLETED. Not discretionary for significant Antigravity feature promotion; combined SHAs get fresh gates and fresh Jules. Never merge to main without authority. This is not an always-call rule for downstream coding sessions. Tracked in jules.md section A.

### Role B — optional Gemma sandbox via MCP (discretionary, runtime)

Gemma decides per bounded task whether isolated execution materially helps; only then does it invoke the Jules MCP capability. Jules is a specialized isolated-execution/delegation capability, NOT a mandatory second reviewer.

Decision contract Gemma evaluates before any call:
sandbox_useful: bool; reason: str; mode: validate_only|validate_and_fix; source_repository; source_revision; objective; allowed_scope; acceptance_commands; permissions; expected_artifacts.
If sandbox_useful=false -> do not call Jules. validate_only must never silently become validate_and_fix. Primary routing criteria: isolation value, environment availability, clean-install value, risk to local host, reproducibility, missing local stack. Repository size alone is NOT a routing signal.

Target use cases: evaluate code absent from the local host; clean-install reproduction (record snapshot/cache reuse); dependency/runtime upgrade rehearsal; bug reproduction with unavailable stack; predeployment readiness (NOT deployment authorization); last predeployment fixes (Gemma/controller still validates); migration rehearsal with disposable/synthetic data only.
Skip for: small local changes; cheap local tests; read-only questions; locally reproducible tasks; hardware Jules cannot access; production credentials/secrets; unfalsifiable production environments.

MCP task contract (minimum): start task (repo, revision, objective, mode, permissions, allowed scope, setup requirements, acceptance commands -> task/session handle, submitted revision, state); poll task (normalized ID, status, phase, progress, pending question, terminal flag); retrieve result (resulting revision, patch/changed files, setup evidence, test/build evidence, exit state, failures, report — structured, not reduced prose when structure exists).

Source availability boundary: Jules only sees what is pushed/published. Gemma must reason about whether the target state exists at a reachable source revision and record the exact source revision supplied; never validate an old remote SHA while believing Jules has local unpushed work.

Sandbox security boundaries: isolation is not trust; no unnecessary secrets or production credentials; arbitrary build scripts are not assumed safe; sandbox success never proves production behavior; no production deployment as part of sandbox validation; sandbox tasks are async delegation jobs, not persistent remote shells; avoid long-running dev servers/watchers in setup. Result acceptance requires verifying the actual checked-out input SHA (reject moving-branch mismatch) and validating Jules' exact output state with deterministic evidence. Gemma decides usefulness; controller enforces scope/permissions/budgets; Hermes promotes/deploys. A Role B PASS never counts as a Role A promotion PASS (jules.md separation).

Implementation: pending behind an optional seam so local execution works without Jules (M6-class milestone). Research/contract record: jules-sandbox-research.md. Tracked in jules.md section B.

Final proofs still required:
1. Actual Hermes coordinator acceptance with fake worker services: coding delegates, read-only does not, zero-diff fake success rejected. Fixture exists before decision, coordinator source writes forbidden after test starts.
2. Deterministic Antigravity E2E with fakes.
3. Live E2E through actual antigravity/app.main:async_main or python3 -m app.main (not internal-function bypass), multi-file calculator requiring >1 WorkerTask, scope/dependency/session/validation/replan invariants enforced.
4. Final Jules validation of exact integrated SHA.

## Safety and operating rules

Use isolated worktrees. No primary coding edits by coordinator; delegate through OpenCode. Never intentionally run unsupervised OpenCode coding. Preserve user work, .serena and unrelated untracked artifacts; never commit secrets or .serena. No public ports/Caddy/Tailscale changes, destructive cleanup, main rewrites, reboot, unrelated installations or credential changes. Gemma concurrency maximum one, and no new Gemma worker while inference is saturated. Quiet stdout is not sufficient evidence of stalled work. Model claims and exit0 are not acceptance.

## Verified progress

### Foundation-v2

Existing integration/runtime-foundation had already merged validated progress; duplicate merge/run avoided.
- Worktree: /home/leon/antigravity-worktrees/runtime-foundation
- Exact local and remote SHA: c7426df1f59261c95264f7b95412f9208bb7c7b3
- Jules run: 2554ba223b6e; remote session13024641715002500900
- Fresh canonical poll: PASS, finalized=true, terminal=true, COMPLETED; persisted poll/state read back.
- Local tests/: 61 passed; compileall and git diff --check passed; clean at verification.
- Important: later independent reproduction found defects in its progress classifier. Jules PASS is historical evidence, not proof those defects do not exist. Fixes need new exact-SHA validation.

### Supervisor and routing investigation

Installed /home/leon/.local/bin/opencode-supervise matches validated source at 324e803b0f2c2a23b3ae09032014c83190ccd60c; SHA256 e6afe2e3b3402c23a680d02f7ae9c76da7883375509e0f9585039f2d978f4518. Self-test and real Nemotron smoke test passed.

Routing repair DEPLOYED 2026-09-26 ~18:17 CEST: installed ~/.local/bin/opencode-worker sha256 c4436141fd555797b84bd2698e8bc5297ce29d2bd796a152aa81e4906b967f91 is byte-identical to the Jules-validated candidate at exact SHA e2dcb612. Previous direct-exec wrapper preserved at ~/.local/bin/backups/opencode-worker.pre-supervised-4ffb6836. Historical OS PID/PPID captures proved supervisor -> opencode run only for bootstrap/candidate jobs; installed-wrapper proof is recorded in the routing milestone below.

Default-profile OpenCode skill was rewritten: supervised wrapper is canonical, direct coding runs prohibited, unsupported flags explicitly rejected, capacity checks and deterministic acceptance required. Other profiles/configurations untouched.

### Persistent evidence

All handoff documents are under /home/leon/antigravity-hybrid/state/autopilot/:
- goal.md, status.md, decisions.md, activity.log, jules.md
- live-progress-corrections.md
- supervisor-lifecycle-corrections.md
- coordinator-acceptance-design.md
- this spec.md

Canonical Jules controller: /home/leon/antigravity-worktrees/canonical-jules-state at294c12089a8a6fcd42b75702245e21c90b66e945. Pre-existing untracked .serena/ and instead preserved. Credential loading uses existing dotenv CLI with ~/.config/antigravity-hybrid/jules.env; no secret values printed.

## Routing task — MILESTONE COMPLETE (deployed, FINAL PASS)

Worktree /home/leon/antigravity-worktrees/supervised-worker-routing
Branch feature/supervised-worker-routing, base324e803b0f2c2a23b3ae09032014c83190ccd60c.
Only implementation files authorized: scripts/opencode-worker and tests/test_worker_routing.py. Untracked .serena/ and graphify-out/ are generated context, not deliverables.

Required wrapper:
- Exclusively exec supervisor; no direct OpenCode/fallback/service management.
- Forward repo/agent/server/prompt/optional unchanged session as array argv.
- Preserve env defaults and CLI precedence; one prompt argument; positional joining and stdin input.
- Explicitly reject --json/--thinking/--continue, invalid/missing values, invalid sessions, nonexistent repo, empty/whitespace-only/unsafe leading-dash tasks.
- Explicit empty positional task never reads stdin as fallback.
- No stdout debug noise; propagate supervisor exit code.
- Deterministic fake-supervisor tests assert exact argv and no invocation on validation failure.

Attempt history:
- alias-code session ses_f23f5589effexZ3l1jQulw9hZL: permission denial/no diff first; resumed attempt timed out but server later wrote partial wrapper. Further resume exposed Bad Gateway retry72. Exact-session abort acknowledged; status map empty; local processes gone. Do NOT treat empty map alone as schema-proven remote terminal state.
- alias-fast session ses_f22a7bdccfferVsblOCEwt6REs: produced code/tests, but two reviews rejected guard weakening and deficient tests. Full suites passed28 then38 tests, yet independent invalid-repo/whitespace-task probes returned0 instead of2. Nothing installed/committed/promoted.
- User approved stronger model. alias-large session ses_f228d0065ffeDJRvDLmcB431S0, process proc_1f159d046ae9, supervisor PID754053. It substantially repaired wrapper and rewrote tests, but supervisor exited76 on REPEATED_TOOL_LOOP while repeatedly editing test file. This is not proof the implementation is bad or good; coordinator must inspect and validate current artifacts.
- At17:52 read-back: remote status map{}, no active task claimed. Source restores repository and positional-whitespace checks; tests contain exact JSON argv/negative marker assertions.
- Milestone17:54: independent full tests/ passed57 tests and43 subtests; bash syntax, compileall and diff-check passed. Reviewed both complete files. Committed ONLY wrapper/tests as e2dcb612e63f7c6a4edfb68cc74349ca088861b9; pushed via ag-git; local SHA equals remote feature/supervised-worker-routing SHA. .serena/ and graphify-out/ remain untracked, excluded.
- [Historical at 17:54 — superseded at 18:15 by the FINAL PASS milestone below.] Canonical Jules run f334a91dbffa, remote session15902857535158960843, validates that exact routing SHA. Fresh API poll read-back at that time: IN_PROGRESS, verdict=null, finalized=false, terminal=false. Next gate then: final Jules result, then deploy exact reviewed binary and prove installed public-wrapper process route.
- Milestone 18:15–18:20 (COMPLETE): fresh canonical Jules poll f334a91dbffa = COMPLETED, verdict=PASS, finalized=true, terminal=true for exact SHA e2dcb612 (initial=current; local=remote branch). Wrapper installed with byte-identical sha256, old wrapper backed up. Negative probes with OPENCODE_SUPERVISOR=/usr/bin/true (nonexistent repo, whitespace-only task, --json, leading-dash task, whitespace-only stdin): all exit 2, supervisor never invoked. Installed-wrapper smoke: supervisor PID876235 (exec of wrapper, parent Hermes598057) -> opencode run PID876460, exit0, ROUTING_SMOKE_OK. Explicit-session smoke: bound to existing ses_f2191f908ffeb5QpcYI9jjiwDs, session record token counters advanced 72,474 -> 145,288 input / 49 -> 103 output (same-session turn confirmed), exit0, SESSION_FORWARD_OK. Both runs: tracked diff unchanged, no residual processes. Public installed-wrapper routing proven; supervisor lifecycle and live-progress defects remain separate open corrections.

## Commit-based development gatekeeping — target architecture and phased implementation plan (user direction 2026-09-26 19:15 CEST)

Status: PLANNED. This pass is planning/documentation only — no code changed. Commit-based gatekeeping becomes the primary development path; the existing exact-SHA Jules promotion (Role A) becomes the sparser Level 3 boundary check below. Do not treat "all stages committed" as plan acceptance.

### Control structure (target)

Cloud planner -> DevelopmentPlan -> ordered development stages -> Nemotron supervisor -> Gemma implements current stage -> meaningful stage commit -> Nemotron cumulative deterministic commit gate -> decision: LOCAL_ACCEPT | LOCAL_REPAIR | JULES_VALIDATE | JULES_VALIDATE_AND_FIX | REJECT.

Jules is NOT automatically run after every commit. LOCAL_ACCEPT is the common path.

### Role contracts

- Cloud planner defines what "done" means; never implements code. The DevelopmentPlan contains PLAN-LEVEL acceptance criteria (set before implementation begins) and ordered stages. Each stage carries: stage_id; objective; dependencies; allowed scope/files; expected behavior; use cases; deterministic acceptance commands; cumulative invariants; expected artifacts; risk; isolation requirements; jules_policy (never | on_failure | recommended | required); acceptance criteria.
- Gemma (coding worker): receives one bounded stage at a time; inspects code; implements; adds/updates tests; runs local verification; produces one meaningful stage commit per accepted stage (bounded repair commits allowed; avoid artificial tiny commits). Gemma never declares its own commit accepted. Gemma may RECOMMEND Jules sandbox use (recorded, non-binding).
- Nemotron (supervisory commit gatekeeper): for every stage commit evaluates DevelopmentPlan, current stage, parent SHA, commit SHA, diff, changed files, allowed scope, local test evidence, cumulative tests, previous accepted stages, plan invariants, risk/isolation characteristics, then decides among the five gate decisions. Worker/model prose is never authoritative evidence. LOCAL_REPAIR = bounded repair task for Gemma in the same stage lineage (ordinary deterministic local test failures do NOT go to Jules unless sandbox isolation materially helps); the cumulative gate re-runs after every repair commit.

### Three validation levels (maps to jules.md ledger)

- LEVEL 1 — per-commit cumulative local gate: ALWAYS. Not a Jules role.
- LEVEL 2 — Jules sandbox validation/fix (validate_only | validate_and_fix): OPTIONAL, Nemotron-selected per stage jules_policy and isolation value. = jules.md section B.
- LEVEL 3 — independent exact-SHA Jules promotion: SPARSE, only at major integration/release boundaries (major integration branch, high-risk architecture transition, release candidate, major runtime/controller change, final Antigravity E2E state). = jules.md section A.
Level 2 and Level 3 stay clearly separate in state and reporting. A Level 2 sandbox PASS never counts as a Level 3 promotion PASS. Repository size alone is never a sandbox trigger.

### Cumulative deterministic commit gate

Per meaningful stage commit, the gate intelligently combines: relevant repository regressions; tests from all previous accepted stages; current-stage tests; stage-specific acceptance commands; applicable architecture/contract checks; compile/import/static checks; changed-file scope checks. Not the entire slowest suite every time — but previously accepted behavior must never silently disappear from validation. Meaningful stage commits require a nonempty meaningful diff (zero-diff implementation attempts are not successes — existing rule, now enforced at the gate).

### Commit gate records

Persist per meaningful stage commit at state/runs/<plan_id>/commits/<sha>.json with fields: plan_id, stage_id, commit_sha, parent_sha, changed_files, scope_ok, local_gate {status, commands, results}, gemma_sandbox_recommendation {useful, reason}, nemotron_decision, jules {invoked, mode, task_id, result}, final_commit_gate. Final commit-gate states: LOCAL_ACCEPTED | JULES_VALIDATED | JULES_FIX_ACCEPTED | REJECTED | BLOCKED.

### Level 2 Jules sandbox rules (summary; full contract jules-sandbox-research.md)

Modes kept strict and separate: validate_only must not modify code; validate_and_fix makes bounded changes only when explicitly permitted. A Jules fix is never auto-trusted: Jules patch/revision -> Gemma technical inspection -> Nemotron diff/evidence review -> apply/cherry-pick candidate -> local cumulative gate -> ACCEPT/REJECT. Jules fix rules: may reproduce failure, change permitted scope, add repair-demonstrating tests, run acceptance commands, return revision/patch/evidence; must not weaken/remove tests to pass, redesign unrelated architecture, silently expand scope (report instead), expose secrets, deploy, or treat sandbox success as production proof. Context bundle per invocation: repository, exact accessible revision/SHA (unpushed local changes are NOT visible to Jules — always verify the exact revision Jules receives), goal, spec, DevelopmentPlan, current stage, acceptance criteria, use cases, allowed files/scope, relevant diff, local failure evidence, test commands, expected behavior, requested mode. No unrelated history.

### Plan-level acceptance (PLAN_ACCEPTED)

After the final stage commit, Nemotron evaluates the cloud planner's original plan-level acceptance criteria: full relevant regression/integration suite; architecture contract checks; required use cases; final Git/scope review; final expected artifact checks. Only then mark PLAN_ACCEPTED (persisted in state/runs/<plan_id>/plan.json). If final validation fails: locate the responsible subsystem/stage, create a bounded repair stage, run the cumulative gate again — never restart the whole DevelopmentPlan.

### Model dispatch policy (user requirement 2026-09-26)

Whenever Hermes dispatches a supervised coding session or runs REAL tests/acceptance runs through Hermes, the supervisor (Nemotron gatekeeper) runs on model nemotron-30b — the local model. Do not substitute a different supervisor/gatekeeper model route without user approval. (User wording: "when setting supervised coding session via hermes, the supervisor should be model nemotron-30b"; "for real tests via hermes, we want to dispatch the nemotron-30b model (local model)" — both recorded: the nemotron-30b route binds the supervisor role in real supervised dispatches and real Hermes test runs.)

SUPERSEDED FOR AGY (2026-09-26 21:00): the AGY supervisor/gatekeeper runs on alias-mimo-v2.6-pro and dispatch on alias-mimo-v2.6-flash (Helmholtz), configurable via AgySettings. The nemotron-30b local-route policy applies to the SEPARATE Hermes Nemotron/Gemma harness, not to AGY.

### Gap map — actual code at 2026-09-26 19:15 CEST

Already exists:
- app/schemas.py: WorkPlan (main @ 54ec4f0). Worktree bounded-worker-tasks @ 94c49fc (Jules c5a189aeb35d PASS) additionally has TaskVerification (deterministic command + expected exit code) and WorkerTask (bounded: <=3 repo-relative files, 1-5 steps, one verification command, dependency IDs) plus app/orchestration/decompose.py (topological_order, parse_and_validate_tasks, fallback_decomposition). Integration into main still pending — this is the base layer for stage schema work.
- app/agents/planner.py: plan-level prompt + strict JSON extraction (no stage structure).
- app/validation/runner.py validate(): runs tests + git diff --check but does NOT require a nonempty diff (known gap; the commit gate must add this).
- app/orchestration/loop.py: legacy plan/execute loop + state/runs/<run_id> persistence (run_meta.json superset normalization still open from goal.md priority 7).
- app/orchestration/jules_modes.py (mode definitions), jules_poll.py (untracked, verdict-parser work in progress), prepare_jules.py (readiness/preparation), app/integrations/jules_api.py (REST client) — all Level 3 (promotion) plumbing.
Missing (work to schedule): DevelopmentPlan/PlanStage schema with per-stage acceptance criteria; per-stage jules_policy enum; Nemotron commit-gate decision enum + gatekeeper step; cumulative test selection; CommitGateRecord schema + persistence under state/runs/<plan_id>/commits/; sandbox recommendation/decision recording; Level 2 Jules MCP sandbox adapter (optional seam; local execution keeps working without Jules) with sandbox/promotion record separation; plan-level acceptance state machine; nemotron-30b supervisor dispatch binding.

### Phased implementation plan (smallest coherent seam first; isolated worktrees; one Gemma worker max)

- M0 (AGY, DONE 59f83a2) — Helmholtz model seam + Needle routing + neutral supervisor lifecycle on main. AGY P1-P6 below now execute under AGY models (Pro supervisor / Flash dispatch / Needle routing); the old nemotron-30b supervisor binding in P4 is superseded for AGY per the pivot; Jules sandbox (P5) is deferred out of the first AGY milestone and stays optional.
- P1 — Plan/stage schema. Add DevelopmentPlan + PlanStage pydantic models (all stage fields incl. jules_policy enum, TaskVerification-style acceptance commands, plan-level acceptance criteria block) with deterministic parse/validation (never trust model output); land together with integration of the validated 94c49fc WorkerTask/decompose primitives. Acceptance: schema/enum/domain validation tests, dependency + scope field rules, topology check reuse. No legacy-loop behavior change.
- P2 — Commit-gate evidence model. app/orchestration/commit_gate.py: decision enum (LOCAL_ACCEPT | LOCAL_REPAIR | JULES_VALIDATE | JULES_VALIDATE_AND_FIX | REJECT), final states (LOCAL_ACCEPTED | JULES_VALIDATED | JULES_FIX_ACCEPTED | REJECTED | BLOCKED), CommitGateRecord matching the documented JSON shape (incl. gemma_sandbox_recommendation and jules blocks); atomic persistence to state/runs/<plan_id>/commits/<sha>.json with read-back. Acceptance: round-trip + illegal-state tests.
- P3 — Cumulative gate assembly/execution. Deterministic command selection = previous accepted stages' tests + current-stage tests + stage acceptance commands + compile/import/static checks + changed-file scope check (diff vs stage allowed scope) + git diff --check + required nonempty meaningful diff. Structured per-command results into CommitGateRecord.local_gate. Acceptance: fixture plans proving cumulative union holds, previously accepted tests are not dropped, out-of-scope file changes flagged, zero-diff rejected.
- P4 — Nemotron gatekeeper decision step. Deterministic rule layer mapping gate evidence to the five decisions (clear local failures -> LOCAL_REPAIR; scope/invariant violations -> REJECT with bounded repair path; isolation materially helpful -> JULES_VALIDATE(_AND_FIX) per stage jules_policy; otherwise LOCAL_ACCEPT — the common path), evaluated with the nemotron-30b (local) supervisor route per model dispatch policy; Gemma's sandbox recommendation recorded but non-binding. Acceptance: decision-matrix tests with recorded provider/model identity and tool action trace.
- P5 — Jules MCP sandbox seam (Level 2), OPTIONAL behind an interface (local execution must keep working without Jules): start/poll/result contract per jules-sandbox-research.md; strict validate_only vs validate_and_fix; Jules-fix return flow (inspection -> review -> cherry-pick -> local cumulative gate); sandbox records under state/runs/<plan_id>/jules-sandbox/, never mixed with Level 3 promotion records. Acceptance: mocked-HTTP tests incl. source-revision mismatch rejection, no auto-fix escalation, review-flow enforcement, no unauthorized deployment/PR.
- P6 — Plan-level acceptance state machine. After the final stage: plan-level commands + invariants + use cases + artifact/git/scope checks -> PLAN_ACCEPTED or a bounded repair stage; persist status in state/runs/<plan_id>/plan.json. Acceptance: end-to-end fixture with fake workers demonstrating a LOCAL_REPAIR loop and PLAN_ACCEPTED without whole-plan restart.
Prerequisite note: P1 should land the 94c49fc primitives (currently FINAL PASS but unintegrated) rather than re-implementing WorkerTask.

### Preservation requirements (non-regression while implementing commit gating)

live-progress-corrections.md, supervisor-lifecycle-corrections.md and coordinator-acceptance-design.md remain in force: deterministic evidence over worker prose; zero-diff not success; quiet stdout alone is not a stall; ACTIVE_PROGRESS | INFERENCE_WAIT | ACTIVE_NO_REPO_PROGRESS | IDLE | STALLED | TERMINAL taxonomy; bounded retry/replan (never whole-plan restart); explicit session identity; supervised coding-worker pathway (no uncontrolled direct OpenCode launches); dependency/readiness checks before execution. Gemma resource policy unchanged: MAX CONCURRENT GEMMA CODING WORKERS = 1 (unless newer status.md evidence supersedes).

## Known blockers and corrective specifications

### Supervisor lifecycle

Local client termination does not guarantee remote attached session cancellation. Cleanup tracks PID but not server+directory+session. Quiet stdout causes false stall classification; repeated edits can trigger a loop guard despite meaningful corrective work. A remote abort can yield client exit0, which must remain classified cancellation, not success. OBSERVED 2026-09-26 18:35 CEST: both sessions of a stalled wrapper-default run still reported busy after local exit (aborted via documented API; {} read-back is acknowledgement only), and the 120s default produced false stalls twice with zero implementation output — the public wrapper cannot currently forward supervisor tuning flags (future bounded follow-up, separate SHA).

Minimal follow-up: bounded idempotent abort-exact-session -> verify remote stop -> reap local client; always reap on HTTP errors; prohibit recovery if REMOTE_STOP_UNCONFIRMED. Live API has abort boolean and status idle/busy/retry; absent-key semantics need implementation confirmation before terminal claims. Add mocked lifecycle tests. Keep larger structured-progress work separate. See supervisor-lifecycle-corrections.md.

### Live-progress integration

Worktree live-progress-integration at762239c; pre-existing dirty implementation preserved. Reviewer reproduced73 passing tests, but found no-repo-progress baseline error, tool-only TypeError, startup observation extending execution deadline, lingering monitor thread, scanner deadline/output errors, EOF busy loop, unintended HTTP in tests and malformed nested API parsing. Parent independently reproduced classifier faults on foundation-v2. Do not promote current dirty state. Bounded repair tasks and regressions in live-progress-corrections.md.

### Bounded runtime and coordinator proof

Primitives at94c49fc8a6a40cca5d1a125e3b961a253f486f3e are Jules valid; production integration remains pending. Existing actual CLI path still invokes legacy plan/execute flow. No actual Hermes fake acceptance harness found; scripted routing tests cannot substitute for Hermes decision-loop proof. See coordinator-acceptance-design.md.

## Active jobs at latest milestone (2026-09-26 18:40 CEST)

- 2026-09-26 21:33 CEST: AGY pivot milestone 1 committed on main as 59f83a2 (unpushed); 39/39 tests green (system python3 + .venv); no live model run; state files updated 21:51.
- Routing milestone COMPLETE: Jules FINAL PASS + wrapper deployed + negative probes + installed-wrapper smokes (above). No routing job active.
- progress-baseline-fix LANE ADVANCED: review deleg_f3d3cde6 returned ACCEPT (all 7 contract items proven; cosmetic nits only: one PEP8 blank line, untested non-list `content` guard, untested non-str `outputPaths` drop). Coordinator gates re-run at commit: unittest 59 OK, pytest 70 passed, compileall OK, diff-check OK. Committed ONLY app/integrations/opencode_progress.py + tests/test_opencode_progress.py as dce0816df4f690c469f765033feb931bcb464b25 on feature/progress-baseline-fix (base c7426df); ag-git push created origin branch; remote=local SHA verified; .serena/ and graphify-out/ excluded. Role A Jules run 3308b949dfb3 / session 10977189719125732697 for starting_sha dce0816 reached FINAL PASS at 18:38 (COMPLETED, verdict=PASS, finalized=true, terminal=true; report activity fcf57726f95f44d6aa7ebd55ef2cf047). Promotion gate SATISFIED; NOT yet integrated — integration deferred so lanes A+B land as one combined SHA requiring one fresh gates+Jules cycle.
- supervisor-lifecycle-fix WORKER ACTIVE (attempt 3, proc_40fa845ec895): attempt 1 (wrapper defaults) exited 76 ZERO-diff on false 120s stalls; attempt 2 (stall-safe continuation proc_1bf77a38ec0e) exited 0 but ZERO-diff — investigation-only (binary strings, external GitHub curl; final external read auto-rejected) — REJECTED as fake success; worker prose never accepted. Both attempts left the tree at base 324e803 (suite 20 OK, self-test PASS). Cleanup observation: after attempt 2's healthy exit, status map {} and no local processes — supervisor healthy-path cleanup works; earlier stall-path cleanup gap evidenced (both sessions busy, aborted via API). DISCOVERED GAP: the public wrapper cannot forward --stall-seconds/--repeat-limit/--max-restarts (future bounded wrapper follow-up, separate SHA). Attempt 3 = prescriptive: API routes supplied in-task, external network banned, implementation-first, enumerated mocked-HTTP regressions; stall-safe flags; scope identical (scripts/opencode-supervise + tests/test_supervise_session_binding.py); worker gates + independent review required before any commit.

## Next milestones

0. Commit-based development gatekeeping under the AGY pivot (primary direction, user 2026-09-26 21:00): AGY is now the standalone Helmholtz harness — smallest coherent seam = DevelopmentPlan/PlanStage schema + stage dispatch on alias-mimo-v2.6-flash with cumulative deterministic gate and alias-mimo-v2.6-pro gatekeeper (P1, including integration of validated 94c49fc WorkerTask/decompose primitives), then P2 commit-gate records, P3 cumulative gate, P4 gatekeeper decision step (nemotron-30b supervisor binding superseded for AGY per pivot), P5 optional Jules sandbox seam (Jules stays out of the first AGY milestone), P6 plan-level acceptance. Real tests via Hermes dispatch on nemotron-30b (local) per the model dispatch policy — that route belongs to the separate Hermes harness, not AGY. Outstanding lanes below remain required and are not superseded.
1. integrated promotion (deferred): baseline-fix dce0816 holds FINAL PASS (3308b949dfb3, 18:38 COMPLETED/PASS/finalized/terminal). Hold until lane B lands; then integrate lanes A+B into integration/runtime-foundation as one combined SHA with fresh local gates + fresh Role A Jules before any further promotion. Never merge to main without authority.
2. supervisor-lifecycle attempt 3 proc_40fa845ec895 (prescriptive, stall-safe, network-banned): on exit run independent full gates + read-only review; REJECT on any zero diff; commit/push/Role A Jules only after ACCEPT. Contract: supervisor-lifecycle-corrections.md.
3. Live-progress lane B (external_stack.py + tests) bounded repair per live-progress-corrections.md; lane A outcome is the baseline-fix above.
4. Bounded runtime orchestration preserving legacy mode; deterministic dependency/scope/replan tests (primitives already Jules-validated at 94c49fc; integration pending).
5. Coordinator fake acceptance harness running the actual Hermes decision loop (coordinator-acceptance-design.md), then integrated fake E2E through real app.main.
6. Jules MCP sandbox seam (Role B): start/poll/result tools + Gemma decision-contract tests (sandbox_useful routing, validate_only vs validate_and_fix separation, source-availability blocking, evidence gates, bounded async polling, no unauthorized deployment) behind an OPTIONAL interface — local execution must keep working without Jules. Does not block unrelated validated integration.
7. run_meta.json superset normalization, live coordinator E2E, full antigravity E2E, final Role A Jules of the exact integrated SHA.
8. Gemma diagnostic lane (separate, non-blocking for AGY): isolate failing boundary of vLLM -> Headroom :8787 -> tool repair :8790 -> OpenCode gemma-local; Gemma is UNHEALTHY/UNPROVEN and must not gate AGY progress.

No overall completion claimed. Update this file with exact commands/results, SHA/run IDs, acceptance decisions and active job/session changes after each milestone.
