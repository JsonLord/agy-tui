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

## 2026-10-14 immutable OpenCode 2.0.28 investigation and safe adaptation

STARTING_REMOTE_SHA: `bedba8c4434a0834ed46ca546753f1ae58fe5ae3`.
RESOLVER_FIX_PRESENT at start: **false**. The missing modern resolver was ported
first in `f07411718919a53ae89656f919953bcb0226d0f5`, not assumed from unprovided
native-Windows reports. Their claimed ~118 passes/live validation are not reused.

Exact official tag v2.0.28 source:
`026b106bc5d41b679ffc413264229c340871e4d3`, npm `@opencode/cli@2.0.28` metadata.
Downloaded source archive SHA-256:
`92db4b1c94902153ea1d9ea4c7e53df97b60471e563ef22a84fce9e8cb795c6a`.
Source/docs/tests inspected without installing, compiling or patching OpenCode.
Complete findings and exact device commands: **OPENCODE-2X-EVIDENCE.md**.

### Important unmet operational requirements

This is **not a claim that 2.0.28 workers now satisfy every requested invariant**.
The old flags have source-proven replacements for workspace and network context:
OS cwd plus absolute ACP session cwd, stdio ACP, owned `serve --stdio --port 0`
child with default `127.0.0.1` and random lease authentication. It does have an
internal HTTP listener despite no ACP TCP endpoint.

However, native credentials and sessions share the writable native database.
Starting its private server can create/migrate/chmod/WAL that database; redirecting
or copying it breaks the no-auth-profile-override/no-credential-duplication rules.
No separate read-only native credential store with writable owned sessions was
established. Plugin wildcard removals exist, but operate after earlier module
load/install/import operations and may also disable native integrations; they
are not proof of old --pure's external-code exclusion. A pre-load integration
policy gate exists, but higher-priority native/organization policy can override
an inline denial; universal enforcement under unknown native policies is unverified.

The explicit capability layer recognizes **2.0.28** and its candidate flagless
launch contract, allows manual installation/metadata diagnostics, but blocks
before spawning ACP with `OPENCODE_NATIVE_STATE_MUTATION` and reports
`OPENCODE_PLUGIN_ISOLATION_UNVERIFIED`. This replaces arbitrary <2 rejection
with precise source-derived blockers; unknown versions still fail before ACP
help/startup. No bypass flag, workaround credentials copy, project-config disable,
model/provider/URL/key/auth override, or fake 2.x success trace was introduced.
An operational 2.x worker requires supported upstream isolation or a separately
revised user contract. Device testing cannot invalidate these source facts.

### Implemented and verified changes

* Current npm `@opencode/cli` metadata/native exe and old-layout resolver,
  `.cmd`/`.ps1`/extensionless locators without executing/reading shim text,
  path-escape/missing/blocked-executable diagnostics.
* Central pinned capabilities for 1.18.35 and 2.0.28, no scattered major-version
  bypasses. Existing 1.18.35 full bounded argv retained. Unknown/contradictory
  help contracts fail, and 2.x's unfulfilled properties are explicit data.
* Manual registration is the installer/PowerShell default. CLI installation has
  no client config path; only our receipt/root mcp-entry.json/output stores the
  computed fragment. Doctor checks client registration only on explicit path and
  reports UNVERIFIED_MANUAL, VERIFIED_EXPLICIT_READ_ONLY or mismatch honestly.
  The old Python mutation API requires explicit opt-in solely for compatibility
  contracts, and is never selected by user-facing CLI. CLI legacy rollback fails
  closed for operator manual removal instead of editing the client.
* Read-only version/help/config-hash diagnostic, no ACP/session/inference or
  credential-file contents. No user workstation interaction attempted.
* Existing scoped permissions, model/provider inheritance and poisoned-Argus
  stripping preserved. Owned Windows Job Object retained with CloseHandle failure
  detection. Owned POSIX process group, parent-exit observer and bounded EOF drain
  added: inherited child pipes cannot hide an ACP crash. Cancellation/timeout/
  crash kill owned descendants only; uncertain turn/cleanup retains a lease and
  blocks duplicate dispatch. No kill by executable name or Desktop adoption.

### Fresh evidence levels

IMPLEMENTED: resolver, capability classification/blockers, manual registration,
read-only diagnostics, conservative shutdown and docs above.

TESTED SYNTHETICALLY: new capability/version/cwd/Windows-path contracts, no unsafe
2.x startup/native config changes, environment inheritance, manual fragment/doctor
behavior, blocked executable, and actual controlled POSIX owned-tree shutdown with
unrelated process preservation. These are not a live OpenCode 2.x provider proof.

Existing real 1.18.35 synthetic-provider regression tests remain independent:
two workspace model invocations, permissions, resume and MCP restart. No 2.x
binary was installed/upgraded/downgraded/replaced or used as a real worker.

VALIDATED ON NATIVE WINDOWS: **none in this session**; all actual Windows 11/
2.0.28 Job Object, resolver and filesystem/runtime tests remain user-run/pending.
DEFERRED TO PHASE 1.5: after state/plugin blockers are resolved, native A/B cwd/
model/provider invocation, config integrity, loopback ephemeral listener/lease,
owned child shutdown and unrelated Desktop survival, resume/restart evidence.
ClickUp, /goal, Argus orchestration, pools and deployment remain out of scope.

Focused validation: 60 previous hybrid/readiness cases retained (legacy mutation
contracts explicitly opt in; CLI assertions now test manual nonwrites), 36 new
resolver/capability/shutdown cases, 40 unchanged legacy core cases = **136**.
The final completed test/audit results and exact checked adaptation SHA are
recorded by the following evidence commit. `docs/validation/opencode2028.json`
records this task separately from the historical Phase 1.2 manifest.

A real PowerShell 7.5.3-on-Linux manual transaction using the **already present**
1.18.35 research executable passed: INSTALLED/USER_ACTION_REQUIRED →
HEALTHY/UNVERIFIED_MANUAL → UNINSTALLED; pre-existing client JSON remained
unchanged throughout, fragment matched output, source/venv removed, data retained.
Our runtime dependencies were installed only in this disposable isolated venv;
OpenCode/Node/Python installations and global PATH/security controls were untouched.

The mandatory user-defined final summary and the explicit answer to all five
safety/evidence questions are also documented in OPENCODE-2X-EVIDENCE.md.

Completed validation for the above changes: **136 passed, 0 failed, 0 skipped**
(96 focused hybrid/readiness/new cases + 40 unchanged legacy cases; one old
return-value warning). Ruff/format/compile/diff checks passed. Final manual
PowerShell/Linux transaction was repeated after packaging the diagnostic:
INSTALLED/USER_ACTION_REQUIRED → HEALTHY/UNVERIFIED_MANUAL → UNINSTALLED,
with client JSON unchanged throughout. Native Windows and live 2.x remain
unrun; 2.x workers remain deliberately blocked, not accepted as ready.
Fresh versioned evidence: `docs/validation/opencode2028.json`.

Checked safe-adaptation commit: `8511928838e1c0756905e4fd12b896e5a40f1aad`.
Its all-object scan covered 47 commits and 317 blobs, zero strong
credential matches. The following documentation/evidence-only commit records
this exact SHA and does not change runtime code. Operational 2.x remains blocked
under the strict contract; no native compatibility claim is made.
