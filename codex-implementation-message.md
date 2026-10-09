# Prompt 2 — Codex: implement AGY Argus Hybrid from spec.md

You are the lead implementation agent in the new `agy-argus-hybrid` repository. Read `spec.md` and `docs/REPOSITORY-PREPARATION-REPORT.md` completely before changing code. Work against the actual source inventory, not remembered file names. Keep the original `/home/leon/antigravity-hybrid` installation and existing work-PC OpenCode Desktop configurations untouched.

## End state

The **official AGY Web/Remote Control cloud Gemini** uses the registered `hybrid-local` MCP server plus a new `argus` MCP server. `/goal` (if officially supported, otherwise documented prompt convention) calls Argus to inspect an authorized project and propose ClickUp tasks. The human approves any subset. Argus starts only approved, revision-locked tasks as independently persisted child projects, dispatches each through the existing OpenCode installation in that workspace with its existing effective model/provider/agent and credentials, supervises bounded recoveries through `hybrid-local`, independently reviews every result, and integrates verified outputs. Gemini retrieves status/events using MCP calls; no fake server-initiated updates.

**Absolutely no fixed worker-model dependency** (Nemotron/Gemma/Qwen), no hardcoded provider or `--model`, no automatic external provider fallback, and no credentials copied from OpenCode. The cloud Gemini caller is not the worker model.

## Required implementation order

**P0: inspect and design.** Confirm MCP entrypoints, actual AGY tool exposure, Argus pinned revision and installed CLI/runner behavior, ClickUp MCP tool availability, and OpenCode Desktop/ACP/server compatibility. Record the verified architecture and explicit blockers. If AGY Web cannot invoke local MCP, preserve official UI and document the supported AGY CLI/Remote Control route; do not fork/build a replacement Web UI.

**P1: workspace-aware OpenCode and hybrid.** Add canonical allowlisted workspace registry, per-workspace session management, native OpenCode model inheritance, observable effective-model diagnostics, role-specific permissions, and compatibility with real existing `hybrid-local` tools. Ensure the hybrid supervisor/repair path invokes a separate authorized OpenCode session/agent instead of Nemotron/Gemma. Explicitly identify and neutralize inherited `ARGUS_SKILL_*_MODEL` / provider overrides, with tests proving no `--model` injection. Preserve CLI credentials and native workspace agent settings; do not rewrite global or project OpenCode configuration.

**P2: Argus MCP and durable state.** Reuse the pinned `JsonLord/microsoft-argus` native Manager/Planner/Engineer/Reviewer engine through a narrow adapter (no copied rewrite), expose the proposed `argus_*` MCP contracts from `spec.md`, add a durable campaign/approval/event store, idempotency keys, auditable state transitions, actor-verified/revision-locked ClickUp approval and safe polling via existing ClickUp MCP tools. No work runs from unapproved proposal states.

**P3: child projects and integration.** Each approved ClickUp task becomes one bounded Argus child project with independently scoped OpenCode session, artifact/evidence registry, reviewer (read-only), retries, lease/time budgets, pause/recovery and dependency-aware scheduling. Use isolated worktrees or non-Git artifact versions. Preserve one control-loop owner: Argus manages lifecycle; hybrid provides bounded assistance. Parent integration runs only on verified current artifacts, performs whole-project acceptance tests, and returns artifacts/events to Gemini on demand.

**P4 (deferred unless P0–P3 pass):** Optional OpenDesign, Power BI, Laya, Needle3 and Loki adapters. Do not add them before the MVP works.

## Acceptance tests: mandatory

Create automated unit/contract/integration tests with fake OpenCode and ClickUp servers first, then safe real local smoke tests. Cover:
- two local OpenCode workspaces with different configured effective models and no model overrides;
- correct absolute cwd and session persistence, no cross-workspace writes;
- ClickUp three proposed tasks, two human-approved, one rejected: only approved tasks execute;
- task mutation after approval invalidates its revision;
- ClickUp bot-initiated state change cannot impersonate human approval;
- task duplicates, failed polling and restart do not create duplicate executions;
- bounded supervisor recovery (no recursive supervisor loops), independent read-only reviewer and false-success prevention;
- two separately verified child outputs integrated and retested into one final artifact;
- 8-hour campaign budget can be configured and stops taking new work when expired;
- no leaked credentials, no new public port, no unapproved production modifications.

When testing cannot run against corporate data/services, use anonymized fixtures and report that live enterprise compatibility remains unverified. Do not fabricate endpoint results or success.

## Progress and commits

Implement by phases with small auditable commits; keep tests green at each phase. After each phase update `docs/IMPLEMENTATION-REPORT.md` with changed files, actual commands/results, acceptance tests, the chosen OpenCode transport and what was verified vs only mocked. Run tests, lints, formatting, dependency/license review and secret scans. Do not modify the original running Debian install, log in to new accounts, change API credentials, alter corporate permissions, create a new remote or push/deploy without a separate explicit approval. If blocked, provide precise next action and stop that workstream, but continue safe independent tasks.

**First deliverable:** P0 + P1 with a functioning MCP status call and verifiable inherited models from two OpenCode workspaces. Then implement P2 and P3. Conclude with a complete report against every `spec.md` acceptance gate and the exact commit SHA.
