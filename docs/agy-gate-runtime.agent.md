# agy-test-author — test-only OpenCode worker for the G1 Behavior/TDD gate (AGY P9)

The bounded OpenCode worker the G1 runtime (`app/orchestration/checkpoint_planning/g1_executor.py`)
dispatches at step 2 (TEST AUTHOR). It writes failing tests for the
planner's behavior contract and nothing else — the TDD RED step exists
because of what it produces, and the implementation worker (`agy-dispatch`)
may never touch its output.

- Role: given the G1 `GateExpectations` (planned_tests, test_instructions,
  focus) and the checkpoint objective, create failing tests **only under
  the configured test path** (e.g. `tests/`).
- Dispatched via `run_supervised(..., agent="agy-test-author",
  repeat_limit=3)` (the `dispatch_worker` seam of `G1Executor`).
- Model: MiMo Flash via Helmholtz (`helmholtz/alias-mimo-v2.6-flash`),
  the same provider path the `agy-dispatch` / `agy-planning-analyst`
  agents use.

## OpenCode agent block (add to `opencode.jsonc` under `agent`)

```json
"agy-test-author": {
  "description": "Test-only writer for the AGY G1 Behavior/TDD gate. Creates test files via write; never edits or patches; never writes production code.",
  "mode": "primary",
  "model": "helmholtz/alias-mimo-v2.6-flash",
  "prompt": "You are the AGY test author. Create failing tests ONLY under the test paths given in the task (e.g. tests/). Write complete new test files with the write tool. Never edit or patch any file. Never create, modify, or delete production code. Tests must fail because the behavior is missing (RED), not because of syntax errors.",
  "permission": {"*": "deny", "read": "allow", "write": "allow", "bash": "allow",
                 "glob": "allow", "grep": "allow", "list": "allow",
                 "serena_get_symbols_overview": "allow", "serena_find_symbol": "allow",
                 "serena_find_referencing_symbols": "allow",
                 "edit": "deny", "patch": "deny", "webfetch": "deny",
                 "websearch": "deny"},
  "tools": {"*": false, "read": true, "write": true, "bash": true, "glob": true,
            "grep": true, "list": true,
            "serena_get_symbols_overview": true, "serena_find_symbol": true,
            "serena_find_referencing_symbols": true,
            "edit": false, "patch": false}
}
```

## Path scoping is a RUNTIME authority, not an agent permission

OpenCode agent permissions are **tool-granular only** — there is no way
to say "write, but only under `tests/`". The agent block above denies
`edit`/`patch` (so the author creates files via `write` and cannot edit
production files in place), but that is only a second layer. The
authority is the G1 runtime itself:

1. **AUTHOR GUARD (step 3):** `G1Executor` snapshots the repo
   (`repo_snapshot`, git status + HEAD, same shape as the P8
   zero-mutation guard) before and after the test-author dispatch. If
   ANY changed file lies outside the configured `test_path` prefix, the
   executor raises `TestAuthorEscapedTestPaths` and the gate goes to
   REJECTED — fail closed, never a silent continue.
2. **IMPLEMENT GUARD (step 6):** the test tree is snapshotted by content
   hash (`test_path_snapshot`) at RED time and re-snapshotted after the
   implementer runs. Any test-file change is `TestFileTampered` ->
   REJECTED: an implementer may not rewrite tests to make them pass.

The gate then advances to PASSED only on deterministic evidence (RED +
GREEN + cumulative suite + both guards) **plus** a `MiMoProGatekeeper`
ACCEPT — see `docs` of `g1_executor.py` and the P9 invariants in
`gate_engine.py`.
