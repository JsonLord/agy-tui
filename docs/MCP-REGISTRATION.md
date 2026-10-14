# Manual hybrid-local MCP registration

Installation now defaults to manual registration. The PowerShell installer has
no client MCP config argument and does not read or write the user's Antigravity/
Gemini config. `-ManualMcpRegistration` explicitly selects the same safe default.
No auto-opt-in is exposed by the CLI. A legacy Python API opt-in remains only
for compatibility/testing; it is not used by this workstation procedure.

After local isolated runtime installation, our receipt holds the generated entry
and `<InstallRoot>/mcp-entry.json` holds this JSON fragment:

```json
{
  "mcpServers": {
    "hybrid-local": {
      "command": "<absolute installed venv Python executable>",
      "args": ["<absolute installed scripts/hybrid-local.py>"],
      "env": {
        "HYBRID_WORKSPACES_FILE": "<our data/workspaces.json>",
        "HYBRID_STATE_ROOT": "<our data/state>",
        "HYBRID_OPENCODE_BIN": "<already installed native opencode.exe>"
      }
    }
  }
}
```

The installer computes these values and prints the fragment. The user inserts
only the hybrid-local entry into the actual client's mcpServers object, retaining
other servers. Do not overwrite the whole client document with the fragment.
Never add model/provider/API URL/key/auth profile or target an OpenCode native
configuration/credential file. No credential store is copied into our receipt.

Default doctor reads only our files, versions/help and dependency metadata; it
reports `UNVERIFIED_MANUAL` until explicitly pointed at a client file:

```powershell
.\scripts\doctor-windows.ps1 -InstallRoot $install -PythonPath $python `
  -McpConfigPath 'C:\Actual client config\mcp_config.json'
```

That check is read-only and reports VERIFIED_EXPLICIT_READ_ONLY or
MISSING_OR_DIFFERENT; it never repairs/inserts/removes an entry. Do not share the
whole config: other server environments may contain secrets. An existing legacy
automatic receipt also requires an explicit path for doctor registration checks.

Uninstall in manual mode leaves the client untouched; the user removes their
own entry before reopening the client. Legacy automatic receipts fail closed
with MANUAL_REMOVAL_REQUIRED under CLI rollback rather than silently editing the
client. No new public transport, service, login, Desktop adoption or Argus tools.

OpenCode 2.0.28 capability detection works, but workers currently fail before ACP
startup for native-state/plugin-isolation blockers. Registration does not
constitute successful runtime validation. See OPENCODE-2X-EVIDENCE.md and the
read-only device commands. Official AGY integration remains deferred.
