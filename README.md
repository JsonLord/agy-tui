# agy-argus-hybrid

> **Portable Control-Plane & Multi-Agent Orchestration Engine for AGY and Argus Architecture**

`agy-argus-hybrid` is a clean, modular control-plane and orchestration engine designed to run multi-agent development workflows across cloud model providers (Gemini 2.5 Pro/Flash, Helmholtz MiMo), local OpenCode workers, and Model Context Protocol (MCP) integrations.

---

## 🏛 Architecture Overview

```
                      +-----------------------------+
                      |   Official AGY Cloud Gemini  |
                      |    (Gemini 2.5 Pro/Flash)   |
                      +--------------+--------------+
                                     |
                                     v
                       +-------------+-------------+
                       |    AGY Supervisor Core    |
                       | (Plan, Gate, Replan, Exec)|
                       +------+-------------+------+
                              |             |
         +--------------------+             +--------------------+
         |                                                       |
         v                                                       v
+--------+--------+                                     +--------+--------+
| Argus MCP       |                                     | OpenCode Worker |
| Adapter         |                                     | (Per-workspace) |
+--------+--------+                                     +--------+--------+
         |                                                       |
         v                                                       v
+--------+--------+    +------------------+             +--------+--------+
| ClickUp MCP     |    | Needle3 Tool     |             | Hybrid-Local   |
| Integration     |    | Router           |             | MCP Server     |
+-----------------+    +------------------+             +-----------------+
```

### Key Components

1. **Official AGY Cloud Gemini Interface:** Uses Google Gemini models (`gemini-2.5-pro` as supervisory gatekeeper, `gemini-2.5-flash` as dispatch worker) via model-neutral OpenAI-compatible / Gemini REST providers (`app/providers.py`).
2. **Argus MCP Adapter:** Standardized protocol adapter for connecting Argus telemetry and task execution streams.
3. **ClickUp MCP Integration:** Task management, progress syncing, and workflow card updates.
4. **Workstation OpenCode Installation & Per-Workspace Selection:** Supports native OpenCode per-workspace provider/model selection (`anthropic`, `openai`, `ollama`, `gemini`).
5. **Hybrid-Local MCP Server (`mcp_gateway.py`):** Provides repository analysis (`agy_codebase_analyze_tool`), checkpoint validation (`agy_checkpoint_plan_validate_tool`), and execution control. *Note: Legacy fixed-model Nemotron-30B/Gemma local coupling is completely decoupled and env-configurable.*

---

## 🚀 Quickstart & Setup

### Prerequisites

* **Python:** `Python >= 3.11` (Python 3.13 tested & verified)
* **OpenCode CLI:** Installed on workstation (or run via container)
* **Dependencies:** Install requirements via `pip`:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configuration

Copy `.env.example` to `.env` and fill in your target configuration:

```bash
cp .env.example .env
```

Key environment variables:

```env
GEMINI_API_KEY=your_gemini_api_key_here
AGY_SUPERVISOR_MODEL=gemini-2.5-pro
AGY_DISPATCH_MODEL=gemini-2.5-flash
OPENCODE_SERVER_URL=http://127.0.0.1:4096
OPENCODE_PROVIDER=anthropic
OPENCODE_MODEL=claude-3-7-sonnet
```

---

## 🪟 Windows Deployment Notes

For deployment on Windows workstations or WSL2:

1. **WSL2 Environment (Recommended):**
   * Run under Ubuntu 22.04 / 24.04 inside WSL2 for native POSIX socket performance.
   * Path resolution automatically converts Windows `/mnt/c/...` paths using `pathlib.Path.resolve()`.

2. **Native Windows Setup (Powershell):**
   * Use Python 3.11+ for Windows.
   * Ensure `OPENCODE_SERVER_URL` points to `http://127.0.0.1:4096`.
   * Set `AGY_STATE_ROOT` to a local directory (e.g., `AGY_STATE_ROOT=state`).
   * Run tests with `pytest`.

---

## 🧪 Testing & Verification

Run the reproducible, secret-free test suite:

```bash
pytest -q
```

Verify system health endpoint:

```bash
python3 -c "from app.health import health; import json; print(json.dumps(health(), indent=2))"
```

---

## 📄 License

Distributed under the **Apache License 2.0**. See `LICENSE` for details.
