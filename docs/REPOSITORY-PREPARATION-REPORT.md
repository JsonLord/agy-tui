# Repository Preparation & Architecture Export Report: agy-argus-hybrid

> **Date:** October 9, 2026  
> **Source Repository:** `/home/leon/antigravity-hybrid`  
> **Target Repository:** `JsonLord/agy-argus-hybrid` (Private GitHub Target)  
> **Prepared Branch:** `hybrid-optimised`  

---

## 1. Executive Summary & Mission

This report documents the preparation, architecture refactoring, security sanitization, and export of the `agy-argus-hybrid` codebase on branch `hybrid-optimised`. 

The primary objective of this release preparation is to transition from the legacy fixed-model Nemotron/Gemma local harness to a portable, multi-agent control-plane capable of orchestrating:
1. **Official AGY Cloud Gemini Interface** (`gemini-2.5-pro` supervisory gatekeeper, `gemini-2.5-flash` dispatch worker).
2. **Existing Hybrid-Local MCP Server** (`mcp_gateway.py` & `app/orchestration/checkpoint_planning/mcp_tools.py`).
3. **Argus MCP Adapter** (`app/orchestration/sentinel/adapter.py`).
4. **ClickUp MCP Integration**.
5. **Workstation OpenCode Installation** with native per-workspace model and provider selection (`anthropic`, `openai`, `ollama`, `gemini`).

All changes have been performed cleanly on branch `hybrid-optimised` without modifying or disrupting the host Antigravity environment.

---

## 2. Architecture & Source Inventory

```
agy-argus-hybrid/
├── app/                                  # Core control-plane logic
│   ├── agents/                           # Planner & worker agent definitions
│   ├── config.py                         # Env-configurable settings (AgySettings)
│   ├── context/                          # Repository context & graph management
│   ├── health.py                         # Secret-free health check payload
│   ├── human_surface.py                  # Local human approval interfaces
│   ├── integrations/                     # External stack, Needle router & Jules API
│   │   ├── external_stack.py
│   │   ├── jules_api.py
│   │   └── needle.py                     # Needle3 tool router client
│   ├── investigation/                    # Forensic graph analysis
│   ├── main.py                           # Application entrypoint & CLI handler
│   ├── orchestration/                    # Multi-agent dispatch, gatekeeping & planning
│   │   ├── agy_commitgate.py            # Commit-based development gatekeeper
│   │   ├── agy_dispatch.py              # Staged development task dispatcher
│   │   ├── agy_execute.py               # Deterministic execution runner
│   │   ├── agy_gatekeeper.py            # Supervisor acceptance/repair evaluator
│   │   ├── agy_plan.py                  # DevelopmentPlan generator
│   │   ├── agy_supervisor.py            # OpenCode supervisor process binding
│   │   ├── checkpoint_planning/         # Checkpoint gating & MCP tool handlers
│   │   ├── cloud_batch/                 # Cloud batch planner & human bridge
│   │   └── sentinel/                    # Argus event monitoring & sentinel daemon
│   ├── providers.py                      # Model-neutral OpenAI/Gemini REST provider
│   ├── release_info.py                  # Build & release SHA tracking
│   ├── runtime_config.py                # Repo-relative path resolution
│   ├── schemas.py                       # Pydantic schemas (DevelopmentPlan, WorkPlan)
│   └── validation/                      # Deterministic test & diff runner
├── container/                            # Docker containerization & OpenCode templates
│   ├── Dockerfile
│   ├── opencode.template.jsonc           # Redacted config template for OpenCode
│   └── space-entrypoint.sh
├── deploy/                               # Systemd service units
│   └── sentinel/agy-sentinel.service
├── docs/                                 # Architectural specifications & reports
│   └── REPOSITORY-PREPARATION-REPORT.md
├── scripts/                              # Operational & supervisor scripts
│   ├── agy-hybrid-release
│   ├── entrypoint.sh                     # Portable container boot script
│   └── opencode-supervise                # Supervised wrapper binary
├── tests/                                # Reproducible test suite (800+ assertions)
├── .env.example                          # Environment template
├── .gitignore                            # Exclusion patterns
├── LICENSE                               # Apache License 2.0
├── README.md                             # Comprehensive project overview & setup guide
├── mcp_gateway.py                        # MCP Server entrypoint
├── requirements.txt                      # Python dependencies
└── spec.md                               # Living control-plane specification
```

---

## 3. Decoupling of Legacy Nemotron/Gemma Models

The original implementation contained hardcoded assumptions coupling local execution to `nemotron-30b` (supervisor) and `gemma-local` (worker).

### Refactoring & Decoupling Map:
1. **Model Neutrality (`app/providers.py`):**
   * Replaced hardcoded local endpoints with generic, model-neutral `OpenAICompatibleProvider` supporting arbitrary base URLs, custom headers, and API keys.
   * Defined named roles: `SUPERVISOR` (`AGY_SUPERVISOR_MODEL`, default `gemini-2.5-pro` or `alias-mimo-v2.6-pro`) and `DISPATCH` (`AGY_DISPATCH_MODEL`, default `gemini-2.5-flash` or `alias-mimo-v2.6-flash`).

2. **Supervisor Lifecycle (`app/orchestration/agy_supervisor.py` & `app/config.py`):**
   * Abstracted stall timeout, repeat limits, and max restarts into `AgySettings.SupervisorLifecycle`.
   * Enforced explicit agent parameterization (`agent_name` required; legacy `gemma-local` default removed).

3. **Workstation OpenCode Per-Workspace Selection (`container/opencode.template.jsonc` & `scripts/entrypoint.sh`):**
   * Created a template parameterizing provider and model choices (`OPENCODE_PROVIDER`, `OPENCODE_MODEL`).
   * Absent credentials automatically disable the respective provider rather than throwing hard crashes.

---

## 4. Audit of Retained, Sanitized, and Excluded Components

| Category | Component / File | Status | Rationale |
| :--- | :--- | :--- | :--- |
| **Retained** | Core Application (`app/`) | ✅ Retained | Clean control-plane architecture & schemas |
| **Retained** | MCP Server (`mcp_gateway.py`) | ✅ Retained | Provides codebase analysis & checkpoint tools |
| **Retained** | Test Suite (`tests/`) | ✅ Retained | 800+ deterministic unit & integration tests |
| **Retained** | Container Setup (`container/`) | ✅ Retained | Portable Dockerfile & space entrypoints |
| **Sanitized** | Host Paths in Scripts & Tests | 🧹 Sanitized | Replaced `/home/leon/...` with `runtime_config` |
| **Sanitized** | OpenCode Template | 🧹 Sanitized | Replaced raw API keys with `__ABSENT__` placeholders |
| **Excluded** | `.env`, `.env.pre-repair-proxy` | ❌ Excluded | Secrets & machine-specific keys |
| **Excluded** | `state/`, `logs/` | ❌ Excluded | Runtime logs, run state, and autopilot session data |
| **Excluded** | `.serena/`, `graphify-out/` | ❌ Excluded | Local index files and temporary context graphs |
| **Excluded** | `recovery/investigator/*.dump` | ❌ Excluded | Local forensic memory dumps |
| **Excluded** | `.pytest_cache/`, `.venv/` | ❌ Excluded | Virtualenv & test cache binaries |

---

## 5. Security & Secret Audit Results

A full recursive regex scan was conducted across all tracked files to ensure zero credentials or secret tokens exist in git tracking or commit history.

### Audit Checklist:
- [x] No `GEMINI_API_KEY`, `HELMHOLTZ_API_KEY`, or `OPENAI_API_KEY` literals.
- [x] No GitHub Personal Access Tokens (`ghp_*`, `github_pat_*`).
- [x] No private SSH keys or bearer tokens in code or tests.
- [x] All test fixtures use synthetic mock keys (e.g. `"test-injected-not-a-real-key"`).
- [x] Health check payload verified secret-free (`app/health.py`).

---

## 6. Windows & Cross-Platform Deployment Notes

1. **WSL2 Compatibility (Linux on Windows):**
   * Full support under WSL2 (Ubuntu 22.04 / 24.04).
   * File path utilities in `app/runtime_config.py` use `pathlib.Path.resolve()` to seamlessly handle POSIX and Windows mount paths (`/mnt/c/...`).

2. **Native Windows Execution:**
   * Python 3.11+ supported natively.
   * `OPENCODE_SERVER_URL` defaults to `http://127.0.0.1:4096`.
   * Environment variables can be passed via standard Windows System Settings or `.env` file loading.

---

## 7. Test & Validation Results

The full test suite was executed locally using Python 3.13.5.

```bash
============================= test session starts ==============================
platform linux -- Python 3.13.5, pytest-9.1.1, pluggy-1.6.0
rootdir: /home/leon/antigravity-hybrid
plugins: playwright-0.8.0, anyio-4.14.0, base-url-2.1.0
collected 800 items

tests/test_agy_commitgate.py .......                                     [  0%]
tests/test_agy_config.py ................                                [  2%]
...
tests/test_agy_p20_portability.py .........s                            [100%]

======================== 799 passed, 1 skipped in 24.12s ========================
```

* **Total Test Items:** 800
* **Pass Rate:** 100% (799 passed, 1 container smoke test skipped due to non-running Docker daemon)
* **Execution Time:** ~24 seconds

---

## 8. Branch, SHA & Remote Configuration

* **Branch:** `hybrid-optimised`
* **Local Workspace:** `/home/leon/antigravity-hybrid`
* **Target Remote Repository:** `https://github.com/JsonLord/agy-argus-hybrid.git`
* **Remote Alias:** `jsonlord`

### Next Steps for Codex / Remote Push Protocol

When remote outbound network access to GitHub is active, execute the following command to push `hybrid-optimised` to the private target repository:

```bash
git push jsonlord hybrid-optimised
```
