# Windows installation — Phase 1.2

Install the portable **hybrid-local** stdio MCP interface, not a replacement AGY
web application or an Argus campaign daemon. Official AGY, authenticated work-PC
OpenCode and production ClickUp gates are deliberately deferred to Phase 1.5.
Native Windows compatibility is **pending**: the implementation was tested using
PowerShell 7.5.3 on Linux, not a Windows kernel or Windows PowerShell 5.1.

## Prerequisites

Windows 10/11, PowerShell 5.1 or 7, Python 3.11–3.13, Node 22/24 LTS and an
existing native OpenCode installation. Preflight accepts OpenCode 1.18.35–1.x
and checks its ACP flags. Accepted ranges are not a certified version/platform
matrix; tested Linux versions are recorded in IMPLEMENTATION-REPORT.

Install missing prerequisites using their official per-user installers. Our
scripts do not install/upgrade OpenCode or Node, elevate, change global PATH,
execution policy or Desktop configuration. Python discovery supports normal
executables and the Windows `py.exe` launcher. Pass executable paths explicitly
when discovery fails. An npm `opencode.cmd` is only a locator for the native
package's `opencode.exe`; its shell text is never read or executed.

Close the MCP client while editing its configuration. Determine its real,
strict-JSON `mcpServers` file (see MCP-REGISTRATION); never select OpenCode's
provider/model/agent configuration or `auth.json`. Actual official AGY config
location and client support remain device-validation questions.

## Install and diagnose

From the reviewed checkout:

```powershell
$install = Join-Path $env:LOCALAPPDATA 'AGYArgusHybrid'
$config = 'C:\Client configuration\mcp-servers.json' # Replace with the actual client file.
.\scripts\install-windows.ps1 -InstallRoot $install -McpConfigPath $config `
  -PythonPath 'C:\Python313\python.exe' -OpenCodePath 'C:\OpenCode\opencode.exe' -DryRun
# After reviewing the plan, omit -DryRun:
.\scripts\install-windows.ps1 -InstallRoot $install -McpConfigPath $config `
  -PythonPath 'C:\Python313\python.exe' -OpenCodePath 'C:\OpenCode\opencode.exe'
.\scripts\doctor-windows.ps1 -InstallRoot $install -PythonPath 'C:\Python313\python.exe'
```

Dry run invokes only CLI version/help commands and writes no application,
registry or MCP configuration files. Native tools may maintain their own CLI
cache; no ACP session or model/provider invocation occurs during install.

The installer copies only allowlisted portable runtime Python files, launchers,
installation scripts and `requirements-windows.lock`; legacy model/provider
modules, Debian services, containers, recovery sources, Git history, credential
stores, caches and state are excluded. Each release has an isolated venv at its
final absolute path (venvs are not relocated). Dependencies are wheel-only,
version/hash-pinned, installed with `pip --require-hashes`. Use existing proxy
and CA trust; never bypass TLS or hashes. PyPI/an approved mirror is needed;
standard pip environment options support an existing offline hashed wheelhouse.
Do not put credentials in commands, Git, the lockfile or setup scripts.

An external data directory stores the initially empty workspace registry and
SQLite evidence. The receipt journals installation phase, source SHA-256
manifest, versions and only our nonsecret MCP entry. The real local stdio smoke
checks initialization, all seven tools and `hybrid_status` without OpenCode.
Registration occurs last. No public listener, service, scheduled task, firewall
rule or global credentials are created. Later authorized ACP tasks use explicit
loopback/ephemeral-port arguments; these are not public endpoints.

Doctor verifies receipt/phase, file hashes, exact registration, imports, locked
installed dependency versions/markers and prerequisite versions/ACP flags. It
does not open ACP sessions or production providers. `HEALTHY` means local
installation checks passed, not real-device integration approval.

## Read-only discovery and explicit registration

```powershell
$receipt = Get-Content (Join-Path $install 'installation.json') -Raw | ConvertFrom-Json
$python = $receipt.entry.command
$discovery = Join-Path $receipt.release 'source\scripts\workspace-discovery.py'
$registry = Join-Path $install 'data\workspaces.json'
& $python $discovery --registry $registry
& $python $discovery --registry $registry --register-id reporting `
  --root 'C:\Projects équipe\Reporting 日本語' --write-path src --write-path tests
```

Listing creates nothing, does not recursively scan home directories, opens no
native configuration/auth contents, and invokes no OpenCode process/session.
It lists registered roots, capabilities, scope and candidate configuration
locations: project/ancestors up to the Git root, user XDG config and explicit
native config environment paths. Only existence flags and inline-config
presence are shown. This resolves **locations**; effective precedence, merged
config/model evaluation belongs to OpenCode and is a deferred ACP device check.
Missing registry gives an empty list without creating a file.

Only `--register-id` mutates our registry. Duplicate IDs, overlapping roots,
traversal and invalid scopes fail before atomic replacement. Without write paths
the registered workspace is read-only. No OpenCode settings/credentials are
changed. Git, native configs, auth and private profile paths are protected.
Windows device, UNC, drive-relative, alias and alternate-data-stream paths are
refused. Local paths with spaces/Unicode are implemented and contract-tested;
Windows behavior still requires device validation. Keep registry/evidence local
because paths and session identifiers may be personal information.

## Diagnostics and recovery

| Diagnostic | Action |
| --- | --- |
| PYTHON/NODE/OPENCODE_VERSION, ACP_FLAGS | Supply an accepted existing executable; do not overwrite Desktop settings. |
| PROCESS_FAILED, INSTALL_FAILED | Inspect receipt phase; check permissions, paths, proxy/CA trust and wheel availability. Raw subprocess output is suppressed to avoid exposing secrets. |
| MCP_CONFIG_FORMAT, NATIVE_CONFIG_PROTECTED | Select actual strict-JSON client mcpServers config. JSONC/duplicate keys/native provider documents are unsupported. |
| MCP_NAME_COLLISION, INSTALLATION_EXISTS | Doctor existing installation, safely uninstall, then reinstall. Never replace an unrelated server. |
| CONFIG_CHANGED | Close client, review concurrent edits, rollback partial installation, retry. |
| SOURCE_HASH_MISMATCH, dependency mismatch | Rollback owned release and reinstall reviewed source. |
| MCP_ENTRY_CHANGED, OWNED_PATH_CHANGED, RELEASE_LINK | Review changes; no blind deletion or full-config restoration. |
| RUNS_ACTIVE_OR_UNCERTAIN, STATE_UNREADABLE | Verify owned processes stopped and reconcile evidence before rollback. |

See [rollback](ROLLBACK.md). The user-local root needs no administrator rights
when prerequisites and client config are user-writable. No execution-policy
bypass is supplied if company policy blocks PowerShell scripts; use approved
IT procedures. No corporate data or production provider tests are required here.

## Phase 1.5 on the work computer

Record Windows build/architecture, tool versions and Git SHA. Run dry-run,
install, doctor, rollback and reinstall with spaces/Unicode paths; verify
existing server entries and native configuration hashes remain unchanged. Test
actual Windows job/process-child termination, path isolation and persisted ACP
sessions. Then verify two workspaces' **existing** models/providers, official
AGY tool access and `/goal` behavior. Do not set models to make tests pass.
ClickUp approvals and maintained Argus child/integration runtime remain later work.
