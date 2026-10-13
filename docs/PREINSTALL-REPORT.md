# Phase 1.2 recreation and preinstallation audit

## Identity and provenance

Existing `/workspace/agy-tui` checkout, recreated branch
`implementation/argus-phase01`, seed `74f5e2434dd135c7a314b781b531494656a697ed`.
The prior unpublished workspace's Phase 1 and Phase 1.2 commits (including
`b8a4d6a`, `7c72fb7`, `c21d618` and `c1f9bdd`) are unavailable in this Git object
store or remote branches. This is a **behavioral reconstruction**, not recovery
of their exact bytes/SHAs. Reports/test/audit counts below describe this run.

The complete seed `spec.md`, historical preparation report on
`hybrid-optimised` (`e5b416d70b4dd837514c16c710c598fd62eb7b7c`), prior conversation
implementation details and actual legacy sources were inspected. The report
was archived with trailing whitespace normalized; its deployment/test/license
claims are historical input, not reproduced evidence.

Remote: `origin https://github.com/JsonLord/agy-tui.git` (unchanged).
The user explicitly authorized recreating and pushing the implementation branch
to this remote. The recommended separate private `agy-argus-hybrid` destination
was previously inaccessible and is not being created/used. Origin privacy has
not been independently verified. Personal legacy references are disclosed here;
this is not a history sanitized for public release. No original Debian service,
global credentials, other branch, remote configuration or deployment is changed.

## Full-object security and portability audit

The non-shallow fetched repository initially contained **43 commits and 258
historical blobs**, with no unreachable blob. All fetched refs/tags, reflogs,
HEAD/FETCH_HEAD and all Git objects (including any unreachable objects) were
scanned. Commit author/committer metadata/messages and tracked/untracked
nonignored files were reviewed. Ignored environments were inventoried by name,
not credential contents. Final implementation/evidence commits add objects;
exact commit and later audit evidence are in IMPLEMENTATION-REPORT.

Reproduce the read-only audit with `python3 scripts/audit-repository.py`.
It prints classes/object/path references, never matching credential values.
Pattern scanning is not proof of absence or an external vulnerability advisory
assessment. Findings include semantic review of ambiguous literals and source
layout/installer/process permissions. No history is rewritten automatically.

| Category | Observed findings and handling |
| --- | --- |
| Strong GitHub/provider/cloud tokens, private keys, credential URLs | Zero matches in full initial history and nonignored working files. No credential store copied or token printed. |
| Broad credential literals | Three historical app/config.py blobs and current file match public AGY environment-variable identifier strings. Semantically reviewed; not credential values. |
| Personal/machine-specific references | 36 historical blobs contain host-path patterns. Legacy preparation/forensic docs, scripts/tests/config retain personal host paths and deployment references. Author/committer emails remain in history. No values repeated here or automatic rewrite. |
| Email-shaped file contents | Historical synthetic Git user.email test fixture; recreated audit tests also use synthetic metadata. Actual author emails remain separately in commit metadata. |
| Runtime and logs | No tracked raw .log/.sqlite3/.dump/.pem/.key artifacts identified; zero ignored runtime/log/dump filename matches at audit. Archival forensic/recovered Python sources remain, but installation excludes them. |
| Ignored credential-named files | Twelve credentials.py/bytecode names inside ignored third-party legacy venv packages, not user credentials. Environments/caches are not staged or installed as source. Research binaries/cache/logs remain outside the checkout. |
| Legacy fixed providers/models | Remain in legacy app/config.py/templates/services. Portable package excludes them; new runtime imports its own modules and pure app/schemas.py only. No new native production model/provider configuration. |
| Supply chain | Runtime/transitive dependency versions/hashes locked; wheel-only isolated install and TLS/hash checks preserved. OpenCode/PowerShell research artifacts version/checksum verified. No binaries vendored in Git. |

No blanket repository relicense is applied. The upstream branch's license claims
are not assumed to grant rights to all archival material. Runtime dependency
metadata/licensing inventory is versioned in `docs/validation/phase12.json`;
direct mcp/pydantic are MIT. Future maintained Argus reuse must retain its pin,
provenance/license; no Manager/Planner/Engineer/Reviewer sources are vendored.
Public redistribution/license/privacy clearance is separate from this authorized
push of reviewed source to the existing user-selected repository.

## Portability and safety

Installer copies an allowlisted stdio runtime into a unique release and isolated
venv, preserving native Desktop credentials/providers/agents/projects. It never
sets a model, copies auth, elevates, replaces prerequisites or exposes a public
port. MCP merge is additive/strict JSON with collision, duplicate-key and
concurrent-edit guards; the receipt never duplicates other servers' environment
values. Registration follows a real no-provider stdio smoke. Doctor checks
phase/source hashes/registration/imports/locked package versions and prerequisites.
Rollback validates ownership/paths, refuses edited entries, active/uncertain
state and reparse points, removes only owned release/entry, and retains evidence.

Discovery lists registered roots and configuration locations read-only without
opening native configs/auth; effective model evaluation belongs to OpenCode.
Registration is explicit and scoped. Native Windows argv/executable resolution,
owned group/job termination and alias/ADS protections are implemented; only
contracts/mocks were executed for Windows-specific APIs. Real ACP on Linux
verified paths with spaces/Unicode, independent models, permissions, resume and
MCP restart evidence. This is a native permission boundary, not an OS sandbox.

**Native Windows compatibility remains PENDING.** No Windows kernel, Windows
PowerShell 5.1, authenticated target Desktop or corporate resources are available
here. Official AGY, `/goal`, production OpenCode and ClickUp gates are deliberately
deferred to Phase 1.5; Argus autonomous projects/integration remain out of scope.

## Executed validation

Python 3.12.14, Node 24.19.0, OpenCode Linux x64 1.18.35, PowerShell Linux 7.5.3,
mcp 2.3.0, pydantic 2.14.0, pytest 9.1.1, ruff 0.17.0.
PowerShell archive SHA-256:
`5c74e0bbafd8be59e72267f05700fd6615344d9cf4d30460d9b6d12cd4a88a8c`.
OpenCode native binary SHA-256:
`77b2cfe4b97df6f15c3673b22100b9f79c711f25ecb9bf513bb82526b15d24fa`.

100 focused checks pass: 22 recreated unit/safety, four recreated real ACP
synthetic-provider cases, 34 readiness cases, 40 unchanged legacy regressions
(one pre-existing PytestReturnNotNoneWarning). Do not describe the missing
original test files as recovered byte-for-byte or this as a whole legacy-suite run.

Full real PowerShell/Linux transaction passed using spaces/Unicode install and
client-config paths: **INSTALLED → HEALTHY → UNINSTALLED**. It verified actual
hash-pinned dependency installation, stdio tools/status without a provider,
unchanged existing server/preferences, no retained private environment sentinel,
owned source/venv deletion and workspace/evidence retention. Cloud setup helper
was also executed successfully. See CLOUD-VALIDATION and operating docs.
