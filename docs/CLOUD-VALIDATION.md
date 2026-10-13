# Reproducing cloud validation

Use the existing checkout; do not create another worktree unless requested.
Inspect branch/status and preserve user changes. The recreation is based on
`74f5e2434dd135c7a314b781b531494656a697ed`, on `implementation/argus-phase01`.
No daemon, public endpoint, production credential or corporate data is needed.

Linux x64 setup (the repository helper is tested, checksum verification retained):

```sh
bash scripts/setup-cloud-tests.sh
export HYBRID_TEST_OPENCODE=/workspace/agy-cloud/opencode-inspection/node_modules/opencode-linux-x64/bin/opencode
export HYBRID_TEST_PWSH=/workspace/agy-cloud/pwsh/pwsh
export XDG_DATA_HOME=/workspace/agy-cloud/opencode-inspection/xdg/data
export XDG_CONFIG_HOME=/workspace/agy-cloud/opencode-inspection/xdg/config
export XDG_CACHE_HOME=/workspace/agy-cloud/opencode-inspection/xdg/cache
export XDG_STATE_HOME=/workspace/agy-cloud/opencode-inspection/xdg/state
.venv-hybrid/bin/python -m pytest -q tests/test_hybrid_local.py tests/test_hybrid_local_live_opencode.py tests/test_windows_readiness.py
.venv/bin/python -m pytest -q tests/test_agy_p17_tool_free_transport.py tests/test_agy_p18_supervisor.py tests/test_agy_p22_final_e2e.py
python3 scripts/audit-repository.py
```

The hybrid/readiness suite contains 60 cases, including four real OpenCode ACP
synthetic-provider cases and one real PowerShell dry run. Without research binary
variables those integrations skip: record skips honestly. Legacy core is 40
cases, with one pre-existing return-value warning. The missing historical test
files were behaviorally reconstructed, not recovered byte-for-byte.

CLI help/version and PowerShell need all four isolated XDG directories because
the cloud home is read-only. Never apply these research overrides to Desktop
settings on the real work computer. Live ACP fixtures use their own temporary
native configurations, synthetic provider and per-test XDG state, never the
work computer's authenticated providers. Temporary HTTP servers are loopback
only and stopped by fixture teardown. Tests assert unchanged project configs,
actual distinct provider model invocations, scoped permissions and restart status.

Full installation validation can also run the documented PowerShell scripts on
Linux with explicit InstallRoot, McpConfigPath, PythonPath and the research
OpenCode binary. Use a disposable root/client JSON outside the checkout with an
existing server/preferences, install without DryRun, doctor, then uninstall.
Verify INSTALLED → HEALTHY → UNINSTALLED; preserve other entries/preferences,
remove release source/venv, retain local registry/evidence. This is not Windows
compatibility proof. Real Windows, official AGY and authenticated work-PC gates
remain pending/deferred to Phase 1.5.

An earlier cloud configuration draft was unpublished and did not preserve the
missing Git objects across workspace reset. The reproducible helper and this
document are now committed with the implementation; never assume a live process
or unpublished workspace state survives. The authorized GitHub branch is the
persistent source of the recreated commits. No environment publication or
production deployment is part of this request.
