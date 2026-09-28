# agy-ui-reviewer — read-only OpenCode worker for the G2 UI gate (AGY P10)

The bounded OpenCode worker the G2 runtime
(`app/orchestration/checkpoint_planning/g2_executor.py`) dispatches for
the UI review step. It reads the design reference (`design.md` /
prototype) and the UI surface under the configured `ui_path`, then emits
**only** a JSON verdict. It never mutates the repo — the same
tool-granular trick as `agy-test-author`, inverted: the author was
write-scoped, this one is read-only.

- Role: given `design_ref` + `ui_path` + the checkpoint objective,
  produce `{"verdict":"PASS|FAIL","findings":[{"severity":"major|minor",
  "file":str,"detail":str}],"summary":str}` as the final output.
- Dispatched via `run_supervised(..., agent="agy-ui-reviewer",
  repeat_limit=3)` (the `dispatch_worker` seam of `G2Executor`).
- Model: MiMo Flash via Helmholtz (`helmholtz/alias-mimo-v2.6-flash`),
  the same provider path the `agy-dispatch` / `agy-test-author` /
  `agy-planning-analyst` agents use.

## OpenCode agent block (add to `opencode.jsonc` under `agent`)

```json
"agy-ui-reviewer": {
  "description": "Read-only UI reviewer for the AGY G2 gate. Inspects the UI surface against the design reference; emits a JSON verdict. Cannot edit, write, patch, or commit anything.",
  "mode": "primary",
  "model": "helmholtz/alias-mimo-v2.6-flash",
  "prompt": "You are the AGY UI reviewer. Read the design reference and the UI surface paths given in the task, then produce ONLY a JSON object: {\"verdict\":\"PASS|FAIL\",\"findings\":[{\"severity\":\"major|minor\",\"file\":\"<path>\",\"detail\":\"<what deviates from the design and why it matters>\"}],\"summary\":\"<one-paragraph assessment>\"}. You are strictly read-only: never create, edit, delete, or run anything that mutates the repository. Judge visual/structural fidelity against the design reference only; never invent files or behaviors you did not read. Your entire final answer must be the JSON object and nothing else.",
  "permission": {"*": "deny", "read": "allow", "glob": "allow",
                 "grep": "allow", "list": "allow",
                 "serena_get_symbols_overview": "allow",
                 "serena_find_symbol": "allow",
                 "serena_find_referencing_symbols": "allow",
                 "bash": "allow",
                 "write": "deny", "edit": "deny", "patch": "deny",
                 "webfetch": "deny", "websearch": "deny"},
  "tools": {"*": false, "read": true, "glob": true, "grep": true,
            "list": true,
            "serena_get_symbols_overview": true, "serena_find_symbol": true,
            "serena_find_referencing_symbols": true,
            "bash": true,
            "write": false, "edit": false, "patch": false}
}
```

`bash` is allowed for **read-only commands only** (rendering a page to
text, `git diff`, `ls`, screenshot tooling). OpenCode has no
argument-granular bash sandbox rule — see below.

## Read-only is a RUNTIME authority, not an agent permission

OpenCode permissions can deny `edit`/`write`/`patch`, but `bash` is
all-or-nothing: a determined model could `echo > file`. The agent block
is only the second layer. The authority is the G2 runtime itself:

1. **REVIEWER GUARD:** `G2Executor` snapshots the repo
   (`repo_snapshot`, git status + HEAD, same shape as the P8/P9
   zero-mutation guards) before and after the reviewer dispatch. If ANY
   non-artifact file appears changed, the executor raises
   `UIReviewerEscapedReadonly` and the gate goes to REJECTED
   (`actor="reviewer_guard"`) — fail closed, never a silent continue.
2. **VERDICT PARSE:** a non-JSON or non-PASS/FAIL verdict raises
   `UIReviewUnavailable` with the gate left ACTIVE and resumable — a
   malformed review is re-run, never interpreted.
3. **DECISION AUTHORITY:** the reviewer's verdict is *evidence*. The
   `MiMoProG2Gatekeeper` decision (ACCEPT | REJECT | NEEDS_CONTEXT with
   exactly one bounded slice) is what passes or fails the gate — the P9
   rule: runtime proves, Pro decides. G2 has no REPAIR in this slice; a
   failing review REJECTs and the findings feed a future bounded repair
   slice.
