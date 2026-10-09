# Prompt 1 — Local agent: prepare the correct GitHub-ready source repository

You are working on Debian-Devil. This is a **repository preparation and audit** job, not a deployment or push job.

## Objective

Create a new, clean, portable source checkout for `JsonLord/agy-argus-hybrid`, seeded from `/home/leon/antigravity-hybrid` (NOT `agy-modes-current`). The new project is the local MCP bridge between the official AGY cloud Gemini interface, Argus, ClickUp, and workstation OpenCode. It does not contain a replacement AGY Chat/Builder web gateway. Place the attached `spec.md` at the root of the new checkout and treat it as the scope contract.

## Work plan

1. Inspect and report the existing `/home/leon/antigravity-hybrid`: real git root, branches, remotes, source files, dependencies, MCP registration, implemented tools, local supervisor chain, executable scripts, test configuration, documentation and any running processes. Inspect `/home/leon/antigravity-current` only to understand official AGY registration; do not modify it. Inspect `agy-modes-current` only if needed to resolve source provenance; do not use it as the base.
2. Look for hardcoded Nemotron/Gemma endpoints, model flags and environment overrides. Mark these for future replacement with OpenCode-native model inheritance; do not remove functionality destructively in this preparation phase.
3. Create `/home/leon/agy-argus-hybrid` as a **new clean source export**, copying only reviewed, portable source and required test/config templates. Keep the original service, data and home profiles untouched. Do not copy `.git` history until you have inspected it for credentials; if any risk exists, use a clean initial Git history. Never rewrite the original repository's history.
4. Include root `spec.md` supplied with this task, a clean `README.md` with accurate current-state/not-yet-implemented markings, `.gitignore`, `.env.example`, dependency lock/pins if available, license notices, test commands, and a `docs/SOURCE-INVENTORY.md` mapping original source files to exported paths. If the spec file isn't physically accessible, do not invent its text: record the missing input and proceed with a safe export, leaving a clearly marked placeholder.
5. Exclude tokens, API keys, credential stores (`auth.json`), `.env`, `.gemini` private configuration, OpenCode user/session state, company/customer data, screenshots of private systems, generated reports, SQLite DBs, backups, logs, snapshots, huge models, local virtual environments and build caches. Verify also tracked files and prior history. Never print secret values or include them in reports or prompts. When potential secrets are found, report **paths and classes only**, not values.
6. Make the source layout runnable/testable without the Debian-only service environment where practical, but **do not implement the new Argus feature set yet**. Document Windows adaptation items and any source-level blockers. Retain existing MCP tool compatibility where source confirms it.
7. Run appropriate lint, syntax, tests and packaging checks available without production credentials. Use a fake/dry-run MCP config for smoke tests. Do not modify running services, host AGY registration, global OpenCode auth or active user sessions.
8. Initialize or prepare a clean Git repository in the new checkout. Stage and make a local preparation commit only if audits pass; show `git status`, branch, local commit SHA, source files and test results. Set the proposed GitHub remote to `https://github.com/JsonLord/agy-argus-hybrid.git` **only if the repository already exists and is confirmed to be the intended private target**. Otherwise report the next human action to create it privately. Do not push, create public repositories, force push or overwrite existing remotes.

## Required final report

Write `docs/REPOSITORY-PREPARATION-REPORT.md` with:
- source discovery and why `antigravity-hybrid` was chosen;
- what was copied, excluded or restructured;
- original service unchanged verification;
- old supervisor/model coupling locations;
- GitHub security review results (without secret values);
- dependencies/licenses, test/build results;
- readiness status and exact blockers;
- local checkout path, `git status`, commit SHA, proposed remote;
- safe follow-up commands to create a **private** GitHub repo and push, but do **not** run push.

Stop rather than guessing if the original root is different, if sensitive company material cannot be separated, or if Git history is unsafe. Do not claim deployment readiness from preparation tests alone.
