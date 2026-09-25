# Forensic Recovery Report — `app/investigation/investigator.py`

Status: **EXACT_RECOVERY**

Repository: `/home/leon/antigravity-hybrid` (untouched by this investigation — see §6)
Investigation artifacts: `/tmp/antigravity-deep-recovery/`

---

## 1. Conclusion

The **exact original production source** of `app/investigation/investigator.py`
that generated the surviving bytecode
`app/investigation/__pycache__/investigator.cpython-313.pyc` is recovered as:

- File: `/tmp/antigravity-deep-recovery/investigator.exact.py`
  (identical copy at `/tmp/antigravity-deep-recovery/investigator.reconstructed.py`)
- Size: 3376 bytes
- SHA256: `42742fe39c2c7fa4ae83194acf0da287200f26fee437bdd37df2cefb43cbc07e`
- Git blob OID: `13c9a5eb3f55bdb713ea452e7a77c779bac79f96`
  (present in the repo as a dangling/unreachable loose blob; no tree or commit
  in `main`'s history references it — `git fsck --full` lists it among ~80
  dangling blobs; repo has 141 loose objects, 0 packs)

## 2. Evidence sources (converging, independent)

1. **Surviving `.pyc` (authoritative bytecode).**
   Header: CPython 3.13 magic `f30d0d0a`, timestamp-based invalidation,
   source-mtime field `1790368404` (2026-09-25 22:33:24 CEST),
   **source-size field 3376** — equal to the byte size of blob `13c9a5eb`.

2. **Byte-identical recompilation.**
   `compile(blob_13c9a5eb, "app/investigation/investigator.py", "exec")` →
   unmarshalled pyc code object: **`marshal.dumps` byte-for-byte identical**
   (3485 bytes). This covers module and function: co_names, co_varnames,
   co_consts, full instruction stream (opname+arg), co_lines line table,
   co_positions column table, and co_exceptiontable. No other CPython construct
   can produce the same code object from different source text under the same
   filename — comments/whitespace outside string tokens do not enter code
   objects, but every line/position entry matching pins the physical layout
   token-for-token. This is the strongest possible source-recovery evidence
   short of finding the file itself.

3. **OpenCode session database (write provenance — clinching).**
   `/home/leon/.local/share/opencode/opencode.db` (opened read-only),
   table `part`, row `prt_0da45d5010012dZ2EhU1LsI7P1`:
   a completed `write` tool call by session `ses_f25cae6c3ffefnt3tqA665YIx1`
   to `/home/leon/antigravity-hybrid/app/investigation/investigator.py` whose
   `input.content` is 3376 bytes whose **git hash is exactly
   `13c9a5eb3f55bdb713ea452e7a77c779bac79f96`**, recorded at
   `time_created = 1790368404737` ms = 22:33:24.737 CEST — the same second as
   the pyc's source-mtime field. This directly ties the blob to a completed
   write of the exact file at the exact time the pyc claims its source was
   written.
   An earlier completed write in the same session,
   `prt_0da40b71b001dXscqIz3ww135a` (22:27:49 CEST, git-hash
   `b8c6be06…` = candidate A, 4048 bytes), is the immediately-preceding draft
   — consistent with A being an earlier version and C the final content the
   pyc was compiled from.

4. **Prior recovery artifacts (corroboration).**
   `/tmp/antigravity-recovery/ambiguous/evidence-C.py` is byte-identical to
   blob `13c9a5eb` (sha256 `42742fe3…`) — an earlier recovery pass had already
   extracted this blob but misclassified it as a "test candidate".

## 3. Why the previous pass concluded "zero exact matches"

Earlier comparisons (see `recovery/investigator/compare_pyc.py`) did not
compile candidates under the pyc's recorded `co_filename`
(`app/investigation/investigator.py`) before comparing, and treated the
standalone-function file as the test variant. Recompiled with the correct
filename, `13c9a5eb` matches the pyc down to marshal bytes, and the OpenCode
write log removes any residual doubt. See `candidate-comparison.md` for the
full field-by-field comparison of A / C / test-candidate.

## 4. Documented original-source quirks (NOT corrected, per forensic rules)

- The recovered file contains **no import statements**. At module level the
  only names referenced are the annotation-evaluation globals `str`, `list`,
  `dict`, `Any`, and the function body references `Path`, `re`, `ast` as
  globals. Importing this module standalone in Python 3.13 raises
  `NameError: name 'Any' is not defined` during annotation evaluation of the
  `def` statement itself. The historical module therefore only worked when
  these globals were pre-injected, or (most likely) it was a terminal edit of
  a file that previously had imports and was compiled/`exec`'d in a context
  where the names existed. The pyc proves this exact import-less form existed
  at 22:33:24 CEST. It was faithfully preserved.
- A subsequent, different-generation module (`investigate_repository` +
  private `_extract_source_evidence`, imports intact, relative path field,
  expanded kind list) exists as `recovery/investigator/investigator.recovered.py`
  and in bash_history (line 714 heredoc). That is a **different file state**,
  not the producer of this pyc.

## 5. Recovery status per required schema

**EXACT_RECOVERY**

- Evidence source: unreachable Git blob `13c9a5eb…` + OpenCode DB `write`
  tool-call part `prt_0da45d5010012dZ2EhU1LsI7P1` (session
  `ses_f25cae6c3ffefnt3tqA665YIx1`) + surviving pyc.
- Git object location: `.git/objects/13/c9a5eb3f55bdb713ea452e7a77c779bac79f96`
  (loose, dangling). Session location: `opencode.db` → `part` table, id above.
- SHA256 of recovered source: `42742fe39c2c7fa4ae83194acf0da287200f26fee437bdd37df2cefb43cbc07e`
- Why exact: recompiled under the recorded filename it reproduces the pyc code
  object byte-for-byte (marshal), its size equals the pyc source-size field,
  and the OpenCode store contains the literal content of the completed write
  of this exact file at the exact pyc-recorded mtime.

## 6. Repository integrity

No writes were made to `/home/leon/antigravity-hybrid`. No git mutations
(gc/prune/reset/checkout/restore/stage/commit/ref changes) were performed;
all git access was read-only (`cat-file`, `log`, `ls-tree`, `fsck` without
`--lost-found`). All artifacts are under `/tmp/antigravity-deep-recovery/`.

Note observed during the investigation (not caused by it): new files appeared
in the working tree at 01:44–01:46 CEST under `recovery/investigator/`
(`acceptance_harness.py`, `investigator.recovered.py`, `gm.dump`,
`inv_live.dump`, `acceptance/`) while this investigation ran — apparently a
concurrently running agent. This investigation neither created nor modified
them.

## 7. Artifacts

| File | Content |
|---|---|
| `investigator.exact.py` | Exact recovered source (3376 B, sha256 42742fe3…) |
| `investigator.reconstructed.py` | Identical copy (required artifact name) |
| `investigator.dis.txt` | Full `dis` dump: module + function, consts, names, line tables, exception table |
| `candidate-comparison.md` | Field-by-field A vs C vs test-candidate vs pyc; instruction diff summary |
| `git-object-search.txt` | All-blob keyword enumeration, fsck output, tree search, timestamp/size chain |
| `filesystem-search.txt` | Filesystem + OpenCode DB/log + history search results |
| `hashes.txt` | Preserved hashes/metadata of pyc, blobs, .git/index, .git/HEAD |
| `strict_compare.py`, `compare.py` | Verification scripts used |
| `blobs/` | Extracted candidate blobs and log blobs |

Do not copy the recovered file into the repository unless explicitly requested.
