# agy-adversarial-reviewer + agy-fixer — the two G3 Adversarial gate workers (AGY P10)

The bounded OpenCode workers the G3 runtime
(`app/orchestration/checkpoint_planning/g3_executor.py`) dispatches in its
review/fix loop: the **read-only adversarial reviewer** produces structured
JSON findings every cycle; the **bounded fixer** applies only the gatekeeper's
repair instructions, only inside the checkpoint's `relevant_files`, never to
tests. Both follow the conventions of the `agy-test-author` block in
`docs/agy-gate-runtime.agent.md`.

- Dispatched via `run_supervised(..., agent="agy-adversarial-reviewer" |
  "agy-fixer", repeat_limit=3)` (the `dispatch_worker` seam of `G3Executor`;
  the roles ARE the agent names, see `AGENT_FOR_ROLE`).
- Model: MiMo Flash via Helmholtz (`helmholtz/alias-mimo-v2.6-flash`), the
  same provider path `agy-test-author` / `agy-dispatch` use.
- Authority split: the reviewer verdict is EVIDENCE only — the MiMo Pro
  gatekeeper (`MiMoProG3Gatekeeper`) decides ACCEPT/REPAIR/REJECT/
  NEEDS_CONTEXT; PASSED happens only on Pro ACCEPT with the deterministic
  regression check ok.

## OpenCode agent blocks (add to `opencode.jsonc` under `agent`)

```json
"agy-adversarial-reviewer": {
  "description": "Read-only adversarial reviewer for the AGY G3 gate. Produces ONLY a structured JSON findings document; never mutates the repo.",
  "mode": "primary",
  "model": "helmholtz/alias-mimo-v2.6-flash",
  "prompt": "You are the AGY adversarial reviewer. Review the code you are pointed at for failure modes, edge cases, and security holes. You are strictly READ-ONLY: never create, edit, or delete any file; bash only for read-only inspection. Produce ONLY a JSON document — no prose, no markdown: {\"verdict\": \"PASS|FAIL\", \"findings\": [{\"severity\": \"critical|major|minor\", \"file\": str, \"line\": int|null, \"detail\": str}], \"summary\": str}. At most 25 findings, most severe first. Every finding must be grounded in code you actually read — never invent file paths, line numbers, or behaviour. critical = exploitable or data-losing defect; major = realistic correctness failure on an edge case; minor = robustness/maintainability risk. verdict FAIL iff at least one critical or major finding exists.",
  "permission": {"*": "deny", "read": "allow", "glob": "allow", "grep": "allow",
                 "list": "allow", "bash": "allow",
                 "serena_get_symbols_overview": "allow", "serena_find_symbol": "allow",
                 "serena_find_referencing_symbols": "allow",
                 "write": "deny", "edit": "deny", "patch": "deny",
                 "webfetch": "deny", "websearch": "deny"},
  "tools": {"*": false, "read": true, "glob": true, "grep": true, "list": true,
            "bash": true,
            "serena_get_symbols_overview": true, "serena_find_symbol": true,
            "serena_find_referencing_symbols": true,
            "write": false, "edit": false, "patch": false}
}
```

```json
"agy-fixer": {
  "description": "Bounded fixer for the AGY G3 gate. Applies ONLY the given repair instructions inside the given file scope; never touches tests.",
  "mode": "primary",
  "model": "helmholtz/alias-mimo-v2.6-flash",
  "prompt": "You are the AGY bounded fixer. You receive explicit repair instructions and the critical/major findings to address. Modify ONLY the files inside the scope you were given. NEVER touch test files (anything under tests/ or named test_*.py). No new dependencies. No behaviour beyond what the findings require — you fix, you do not redesign. Do not run or rewrite the test suite; the runtime re-checks deterministically after you.",
  "permission": {"*": "deny", "edit": "allow", "write": "allow", "patch": "allow",
                 "read": "allow", "glob": "allow", "grep": "allow", "list": "allow",
                 "bash": "allow",
                 "webfetch": "deny", "websearch": "deny"},
  "tools": {"*": false, "edit": true, "write": true, "patch": true, "read": true,
            "glob": true, "grep": true, "list": true, "bash": true,
            "webfetch": false, "websearch": false}
}
```

## Scope + test integrity are RUNTIME authority, not agent permissions

OpenCode agent permissions are **tool-granular only** — there is no way to
say "write, but only within `relevant_files`" or "never under `tests/`". The
agent blocks above (read-only reviewer, no-web fixer) are a second layer.
The authority is the G3 executor itself:

1. **REVIEWER GUARD:** `G3Executor` snapshots the repo (`repo_snapshot`,
   git status + HEAD, same shape as the P8 zero-mutation guard) before and
   after every reviewer dispatch. Any non-artifact change is
   `ReviewerMutatedRepo` -> gate REJECTED (fail closed).
2. **FIXER TEST-INTEGRITY GUARD:** the test tree is content-hashed
   (`test_path_snapshot`) before and after the fixer; any test-file change
   is `TestFileTampered` (the G1 exception, reused) -> REJECTED.
3. **FIXER SCOPE GUARD:** product-file changes outside the checkpoint's
   `relevant_files` are `FixerEscapedScope` -> REJECTED — the planner
   template's `fixer_policy.scope` enforced at runtime.
4. **BOUNDED LOOP + DETERMINISTIC NET:** at most
   `MAX_REVIEW_FIX_CYCLES` (=3) review/fix cycles; the test command
   re-runs EVERY cycle (`regression` check), and the gate's ACCEPT
   invariant makes passing over a failed regression impossible. Budget
   exhaustion REJECTs with the `fixer_cycles` escalation check
   (escalation lands in P11 — `GateStatus` is P9-frozen, no BLOCKED).
