# agy-planning-analyst — read-only repository investigator (AGY P8)

The Flash analyst the cloud checkpoint planner (Gemini inside the AGY
CLI; MiMo Pro on fallback) delegates ALL repository understanding to.
It is read-only **by configuration** (permissions/tools below), not by
prompt wording — hard rule of milestone P8.

- Role: answer bounded `AnalysisRequest`s (objective, questions, focus)
  with the structured evidence JSON (`summary`, `relevant_symbols`,
  `dependency_paths`, `existing_tests`, `likely_files`, `constraints`,
  `open_questions`, `source`).
- Dispatched by `app/orchestration/checkpoint_planning/codebase_analysis.py`
  (`codebase_analyze` → one `opencode-supervise` session, agent
  `agy-planning-analyst`). Zero-mutation is additionally verified by a
  git `status --porcelain` + HEAD snapshot taken before and after every
  dispatch (`RepoMutationDetected` on any diff).
- Model: MiMo Flash via Helmholtz (`helmholtz/alias-mimo-v2.6-flash`),
  the same provider path the `agy-dispatch` worker uses.

## OpenCode agent block (add to `opencode.jsonc` under `agent`)

```json
"agy-planning-analyst": {
  "description": "Read-only repository investigator for AGY checkpoint planning (Serena + Graphify). Never mutates.",
  "model": "helmholtz/alias-mimo-v2.6-flash",
  "prompt": "You are the AGY planning analyst. Read-only repository investigation only. Use Graphify for structure and Serena for symbols. Answer the questions, return ONLY the structured JSON evidence object. Never edit, write, commit, or run non-readonly commands.",
  "permission": {"*": "deny", "read": "allow", "bash": "allow", "glob": "allow",
                 "grep": "allow", "list": "allow",
                 "serena_get_symbols_overview": "allow", "serena_find_symbol": "allow",
                 "serena_find_referencing_symbols": "allow",
                 "edit": "deny", "write": "deny", "patch": "deny", "webfetch": "deny",
                 "websearch": "deny"},
  "tools": {"*": false, "read": true, "bash": true, "glob": true, "grep": true,
            "list": true, "serena_get_symbols_overview": true, "serena_find_symbol": true,
            "serena_find_referencing_symbols": true, "edit": false, "write": false, "patch": false}
}
```

Note vs. the shipped `agy-dispatch` block: the sibling agents there also
carry `"mode": "primary"`. The contract block above is authoritative;
if the live OpenCode server refuses to load the agent, `"mode":
"primary"` is the only extra key to add (the `agy-dispatch*` /
`agy-supervisor*` agents all use it). Keep the permission model
strictly read-only in every case.

## Serena + Graphify strategy (what the dispatch prompt enforces)

1. **Graphify first** for anything structural — architecture,
   dependency/caller questions, test impact, "where does X live":
   - If `graphify-out/graph.json` exists, use read-only queries
     against it: `graphify query "<task>"`, `graphify path A B`,
     `graphify explain "X"` (default `--graph graphify-out/graph.json`).
   - NEVER `graphify update|watch|install|add|clone` — those write
     `graphify-out/` or fetch content. The graph may be slightly stale;
     staleness is acceptable for planning evidence and mutation is not.
2. **Serena next** for exact code facts: `serena_get_symbols_overview`
   (module outline), `serena_find_symbol` (definitions/signatures),
   `serena_find_referencing_symbols` (call sites).
3. **Native `read`/`grep`/`glob`/`list` last**, only inside areas
   already narrowed by 1–2. `bash` is granted so the read-only
   graphify/git-introspection commands above remain reachable; the
   analyst must never run anything that writes (no `git add/commit/
   checkout`, no redirection into files, no test runs that create
   artifacts).
4. **Answer shape**: exactly one JSON evidence object — no prose, no
   markdown fences needed. Ground every list entry in evidence
   actually observed; prefer empty lists over guesses. Uncertainty goes
   into `open_questions`, never into fabricated `likely_files`.

## Bounded outputs

`codebase_analysis.evidence_from_payload` hard-caps what even a
well-behaved analyst can return: summary ≤ 2000 chars, ≤ 25 items per
list, ≤ 500 chars per item. Planning context therefore stays bounded
even if the agent misbehaves (`mcp_tools.agy_codebase_analyze_tool`
returns this dict to the caller).
