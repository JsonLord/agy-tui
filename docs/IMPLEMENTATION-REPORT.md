# AGY Argus Hybrid implementation report — recreated Phase 1.2

## Scope and reconstruction

The user requested recreating the missing Phase 1.2 changes and **pushing**
`implementation/argus-phase01` to `https://github.com/JsonLord/agy-tui.git`.
The earlier unpublished checkout and commits are unavailable; new SHAs are
expected. This is a reviewed behavioral reconstruction, not a byte-identical
recovery. The former implementation report was likewise absent; this version
records only fresh evidence. Base: `74f5e2434dd135c7a314b781b531494656a697ed`.
Preparation input: `e5b416d70b4dd837514c16c710c598fd62eb7b7c`.

Read the full spec/preparation report and actual legacy MCP/schema/supervisor/
sentinel sources. Inventory/discrepancies: PHASE0-COMPATIBILITY. The official
Gemini interface stays external; no AGY web app or Argus role engine is built.
Maintained JsonLord/microsoft-argus runtime reuse remains a future requirement.
Per user, actual AGY, authenticated work-PC OpenCode and ClickUp production
validation is deferred to Phase 1.5, not a blocker to installation readiness.

## Reconstructed Phase 1 foundation

* Canonical nonoverlapping registered workspace roots; explicit read/edit and
  write scopes; traversal, symlink, protected native config/auth/Git and Windows
  alias/ADS/device path denial. Empty registry supports unconfigured install.
* Owned OpenCode ACP with absolute cwd, --pure, explicit loopback/ephemeral
  arguments, no fixed --model/provider/URL/model-setting RPC. Native credentials
  and effective model are inherited. Argus environment overrides are stripped.
* Permission-only engineer/reviewer/supervisor agents in process memory; separate
  read-only reviewer/supervisor sessions and one-time scoped engineer file grants.
  Native agent model selection is observed and role switch checked for changes.
  No Desktop session adoption or assumed GUI sharing; plugins remain disabled.
* Managed session/root/role/model bindings and SQLite WAL ledger. Workspace
  turns serialize; tasks are hashed, private remote errors/stderr not persisted.
  Eight-hour maximum turn, independent supervisor assistance capped at two with
  no recursion, uncertain stop/stale lease blocks duplicate dispatch after restart.
  Completion remains COMPLETED_AWAITING_VALIDATION with accepted=false.
* Seven stdio MCP tools including restricted legacy-shaped start/resume, durable
  status, correlation/error codes, absolute launcher independent of caller cwd.
  These are bounded authorization helpers, not campaign/human-approval machinery.

## Reconstructed Phase 1.2 installation/readiness

* PowerShell 5.1+ install/doctor/uninstall scripts, user-local root, no elevation,
  global PATH/execution-policy edits, prerequisite replacement or public listener.
  Python 3.11–3.13, Node 22/24 LTS and OpenCode 1.18.35–1.x accepted preflight
  ranges plus ACP flags; broader platform/version matrix not certified.
* Final-path isolated venv, runtime-only source allowlist and SHA-256 manifest,
  universal hash-pinned wheel dependency lock; normal proxy/CA/TLS preserved.
  Legacy providers, Debian services/containers, recovery, state and credentials
  are excluded from installation.
* Real stdio list/status smoke without ACP/provider before final registration.
  Explicit client path, strict unique JSON keys, additive merge, no replacing
  existing servers, collision/concurrent-edit/native-provider-config protection.
* Durable phase/ownership receipt holding only our nonsecret entry/hashes/versions.
  Doctor checks partial phases, source, registration, imports, dependency versions
  including platform markers and prerequisites. Safe rollback handles interrupted
  installs, rejects edited entry/unknown owner/active or uncertain runs/changed
  paths/reparse points, removes owned source/venv and retains local evidence.
* Default read-only workspace/config-location discovery without credential/config
  contents or native processes; explicit additive validated registration only.
  Effective model parsing remains OpenCode's authority.
* Native Windows .exe resolution without shell shim execution, owned process
  group and kill-on-close job, path alias/ADS/drive/UNC protection. Windows APIs
  contract-tested through mocks; actual Windows testing still pending.
* Read-only full Git object/history audit (including unreachable objects), privacy
  findings and operating docs. No automatic rewrite. Reproducible cloud setup
  helper/validation instructions are now versioned instead of relying on an
  unpublished workspace snapshot.

## Fresh validation

Executed Linux versions: Python 3.12.14, Node 24.19.0, OpenCode 1.18.35,
PowerShell 7.5.3, mcp 2.3.0, pydantic 2.14.0, pytest 9.1.1, ruff 0.17.0.
Artifact hashes and dependency licenses are in PREINSTALL-REPORT/validation JSON.
TLS and checksum verification were retained.

| Check | Result |
| --- | --- |
| Recreated hybrid-local unit/safety suite | 22 passed |
| Recreated real OpenCode ACP synthetic-provider suite | 4 passed: two distinct models with poisoned Argus vars removed, same-session resume, unchanged native config bytes, scoped engineer write, denied cross-root/reviewer writes, unavailable model without fallback/private detail, actual stdio restart status from another cwd |
| Recreated Windows readiness/discovery/audit suite | 34 passed: actual PowerShell Linux dry run and no-provider stdio, installation faults/recovery/merge, source/dependency/receipt protections, read-only discovery, Unicode/space paths and mocked Windows jobs |
| Unchanged legacy P17/P18/P22 core | 40 passed; one pre-existing PytestReturnNotNoneWarning |
| Total focused result | **100 passed, 0 failed, 0 skipped** |
| Full actual PowerShell Linux transaction | **INSTALLED → HEALTHY → UNINSTALLED**, actual hash-locked dependencies, preserved existing MCP/preferences, release deleted and local data retained |
| Cloud setup helper | Executed successfully with pinned research tools and verified PowerShell checksum |
| Changed Python lint/format/compile, shell syntax and Git whitespace checks | Passed |
| Native Windows/Windows PowerShell 5.1/target credentials | **PENDING** |
| Official AGY, native /goal, production OpenCode and ClickUp | **DEFERRED to Phase 1.5** |

The first 26 missing tests were reconstructed; the unchanged legacy core accounts
for 40. The total reproduces the earlier 66+34 behavior/count, but does not assert
identical test-source recovery. No assertion was disabled to force a pass count.
Whole legacy-suite claims from the preparation report are not adopted or rerun.
Reproduction: CLOUD-VALIDATION and scripts/setup-cloud-tests.sh. Synthetic tools
use isolated XDG/native configs and loopback provider fixtures, not production auth.

## Audit, publication and commit evidence

Initial available history: 43 commits, 258 blobs, no unreachable blobs. No strong
credential/key/token/credential-URL matches. Three historical broad literal
matches are environment-variable identifiers, not credentials. Legacy host paths,
author emails and archival source remain disclosed in PREINSTALL-REPORT. No
public-release sanitization or blanket licensing claim; no credential stores read
or duplicated. Later commits add objects and are scanned again before push.

Origin remains https://github.com/JsonLord/agy-tui.git. User explicitly authorized
pushing this branch on 2026-10-13. The separate recommended private destination
is unconfirmed and not used. No remote creation, force push, other branch change,
original Debian change, corporate data access or deployment is authorized/performed.

Checked reconstruction implementation SHA: `6b71f48d6f40d76f7b22708e8b0b2d1fb8492613`.
The implementation-commit all-object rescan covered 44 commits and
293 blobs (0 unreferenced), with zero strong credential findings.
The following evidence-only commit records that exact SHA and changes no runtime
code. A report cannot include its own SHA without changing it.
Versioned evidence: docs/validation/phase12.json. Final remote verification is
reported after the authorized push; do not treat local commit as push evidence.

## 2026-10-14 resolver port before OpenCode 2.x work

Starting fetched remote: `bedba8c4434a0834ed46ca546753f1ae58fe5ae3`.
The separately described Windows resolver/native validation reports are not
present in the branch. Their native validation/~118-test claims are not adopted.
Ported current `@opencode/cli` package-metadata/bin resolution, `.cmd`/`.ps1`/
extensionless locator discovery without reading/executing shims, old npm layouts
and precise resolution failures. Metadata path escapes/missing native images fail
closed. Selected resolver checks: 10 passed (9 new + existing old-layout case).
These are synthetic Linux contracts; native Windows validation remains pending.
The resolver port is committed independently before layering capability/manual
registration changes, without rewriting the fetched branch history.
