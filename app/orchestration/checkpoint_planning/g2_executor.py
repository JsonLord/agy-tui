"""P10 — the G2 UI read-only reviewer runtime executor.

Executes the deterministic G2 sequence for ONE checkpoint on top of the
generic :class:`~app.orchestration.checkpoint_planning.gate_engine.GateEngine`
and the first-class ``GateExecutionRecord`` evidence — the same machinery
P9 proved live for G1 (``g1_executor.py``), reused rather than duplicated:
snapshot guards, bounded evidence assembly, the MiMo Pro transport, and
the one-slice NEEDS_CONTEXT discipline are all imported from G1.

Design rules encoded here (mirroring G1):

* The planner defines what the gate requires; this runtime proves whether
  it passed. The executor is the ONLY writer of gate status (via the
  engine). The reviewer's PASS/FAIL verdict is EVIDENCE — the gatekeeper
  Pro decision is the authority (P9 rule: runtime proves, Pro decides).
* G2 is READ-ONLY review: a ``agy-ui-reviewer`` worker inspects the UI
  surface against ``design_ref`` and emits a JSON verdict. OpenCode agent
  permissions are tool-granular only, so the runtime REPO SNAPSHOT GUARD
  around the dispatch is the authority — any non-artifact file change is
  ``UIReviewerEscapedReadonly`` and the gate is REJECTED (fail closed),
  same philosophy as the G1 author guard.
* NO repair loop in this slice: a failing review REJECTs (its findings
  are preserved for a future bounded-repair slice). GateStatus has no
  BLOCKED yet — escalation is P11/G4 work.
* Fail-closed everywhere: invalid verdict JSON raises UIReviewUnavailable
  with the gate left ACTIVE and resumable; a gatekeeper transport/parse
  failure raises GatekeeperUnavailable with nothing written; a second
  NEEDS_CONTEXT (or an unresolvable slice) REJECTs with a terminal record
  — never silently passed.
* The record's ``green_evidence`` / ``cumulative_gate`` slots carry the
  reviewer run and the guard run (the fixed record schema and the
  engine's PASSED sufficiency predicate require both non-None; G2 has no
  test command — the mechanical shape is reused, their meaning is
  documented at the construction site).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from app.orchestration.checkpoint_planning.g1_executor import (
    GateRuntimeError,
    GatekeeperContextUnavailable,
    GatekeeperDecision,
    GatekeeperUnavailable,
    MiMoProGatekeeper,
    TOOLING_ARTIFACT_PREFIXES,  # noqa: F401 - re-exported for callers/tests
    _changed_paths,
    _extract_json_object,
    _is_tooling_artifact,
    _non_artifact_paths,
    _tail,
    _utc_iso,
    build_implementation_evidence,
)
from app.orchestration.checkpoint_planning.gate_engine import GateEngine
from app.orchestration.checkpoint_planning.gate_evidence import (
    GateExecutionRecord,
    evidence_is_sufficient,
)
from app.orchestration.checkpoint_planning.schemas import (
    APPLICABILITY_REQUIRED,
    Checkpoint,
    GateStatus,
)

G2_GATE_ID = "G2_ui"
ROLE_UI_REVIEWER = "agy-ui-reviewer"

#: role -> OpenCode agent name (mirrors g1_executor.AGENT_FOR_ROLE)
AGENT_FOR_ROLE_G2 = {ROLE_UI_REVIEWER: "agy-ui-reviewer"}

#: ``GateNotApplicable`` is the G2 name for G1's NOT_APPLICABLE refusal —
#: the SAME exception class (G1 raises plain GateRuntimeError there; the
#: contract forbids touching g1_executor.py, so this is a re-export alias,
#: not a new class).
GateNotApplicable = GateRuntimeError


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class UIReviewUnavailable(GateRuntimeError):
    """The reviewer produced no parseable verdict (or never ran).

    Fail-closed: the gate stays ACTIVE, nothing terminal is written, the
    attempt is resumable — an unprovable review is retried, never guessed.
    """


class UIReviewerFailed(GateRuntimeError):
    """The reviewer dispatch itself failed (worker crash / non-ok result)."""


class UIReviewerEscapedReadonly(GateRuntimeError):
    """The read-only reviewer mutated the repo — runtime guard tripped."""


# ---------------------------------------------------------------------------
# Configuration + gatekeeper contract
# ---------------------------------------------------------------------------


@dataclass
class GateConfig2:
    """Static configuration of one G2 execution (pure data, no behavior)."""

    repo: Path
    ui_path: str
    design_ref: str
    #: reserved for the future bounded-repair slice (G2 has no fixer loop
    #: in this slice — a failing review REJECTs; see module docstring).
    max_attempts: int = 2
    clock: Callable[[], float] = time.time


class G2Gatekeeper(Protocol):
    """Structured evidence review by the Pro model (or a fake in tests)."""

    def decide(
        self,
        evidence: GateExecutionRecord,
        prior_repairs: list[GateExecutionRecord],
    ) -> GatekeeperDecision: ...


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def _default_path_snapshot(repo: Path, path_prefix: str) -> dict[str, str]:
    """Content hash of every regular file under ``<repo>/<path_prefix>``.

    The generic (non-test-specific) form of g1's
    ``_default_test_path_snapshot``: derived artifacts are excluded so a
    reviewer merely RUNNING something cannot read as a file change. Used
    for the bounded "UI files reviewed" evidence list; the repo-wide git
    snapshot stays the mutation authority.
    """
    import hashlib

    snapshot: dict[str, str] = {}
    root = Path(repo)
    prefix = str(path_prefix).replace("\\", "/").strip("/")
    target = root / prefix if prefix else root
    if not target.is_dir():
        return snapshot
    for path in sorted(target.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        parts = rel.parts
        if ".git" in parts or any(p in TOOLING_ARTIFACT_PREFIXES for p in parts):
            continue
        if path.suffix in (".pyc", ".pyo"):
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = "unreadable"
        snapshot[str(rel)] = digest
    return snapshot


def _default_repo_snapshot(repo: Path) -> str:
    """Re-export of the G1 git-status authority (same shape, same rules)."""
    from app.orchestration.checkpoint_planning.g1_executor import (
        _default_repo_snapshot as _g1_repo_snapshot,
    )

    return _g1_repo_snapshot(repo)


# ---------------------------------------------------------------------------
# Verdict extraction (fail-closed, same discipline as the G1 gatekeeper)
# ---------------------------------------------------------------------------

_VERDICT_VALUES = ("PASS", "FAIL")


def parse_reviewer_verdict(text: str) -> dict:
    """Extract + validate the reviewer's JSON verdict.

    Reuses G1's ``_extract_json_object`` (whole-object -> fenced ->
    brace-span; NO regex-sloppy parsing), then enforces the verdict shape:
    ``verdict`` must be exactly PASS/FAIL, ``findings`` a list (each
    finding a dict with severity/file/detail strings), ``summary`` a
    string. Anything else raises :class:`UIReviewUnavailable` — the gate
    stays ACTIVE and the attempt is resumable; a malformed review is
    never interpreted, only re-run.
    """
    try:
        parsed = _extract_json_object(text)
    except ValueError as exc:
        raise UIReviewUnavailable(f"reviewer verdict is not a JSON object: {exc}") from exc
    verdict = str(parsed.get("verdict") or "").strip().upper()
    if verdict not in _VERDICT_VALUES:
        raise UIReviewUnavailable(
            f"reviewer verdict must be PASS or FAIL, got {parsed.get('verdict')!r}"
        )
    findings_raw = parsed.get("findings")
    if findings_raw is None:
        findings_raw = []
    if not isinstance(findings_raw, list):
        raise UIReviewUnavailable("reviewer findings must be a list")
    findings: list[dict] = []
    for item in findings_raw:
        if not isinstance(item, dict):
            raise UIReviewUnavailable("reviewer finding must be an object")
        findings.append(
            {
                "severity": str(item.get("severity") or "").strip().lower(),
                "file": str(item.get("file") or ""),
                "detail": str(item.get("detail") or ""),
            }
        )
    return {
        "verdict": verdict,
        "findings": findings,
        "summary": str(parsed.get("summary") or ""),
    }


# ---------------------------------------------------------------------------
# Live gatekeeper (MiMo Pro; same transport as G1, G2 prompt + rules)
# ---------------------------------------------------------------------------

G2_GATEKEEPER_ROLE_RULES = (
    "You are the AGY G2 UI-review gatekeeper. You review STRUCTURED "
    "evidence only: a read-only UI reviewer's verdict and findings "
    "against a design reference, a bounded diff of the UI surface, and "
    "the deterministic reviewer_guard check. Reply with ONLY a JSON "
    "object: {\"decision\": \"ACCEPT|REJECT|NEEDS_CONTEXT\", "
    "\"reason\": str, \"repair_instructions\": str, \"context_request\": "
    "str}. The reviewer verdict is EVIDENCE, not the decision: you are "
    "the authority. ACCEPT only if the evidence shows the UI surface "
    "consistent with the design reference AND the reviewer_guard ok. "
    "REPAIR is NOT a valid G2 decision (there is no fixer loop in this "
    "gate) — a failing review REJECTs, and the findings you name in "
    "repair_instructions inform a future bounded repair slice. "
    "context_request is used ONLY with NEEDS_CONTEXT and must name "
    "exactly ONE bounded slice: a file path as '<path>:full' or a symbol "
    "as '<path>:symbol <name>'. Never request the whole repo. Never "
    "invent facts. Never accept on the reviewer's word alone if the "
    "evidence contradicts the verdict."
)


class MiMoProG2Gatekeeper(MiMoProGatekeeper):
    """Default live G2 gatekeeper: MiMo Pro reviews the structured review.

    Reuses the G1 transport wholesale (constructor, injected ``http``,
    ``response_format: json_object``, ``_extract_json_object`` parse
    discipline, ``_compact_evidence`` bounding) with G2 differences:
    G2 role rules as the system message, the G2 compact prompt, and
    REPAIR not an allowed decision — G2 has no fixer loop in this slice,
    so an ambiguous Pro answer must fail closed rather than silently
    re-dispatch.
    """

    def decide(
        self,
        evidence: GateExecutionRecord,
        prior_repairs: list[GateExecutionRecord],
    ) -> GatekeeperDecision:
        prompt = self._build_prompt(evidence, prior_repairs)
        headers = {"Content-type": "application/json", "Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = "Bearer " + self._api_key
        payload = {
            "model": self._model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": G2_GATEKEEPER_ROLE_RULES},
                {"role": "user", "content": prompt},
            ],
        }
        url = self._base_url + "/chat/completions"
        try:
            status, body = self._http(url, headers, payload, self._timeout)
        except Exception as exc:  # noqa: BLE001 - adapter boundary, fail closed
            raise GatekeeperUnavailable(
                f"g2 gatekeeper transport failed: {exc}"
            ) from exc
        if status != 200:
            raise GatekeeperUnavailable(
                f"g2 gatekeeper transport failed: HTTP {status}: {str(body)[:300]}"
            )
        try:
            envelope = json.loads(body)
            text = envelope["choices"][0]["message"]["content"]
            parsed = _extract_json_object(text)
            decision = str(parsed["decision"]).strip().upper()
            if decision not in ("ACCEPT", "REJECT", "NEEDS_CONTEXT"):
                # fail closed: REPAIR (no fixer loop in this slice) and
                # anything else are invalid G2 answers, never guessed.
                raise ValueError(f"invalid G2 decision {decision!r}")
            context_request = str(parsed.get("context_request") or "")
            if decision == "NEEDS_CONTEXT" and not context_request.strip():
                raise ValueError(
                    "NEEDS_CONTEXT without a context_request is invalid"
                )
        except Exception as exc:  # noqa: BLE001 - fail closed, never guess
            raise GatekeeperUnavailable(
                f"g2 gatekeeper response unparseable: {exc}"
            ) from exc
        session_id = envelope.get("id") if isinstance(envelope, dict) else None
        return GatekeeperDecision(
            decision=decision,
            reason=str(parsed.get("reason") or ""),
            repair_instructions=str(parsed.get("repair_instructions") or ""),
            session_id=str(session_id) if session_id else None,
            context_request=context_request,
        )

    def _build_prompt(
        self,
        evidence: GateExecutionRecord,
        prior_repairs: list[GateExecutionRecord],
    ) -> str:
        doc = evidence.to_dict()
        requirements = doc["requirements"] or {}
        ui_files = requirements.get("ui_files") or []
        compact = {
            "gate_id": doc["gate_id"],
            "checkpoint_id": doc["checkpoint_id"],
            "plan_id": doc["plan_id"],
            "attempt": doc["attempt"],
            "checkpoint_objective": requirements.get("objective", ""),
            "design_ref": requirements.get("design_ref", ""),
            "ui_path": requirements.get("ui_path", ""),
            "ui_files_reviewed": list(ui_files)[:25],
            "focus": requirements.get("focus", []),
            "worker_sessions": doc["worker_sessions"],
            "deterministic_checks": [
                {
                    "name": c.get("name"),
                    "ok": c.get("ok"),
                    "detail": _tail(str(c.get("detail") or ""), 300),
                }
                for c in doc["deterministic_checks"]
                if isinstance(c, dict)
            ],
            "implementation_evidence": self._compact_evidence(
                doc["implementation_evidence"]
            ),
            "prior_repairs": [
                {
                    "attempt": r.attempt,
                    "decision": r.supervisor_decision,
                    "reason": _tail(
                        str((r.requirements or {}).get("last_repair_reason", "")), 200
                    ),
                }
                for r in prior_repairs[:3]
            ],
        }
        return json.dumps(compact, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# The executor
# ---------------------------------------------------------------------------


class G2Executor:
    """Runs the read-only G2 UI-review sequence for one checkpoint."""

    def __init__(
        self,
        repo: Path | str,
        *,
        ui_path: str,
        design_ref: str,
        max_attempts: int = 2,
        dispatch_worker: Callable[[str, str, str], Any] | None = None,
        gatekeeper: G2Gatekeeper | None = None,
        gatekeeper_config: dict | None = None,
        repo_snapshot: Callable[[Path], Any] | None = None,
        ui_path_snapshot: Callable[[Path], Any] | None = None,
        session_capturer: Callable[[str, Any, int], tuple[str | None, str]] | None = None,
        session_read_back: Callable[..., str | None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = GateConfig2(
            repo=Path(repo),
            ui_path=ui_path,
            design_ref=design_ref,
            max_attempts=int(max_attempts),
            clock=clock,
        )
        self._dispatch_worker = dispatch_worker or self._default_dispatch_worker
        self._gatekeeper = gatekeeper
        self._gatekeeper_config = gatekeeper_config
        self._repo_snapshot = repo_snapshot or _default_repo_snapshot
        self._ui_path_snapshot = ui_path_snapshot or self._default_ui_path_snapshot
        self._session_read_back = session_read_back
        self._session_capturer = session_capturer or self._default_session_capturer
        self._clock = clock
        #: every record the executor emitted — the durable evidence trail
        #: the engine references by evidence_refs.
        self.emitted_records: list[GateExecutionRecord] = []
        #: ordered ``(gate_id, from, to, actor)`` transitions observed.
        self.transition_log: list[tuple[str, GateStatus, GateStatus, str]] = []

    # -- seams: live defaults --------------------------------------------------

    def _default_ui_path_snapshot(self, repo: Path) -> dict[str, str]:
        return _default_path_snapshot(repo, self.config.ui_path)

    def _default_dispatch_worker(self, role: str, prompt: str, repo: str) -> Any:
        """Bounded read-only OpenCode reviewer via ``run_supervised``."""
        from app.integrations.external_stack import run_supervised

        agent = AGENT_FOR_ROLE_G2.get(role)
        if agent is None:
            raise GateRuntimeError(f"unknown worker role: {role!r}")
        return run_supervised(repo, prompt, agent=agent, repeat_limit=3)

    def _default_session_capturer(
        self, role: str, result: Any, since_ms: int
    ) -> tuple[str | None, str]:
        """P4-proven capture order (stdout -> single read-back -> none),
        same as G1; the agent name resolves through AGENT_FOR_ROLE_G2.
        Capture never raises and never invents an id."""
        from app.orchestration import agy_session
        from app.integrations.external_stack import OPENCODE_SERVER

        text = str(getattr(result, "output", "") or "")
        try:
            stdout_id = agy_session.extract_session_from_stdout(text)
        except Exception:  # noqa: BLE001 - capture never raises
            stdout_id = None
        if stdout_id:
            return str(stdout_id), agy_session.SOURCE_STDOUT

        agent = AGENT_FOR_ROLE_G2.get(role) or str(role)
        resolve = self._session_read_back
        if resolve is None:
            resolve = agy_session.read_back_session
        try:
            session_id = resolve(
                server=str(OPENCODE_SERVER),
                agent=agent,
                directory=str(self.config.repo),
                since_ms=int(since_ms),
            )
        except Exception:  # noqa: BLE001 - capture never raises
            return None, agy_session.SOURCE_NONE
        return (
            (str(session_id), agy_session.SOURCE_READBACK)
            if session_id
            else (None, agy_session.SOURCE_NONE)
        )

    def _resolve_gatekeeper(self) -> G2Gatekeeper:
        if self._gatekeeper is not None:
            return self._gatekeeper
        if self._gatekeeper_config is not None:
            self._gatekeeper = MiMoProG2Gatekeeper(**self._gatekeeper_config)
            return self._gatekeeper
        raise GatekeeperUnavailable(
            "no gatekeeper available: G2Executor requires an explicit "
            "gatekeeper or an explicit gatekeeper_config (fail closed — a "
            "gate is never silently skipped)"
        )

    # -- P9.2 pattern reused: bounded context slices (NEEDS_CONTEXT) -----------

    def _resolve_context_slice(self, request: str) -> tuple[str, bool]:
        """Resolve ONE ``context_request`` to bounded content.

        Same grammar as G1 (``<path>:full`` / ``<path>:symbol <name>``,
        repo-relative, escapes refused), but the slice cap is 4000 chars
        per the G2 contract. Raises ``GateRuntimeError`` — the caller
        turns that into the terminal NEEDS_CONTEXT rejection.
        """
        req = str(request or "").strip()
        if ":" not in req:
            raise GateRuntimeError(f"context_request is not a bounded slice: {req!r}")
        raw_path, _, selector = req.partition(":")
        selector = selector.strip()
        raw_path = raw_path.strip().replace("\\", "/")
        if not raw_path or not selector:
            raise GateRuntimeError(f"context_request is malformed: {req!r}")
        if (
            raw_path.startswith("/")
            or re.match(r"^[A-Za-z]:", raw_path)
            or ".." in Path(raw_path).parts
        ):
            raise GateRuntimeError(
                f"context_request escapes the repo (absolute or '..'): {req!r}"
            )
        root = Path(self.config.repo).resolve()
        target = (root / raw_path).resolve()
        if root not in target.parents and target != root:
            raise GateRuntimeError(f"context_request escapes the repo: {req!r}")
        if not target.is_file():
            raise GateRuntimeError(f"context_request names no file: {req!r}")

        kind, _, name = selector.partition(" ")
        kind = kind.strip().lower()
        name = name.strip()
        try:
            text = target.read_text(errors="replace")
        except OSError as exc:
            raise GateRuntimeError(f"context file is unreadable: {exc}") from exc

        if kind == "full":
            block = text
        elif kind == "symbol":
            if not name:
                raise GateRuntimeError(f"symbol context_request names no symbol: {req!r}")
            block = self._extract_symbol_block(text, name, req)
        else:
            raise GateRuntimeError(
                "context_request selector must be 'full' or 'symbol <name>'"
                f": {req!r}"
            )
        truncated = len(block) > 4000
        return (block[:4000] if truncated else block), truncated

    @staticmethod
    def _extract_symbol_block(text: str, name: str, req: str) -> str:
        """G1 pattern: definition line through the next same-or-shallower
        indent statement (which ends the block)."""
        pattern = re.compile(
            rf"^(?P<indent>[ \t]*)(?:def|class)[ \t]+{re.escape(name)}\b",
            re.MULTILINE,
        )
        match = pattern.search(text)
        if match is None:
            raise GateRuntimeError(f"context_request symbol not found: {req!r}")
        indent = match.group("indent")
        lines = text[match.start():].splitlines(keepends=True)
        block = [lines[0]]
        for line in lines[1:]:
            if line.strip():
                depth = len(line) - len(line.lstrip())
                if depth <= len(indent):
                    break
            block.append(line)
        return "".join(block)

    # -- engine plumbing ---------------------------------------------------------

    def _advance(
        self,
        engine: GateEngine,
        checkpoint: Checkpoint,
        gate_id: str,
        to: GateStatus,
        *,
        evidence=None,
        actor: str = "runtime",
    ):
        frm = engine.record(gate_id).status
        record = engine.advance(gate_id, to, evidence=evidence, actor=actor)
        self.transition_log.append((gate_id, frm, to, actor))
        # sync live engine state back onto the checkpoint immediately so
        # the gate status is durable even when a guard/step raises later.
        checkpoint.gate_records = dict(engine.records)
        return record

    def _emit(self, record: GateExecutionRecord) -> GateExecutionRecord:
        self.emitted_records.append(record)
        return record

    def _rejected_record(
        self,
        engine: GateEngine,
        checkpoint: Checkpoint,
        base: dict,
        failed_check: str,
        detail: str,
        actor: str = "runtime",
    ) -> GateExecutionRecord:
        checks = [dict(c) for c in base["deterministic_checks"]]
        checks.append({"name": failed_check, "ok": False, "detail": _tail(detail, 800)})
        record = GateExecutionRecord(
            **{**base, "deterministic_checks": checks, "supervisor_decision": None}
        )
        self._emit(record)
        self._advance(engine, checkpoint, G2_GATE_ID, GateStatus.REJECTED, actor=actor)
        return record

    # -- the G2 sequence -----------------------------------------------------------

    def execute_checkpoint(
        self,
        plan_id: str,
        checkpoint: Checkpoint,
        engine: GateEngine | None = None,
    ) -> GateExecutionRecord:
        """Run G2 for ``checkpoint``'s UI gate; return the FINAL record.

        The checkpoint must already carry seeded gate records (same
        precondition as G1). ``engine`` may be supplied by an orchestrator
        running several gates on one checkpoint; it defaults to a fresh
        engine over the seeded records.
        """
        if G2_GATE_ID not in checkpoint.gate_records:
            raise GateRuntimeError(
                "checkpoint gate_records are not seeded — submit the plan "
                "(seed_gate_records) before executing gates"
            )
        if engine is None:
            engine = GateEngine(checkpoint)

        # -- 1. APPLICABILITY (engine never touched when NOT_APPLICABLE) ----
        expectations = checkpoint.gates.G2_ui
        if (
            not expectations.required
            or engine.record(G2_GATE_ID).applicability != APPLICABILITY_REQUIRED
        ):
            raise GateNotApplicable(
                f"{G2_GATE_ID}: plan says NOT_APPLICABLE — nothing for the "
                "UI reviewer to review"
            )

        # -- 2. PENDING -> ACTIVE ---------------------------------------------
        current = engine.record(G2_GATE_ID).status
        if current == GateStatus.PENDING:
            self._advance(engine, checkpoint, G2_GATE_ID, GateStatus.ACTIVE,
                          actor="executor")
        elif current != GateStatus.ACTIVE:
            raise GateRuntimeError(
                f"{G2_GATE_ID} is {current.value} (terminal before the "
                "executor ran); nothing to execute"
            )

        requirements = {
            **expectations.model_dump(mode="json"),
            "objective": checkpoint.objective,
            "design_ref": self.config.design_ref,
            "ui_path": self.config.ui_path,
        }
        started_at = _utc_iso(self._clock())
        base: dict[str, Any] = {
            "plan_id": plan_id,
            "checkpoint_id": checkpoint.checkpoint_id,
            "gate_id": G2_GATE_ID,
            "attempt": 1,
            "requirements": requirements,
            "worker_sessions": [],
            "changed_files": [],
            "deterministic_checks": [],
            "red_evidence": None,
            "green_evidence": None,
            "cumulative_gate": None,
            "supervisor_session_id": None,
            "supervisor_decision": None,
            "repair_lineage": [],
            "timestamps": {"started_at": started_at},
        }

        # -- 3. REVIEWER DISPATCH (read-only) ---------------------------------
        review_prompt = (
            f"Read {self.config.design_ref} and the UI surface under "
            f"{self.config.ui_path}. Produce ONLY a JSON verdict: "
            '{"verdict":"PASS|FAIL","findings":[{"severity":'
            '"major|minor","file":str,"detail":str}],"summary":str}. '
            "You are read-only."
        )
        repo_before = self._repo_snapshot(self.config.repo)
        ui_before = self._ui_path_snapshot(self.config.repo)
        reviewer_since_ms = int(self._clock() * 1000)
        try:
            reviewer_result = self._dispatch_worker(
                ROLE_UI_REVIEWER, review_prompt, str(self.config.repo)
            )
        except GateRuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 - dispatch boundary, resumable
            raise UIReviewerFailed(f"ui-reviewer dispatch failed: {exc}") from exc
        repo_after = self._repo_snapshot(self.config.repo)
        ui_after = self._ui_path_snapshot(self.config.repo)
        reviewer_session, reviewer_source = self._session_capturer(
            ROLE_UI_REVIEWER, reviewer_result, reviewer_since_ms
        )
        reviewer_ok = bool(getattr(reviewer_result, "ok", True))
        base["worker_sessions"].append(
            {
                "role": "ui-reviewer",
                "session_id": reviewer_session,
                "session_source": reviewer_source,
                "ok": reviewer_ok,
            }
        )

        # -- 3b. REPO GUARD (read-only is runtime-enforced authority) --------
        reviewer_changed = _non_artifact_paths(_changed_paths(repo_before, repo_after))
        if reviewer_changed:
            self._rejected_record(
                engine,
                checkpoint,
                base,
                "reviewer_guard",
                f"read-only ui-reviewer changed repo files: {reviewer_changed}",
                actor="reviewer_guard",
            )
            raise UIReviewerEscapedReadonly(
                f"ui-reviewer escaped read-only mode: {reviewer_changed}"
            )
        if not reviewer_ok:
            self._rejected_record(
                engine,
                checkpoint,
                base,
                "reviewer_dispatch",
                "ui-reviewer worker failed",
                actor="reviewer_guard",
            )
            raise UIReviewerFailed(f"{G2_GATE_ID}: ui-reviewer worker failed")

        # -- 3c. VERDICT (invalid -> fail closed, gate stays ACTIVE) ----------
        verdict_doc = parse_reviewer_verdict(
            getattr(reviewer_result, "output", reviewer_result)
        )
        verdict = verdict_doc["verdict"]
        findings = verdict_doc["findings"]

        # -- 4. EVIDENCE RECORD -------------------------------------------------
        ui_files = sorted(ui_before)
        requirements["ui_files"] = ui_files
        impl_evidence = build_implementation_evidence(
            self.config.repo,
            ui_files,
            # "tests" is the never-diffed bucket of the shared evidence
            # builder; G2 feeds it the UI surface, so everything under
            # ui_path lands in product_diff.
            test_path="tests",
        )
        checks = [
            {
                "name": "reviewer_guard",
                "ok": True,
                "detail": "repo snapshot unchanged across reviewer dispatch "
                          "(read-only held; ui tree diff: "
                          f"{_changed_paths(ui_before, ui_after) or 'none'})",
            },
            {
                "name": "ui_review",
                "ok": verdict == "PASS",
                "detail": verdict_doc["summary"] or f"verdict {verdict}",
            },
            {
                "name": "reviewer_findings",
                "ok": verdict == "PASS" and not any(
                    f["severity"] == "major" for f in findings
                ),
                "detail": _tail(json.dumps(findings, default=str), 800),
            },
        ]
        record = GateExecutionRecord(
            **{
                **base,
                "changed_files": reviewer_changed,  # empty — the guard held
                "deterministic_checks": checks,
                "implementation_evidence": impl_evidence,
                # G2 has no test command: the fixed record slots carry the
                # review run (green_evidence) and the guard run
                # (cumulative_gate) so the engine's PASSED sufficiency
                # predicate stays mechanically true-only-on-evidence.
                "green_evidence": {
                    "ok": True,
                    "returncode": 0,
                    "stdout_tail": _tail(json.dumps(verdict_doc, default=str)),
                },
                "cumulative_gate": {
                    "ok": True,
                    "returncode": 0,
                    "stdout_tail": _tail(
                        f"reviewer_guard passed; {len(ui_files)} ui files reviewed"
                    ),
                },
                "timestamps": {**base["timestamps"], "finished_at": _utc_iso(self._clock())},
            }
        )

        # -- 5. GATEKEEPER (the verdict is evidence; the Pro decides) ----------
        gatekeeper = self._resolve_gatekeeper()
        # Fail-closed: if decide() raises, nothing is written and the
        # exception propagates — the gate stays ACTIVE, resumable.
        decision = gatekeeper.decide(record, [])

        # -- 5b. NEEDS_CONTEXT: ONE bounded slice (G1 P9.2 pattern) ------------
        if decision.decision == "NEEDS_CONTEXT":
            slice_err: str | None = None
            try:
                content, slice_truncated = self._resolve_context_slice(
                    decision.context_request
                )
            except GateRuntimeError as exc:
                content, slice_truncated, slice_err = "", False, str(exc)
            if slice_err is None:
                record = GateExecutionRecord(
                    **{
                        **record.to_dict(),
                        "implementation_evidence": {
                            **(record.implementation_evidence or {}),
                            "context_slice": {
                                "request": decision.context_request,
                                "content": content,
                                "truncated": slice_truncated,
                            },
                        },
                    }
                )
                # exactly one re-review; its decision is final
                decision = gatekeeper.decide(record, [])
            if decision.decision == "NEEDS_CONTEXT":
                detail = slice_err or (
                    "gatekeeper still needs context after the single "
                    f"bounded slice {decision.context_request!r}"
                    if decision.context_request
                    else "gatekeeper returned NEEDS_CONTEXT without a "
                         "resolvable context_request"
                )
                terminal_checks = [
                    dict(c) for c in record.deterministic_checks
                ] + [
                    {
                        "name": "context_request",
                        "ok": False,
                        "detail": _tail(
                            f"context request failed closed: {detail}", 800
                        ),
                    }
                ]
                terminal = GateExecutionRecord(
                    **{
                        **record.to_dict(),
                        "deterministic_checks": terminal_checks,
                        "supervisor_decision": "NEEDS_CONTEXT",
                        "supervisor_session_id": decision.session_id,
                    }
                )
                self._emit(terminal)
                self._advance(
                    engine, checkpoint, G2_GATE_ID,
                    GateStatus.REJECTED, actor="gatekeeper",
                )
                raise GatekeeperContextUnavailable(
                    f"{G2_GATE_ID}: NEEDS_CONTEXT could not be satisfied "
                    f"with one bounded slice — gate REJECTED (fail "
                    f"closed): {detail}"
                )

        stamped_checks = [dict(c) for c in record.deterministic_checks]
        if decision.decision == "REJECT":
            stamped_checks.append(
                {
                    "name": "gatekeeper_reject",
                    "ok": False,
                    "detail": _tail(
                        f"{decision.reason} {decision.repair_instructions}".strip(), 800
                    ),
                }
            )
        stamped = GateExecutionRecord(
            **{
                **record.to_dict(),
                "deterministic_checks": stamped_checks,
                "supervisor_decision": decision.decision,
                "supervisor_session_id": decision.session_id,
            }
        )
        if decision.decision == "ACCEPT":
            # The ONLY path to PASSED — and it is impossible without the
            # reviewer evidence (is_passed() requires ACCEPT + all
            # deterministic checks ok, which requires a ran reviewer).
            if not evidence_is_sufficient(G2_GATE_ID, stamped):
                raise GateRuntimeError(
                    "internal invariant violated: ACCEPT decision without "
                    "sufficient reviewer + guard evidence"
                )
            self._emit(stamped)
            self._advance(
                engine, checkpoint, G2_GATE_ID, GateStatus.PASSED,
                evidence=stamped, actor="gatekeeper",
            )
            return stamped
        if decision.decision == "REJECT":
            self._emit(stamped)
            self._advance(engine, checkpoint, G2_GATE_ID,
                          GateStatus.REJECTED, actor="gatekeeper")
            return stamped
        raise GateRuntimeError(
            f"gatekeeper returned invalid G2 decision {decision.decision!r} "
            "(G2 accepts ACCEPT | REJECT | NEEDS_CONTEXT only; fail closed)"
        )


# ---------------------------------------------------------------------------
# Live factory (mirrors the G1 live wiring; no unit test hits real I/O)
# ---------------------------------------------------------------------------


def build_live_g2_executor(
    repo: Path | str,
    *,
    ui_path: str,
    design_ref: str,
    gatekeeper_base_url: str,
    gatekeeper_model: str,
    gatekeeper_api_key: str | None = None,
    gatekeeper_http: Callable[..., tuple[int, str]] | None = None,
    max_attempts: int = 2,
    clock: Callable[[], float] = time.time,
) -> G2Executor:
    """G2 executor wired for LIVE operation.

    - reviewer dispatch: the executor's default seam -> real
      ``run_supervised`` on the ``agy-ui-reviewer`` agent (read-only by
      its OpenCode config; the snapshot guard is the authority);
    - guards: real git-status / content-hash snapshots;
    - sessions: the default P4-proven read-back capturer;
    - decisions: a real :class:`MiMoProG2Gatekeeper` over the Helmholtz
      endpoint (same injected-http transport pattern as G1's live
      gatekeeper).
    """
    return G2Executor(
        repo,
        ui_path=ui_path,
        design_ref=design_ref,
        max_attempts=max_attempts,
        clock=clock,
        # dispatch_worker/repo_snapshot/ui_path_snapshot/session_capturer
        # left as None -> the live defaults above.
        gatekeeper=MiMoProG2Gatekeeper(
            base_url=gatekeeper_base_url,
            model=gatekeeper_model,
            api_key=gatekeeper_api_key,
            http=gatekeeper_http,
        ),
    )
