# Safe rollback and restart recovery

Close the MCP client first. Stop only managed hybrid-local sessions and verify
uncertain remote work against recorded evidence; never kill OpenCode Desktop
by executable name. A stale ledger or reboot is not proof of remote termination.
Native Windows owned-job cleanup is implemented but still needs device testing.

From the reviewed source checkout:

```powershell
$install = Join-Path $env:LOCALAPPDATA 'AGYArgusHybrid'
.\scripts\doctor-windows.ps1 -InstallRoot $install -PythonPath 'C:\Python313\python.exe'
.\scripts\uninstall-windows.ps1 -InstallRoot $install -PythonPath 'C:\Python313\python.exe' -DryRun
.\scripts\uninstall-windows.ps1 -InstallRoot $install -PythonPath 'C:\Python313\python.exe'
```

Use prerequisite Python outside the venv being removed. Keep this checkout for
recovery until device checks pass. Rollback validates ownership, release path
and source-manifest paths. Unexpected owned-directory links/junctions or Windows
reparse points fail closed. It removes the server only when it exactly matches
the receipt, preserves all other server entries/preferences, then removes only
owned release source/venv. An absent entry is safe and repeated successful
uninstall is idempotent. Even a newly created client JSON file is retained.
No OpenCode credentials, providers, agents, Desktop sessions or prerequisites
are removed. There is no full-config backup/restore that could clobber user edits.

`data/workspaces.json`, `data/state` and `installation.json` remain for evidence
and recovery. Retention is intentional; do not commit these local paths/session
identifiers to GitHub. After verifying no managed work remains and exporting
needed evidence, the user may explicitly remove this owned root. No automatic
data purge is provided. Reinstall after UNINSTALLED can reuse retained data;
existing active/failed or unowned nonempty roots require review/rollback first.

The phase journal records PREPARING, DEPENDENCIES, REGISTERING, INSTALLED,
FAILED and UNINSTALLED. Dependencies and no-provider stdio smoke finish before
registration. Failed installs record their last phase and doctor returns nonzero.
Crash after registration may leave REGISTERING; rollback recognizes the recorded
entry and removes it safely. Crash before registration leaves the client unchanged.
An incomplete venv/source does not prevent receipt-owned partial cleanup.

If our entry was edited, rollback stops before deleting files. Review the actual
entry and receipt locally; either restore the exact owned entry after review or
remove it yourself in the closed client, then rerun rollback. Do not dump the
full client JSON: other servers may have credentials in their environments.

STARTING, RUNNING and REMOTE_STOP_UNCONFIRMED records block uninstall. Verify
owned ACP processes stopped and reconcile the ledger through operator-reviewed
recovery; Phase 1.2 offers no blind stale-lease-clearing command. Unreadable
state or corrupt/missing receipt requires local inspection rather than deleting
arbitrary paths or fabricating ownership. No in-place upgrade or automatic
restart takeover is attempted. Reinstall a reviewed earlier checkout for rollback
of application version.
