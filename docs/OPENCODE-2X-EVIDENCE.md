# OpenCode 2.0.28: pinned source evidence and safety decision

Observed npm package: `@opencode/cli@2.0.28`, repository `anomalyco/opencode`,
`bin.opencode = bin/opencode.exe`, platform dependencies at 2.0.28.
No OpenCode package/binary was installed, replaced, patched or executed as a
2.x worker in this task. Only public metadata and source archives were read.
Source tag `v2.0.28` resolves to
[`026b106bc5d41b679ffc413264229c340871e4d3`](https://github.com/anomalyco/opencode/tree/026b106bc5d41b679ffc413264229c340871e4d3).
This source version is deliberately pinned; future versions require review.

## Findings from this source, not inference from 1.x

All paths below refer to that exact commit and can be appended to its `blob` URL.

| Property | Source | Finding |
| --- | --- | --- |
| Accepted ACP CLI options | packages/cli/src/commands/commands.ts (acp specification) | ACP exposes --login; old --cwd/--pure/--hostname/--port are not ACP options. Hybrid never requests login. |
| ACP wire transport | packages/cli/src/commands/handlers/acp.ts | Newline JSON-RPC protocol 1 over stdin/stdout; starts Standalone, never attaches shared Desktop/background service. |
| Private server ownership | packages/cli/src/services/standalone.ts | Self-spawns `serve --stdio --port 0`, cwd captured from startup process.cwd(), random Basic lease credential, piped stdin lease, SIGTERM/3-second force kill. EOF closes lease even if parent dies. |
| Actual network listener | packages/cli/src/server-process.ts; packages/server/src/process.ts | ACP-facing transport is stdio, **but an internal HTTP child listens**. Non-service mode defaults hostname 127.0.0.1, explicit child port 0. No service-config hostname/port/tunnel registration is used in stdio mode. Do not claim fully network-free ACP. |
| Workspace/project selection | packages/cli/src/acp/service.ts; directories.ts | session/new passes absolute params.cwd as location.directory. ACP rejects relative cwd. Load/resume checks saved session directory equals supplied cwd. OS cwd also propagates to server startup; it is not a substitute for session cwd. |
| Session state/model | packages/cli/src/acp/sessions.ts; config-option.ts | Attaches saved session's model/agent; configOptions returned. switchModel is separate from switchAgent. Hybrid never calls model/provider selection. |
| Native configuration | packages/cli/src/server-process.ts; packages/core/src/config.ts | Preserves explicit config file/dir/content and native project discovery; inline content merged last. Disabling project config would violate inheritance and is not used. |
| Credential inheritance | packages/core/src/credential.ts; credential/sql.ts | Stored native credentials are read via the global Database service; not a separate read-only ACP auth store. |
| Database writes | packages/cli/src/database-path.ts; server-process.ts; packages/core/src/database/database.ts; migration.ts | Native data/opencode.db by default; opens WAL, migrations, permission/creation operations and session persistence. OPENCODE_DB can redirect, but an empty/owned DB loses stored credentials; copying/importing them is forbidden. No supported separate read-only credential DB with writable owned sessions was established. |
| Plugins/purity | packages/core/src/config/plugin/source.ts; plugin/{supervisor,module,internal}.ts; packages/cli/src/acp/catalog.ts | Discovers and activates native configured plugins before catalog readiness. PluginSupervisor supports -* and prefix selectors, but processes earlier add/module-load operations before later removals. PluginModule has an integration.use/plugin:* pre-load policy check; ManagedPolicy evaluates reversed file priority plus later organization policy with findLast, so an inline denial is not universally authoritative. PluginModule.load can install/import code before deactivation. No equivalent pre-load external-only --pure gate preserving native provider/auth plugins was established. Empty plugins does not erase discovered plugins; wildcard deactivation is not proof of no external code evaluation. |
| Permission schema | packages/schema/src/permission.ts; config/agent.ts; packages/core/src/config/normalize.ts | Canonical ordered permissions/agents plus legacy normalization. OPENCODE_PERMISSION is not forwarded by 2.x server-process. A removed env knob is not an enforcement mechanism. ACP permission requests remain one-time/cancelled by hybrid. |
| Cancellation/shutdown | packages/cli/src/acp/turn.ts; commands/handlers/acp.ts; services/standalone.ts | Cancel ends prompt as cancelled and rejects pending permission requests. Stdin EOF exits ACP; lease closes private child. Upstream lifecycle test checks EOF, but its private-server-death test explicitly skips Windows. |

Official documentation **at the same source tag**:
[services/www/src/docs/content/cli/acp.mdx](https://github.com/anomalyco/opencode/blob/026b106bc5d41b679ffc413264229c340871e4d3/services/www/src/docs/content/cli/acp.mdx).
Documentation says no network port *for ACP*; implementation still creates the
private HTTP child described above. Implementation controls the conclusion.
Upstream subprocess tests inspected, not executed:
packages/cli/test/acp/{lifecycle.subprocess,session,permission}.test.ts.

## What the old flags actually did

Pinned 1.18.35 source `packages/opencode/src/cli/cmd/acp.ts` supplies cwd and
network options to Server.listen; stdio ACP sits on top of that server.
`packages/opencode/src/plugin/index.ts` uses flags.pure to skip external plugin
origins while retaining internal plugins. It does not discard native providers,
authentication or workspace model configuration. Hybrid still uses this complete
bounded contract for the existing 1.18.35 regression binary; no new binary is
installed by this task. Unknown versions fail instead of inheriting old flags.

## Decision under the user's immutable-state contract

**2.0.28 does not currently satisfy all requested safety properties.** Network,
workspace and owned transport mechanisms are source-established. Native state
immutability and an equivalent external-plugin exclusion mechanism are not.
Stored credentials and writable sessions share the native DB; redirecting it
silently would break auth inheritance, and copying credentials is forbidden.
Plugins can execute outside ordinary tool permission grants.

The capability layer recognizes 2.0.28, records its candidate flagless `acp`
argv and correct transport/cwd model, and permits read-only version/help checks
and installation of **our runtime**. `require_worker()` rejects before starting
ACP with `OPENCODE_NATIVE_STATE_MUTATION`; it also reports
`OPENCODE_PLUGIN_ISOLATION_UNVERIFIED`. No bypass or fake passing 2.x trace exists.
No artificial major-version range rejection remains: this is a specific,
source-derived unmet-capability decision. `hybrid_status` remains available;
workstation execution is not advertised as ready.

A complete operational adapter requires a supported upstream separation of
read-only native credentials from writable owned session state, and verified
plugin isolation (or a separately revised user contract). Do not patch/reinstall
OpenCode or weaken the policy to unblock it. Native Windows testing alone cannot
make source-proven native DB writes disappear.

## Evidence levels and next native tests

IMPLEMENTED: read-only modern resolver, explicit capability contracts, manual
registration default, diagnostics, conservative startup gate, owned Job Object
and POSIX process-group cleanup, immutable caller configuration behavior.

TESTED SYNTHETICALLY: version/help matching, unknown-version rejection, candidate
argv, spaces/Unicode, metadata escapes, environment preservation, pre-start 2.x
block, manual config nonwrites, explicit doctor validation, owned process-tree
cancellation/timeout and unrelated process preservation. Existing real 1.18.35
synthetic-provider tests remain separate evidence, not 2.x validation.

VALIDATED ON NATIVE WINDOWS: none in this checkout/session. Separately mentioned
unprovided reports/~118 tests are not adopted. No target device was contacted.

DEFERRED TO PHASE 1.5: after resolving the strict state/plugin blockers, run two
owned ACP sessions in existing projects A/B; observe actual cwd and native model/
provider invocations without overrides; compare native config hashes; verify
loopback-only ephemeral child listener, child lease cleanup, Windows Job Object
containment including crash/cancel/timeout, unrelated Desktop survival, resume
same cwd/model and restart uncertainty. Never hash/read credential store content.

Safe commands now (no ACP session/inference):

```powershell
Get-Command opencode -All | Select-Object Name,CommandType,Source
opencode --version
opencode acp --help
opencode debug paths
& 'C:\Python313\python.exe' .\scripts\opencode-doctor.py `
  --opencode "$env:APPDATA\npm\opencode.cmd" `
  --workspace 'C:\Projects\Alpha' --workspace 'C:\Projects équipe\Beta 日本語' `
  --hash-config 'C:\Projects\Alpha\opencode.json'
```

Use the actual already-installed Python/executable/project paths. Hashing compares before/after CLI-probe
config bytes without parsing/displaying values; do not pass auth.json, API keys,
provider secrets or credential stores. This diagnostic reports paths/capabilities
and hashes only and does not establish effective model/provider/workspace use.
