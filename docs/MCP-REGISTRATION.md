# Additive MCP registration

Phase 1.2 installs **hybrid-local** over stdio. Argus goal/campaign tools are not
implemented or registered as placeholders. There is no claim the official AGY
client has consumed this config; its exact path/schema, live tool access and
native `/goal` support await Phase 1.5 device evidence.

Select the real client's strict-JSON config explicitly with `-McpConfigPath`:

```json
{
  "mcpServers": {
    "existing-server": {"command": "existing-command"},
    "hybrid-local": {
      "command": "<absolute installed venv Python executable>",
      "args": ["<absolute installed scripts/hybrid-local.py>"],
      "env": {
        "HYBRID_WORKSPACES_FILE": "<installation data/workspaces.json>",
        "HYBRID_STATE_ROOT": "<installation data/state>",
        "HYBRID_OPENCODE_BIN": "<existing native opencode.exe>"
      }
    }
  }
}
```

The installer computes absolute paths. It validates unique JSON keys, refuses a
name collision, preserves other entries/preferences semantically and normalizes
formatting. Original bytes are compared before atomic replacement to detect
concurrent edits; close the client to avoid uncooperative writers. Temporary
atomic-write files are removed on normal failure/completion. The receipt keeps
only our entry and hashes, not a full backup of other servers' environment secrets.

Never target `opencode.json`, `opencode.jsonc`, `auth.json` or native documents
with provider/model/agent/`mcp` keys. No model/provider, credentials, agent files
or Desktop settings are copied/changed. Permission-scoped roles exist only in
owned ACP process memory and inherit native model selection. New ACP processes
are not presumed to share Desktop sessions; unowned sessions cannot be adopted.

Tool list: `hybrid_workspace_status`, `hybrid_run`, `hybrid_review`,
`hybrid_supervisor_assist`, `hybrid_status`, `hybrid_start`, `hybrid_resume`.
Installation checks list/status through local stdio with no provider.
`hybrid_status` reads durable evidence; `hybrid_workspace_status` opens a
read-only ACP session to observe native model selection and belongs to deferred
live-device validation. Successful worker completion remains awaiting validation;
there is no automatic acceptance/reviewer verdict.

If the real client uses another schema, stop and adapt only against verified
client documentation/device behavior. Do not guess an AGY registration location,
overwrite another server, configure a provider or expose a public port. Follow
[rollback](ROLLBACK.md) to remove only an unchanged owned entry.
