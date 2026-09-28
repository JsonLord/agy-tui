"""P10 — the G3 Adversarial gate runtime executor.

Executes the G3 sequence for ONE checkpoint on top of the generic
:class:`~app.orchestration.checkpoint_planning.gate_engine.GateEngine`
and the first-class ``GateExecutionRecord``, reusing the P9/P9.2 G1
machinery (helpers are IMPORTED from ``g1_executor``, never duplicated;
G1 itself is untouched).

Gate template (planner ``gate_templates["G3_adversarial"]``): an
adversarial reviewer (read-only, structured findings) faces a bounded
fixer (``bounded_fixer``, ``max_cycles = MAX_REVIEW_FIX_CYCLES``, scope
"fixer writes only within checkpoint relevant_files", escalation after
budget exhaustion). Design rules encoded here:

* The reviewer is READ-ONLY authority: any non-artifact repo change
  around a reviewer dispatch is ``ReviewerMutatedRepo`` -> REJECTED
  (same philosophy as the G1 author guard).
* The reviewer verdict is EVIDENCE, not the decision. The MiMo Pro
  gatekeeper (``MiMoProG3Gatekeeper``, identical transport/decision
  contract as G1's ``MiMoProGatekeeper``) holds ACCEPT/REPAIR/REJECT/
  NEEDS_CONTEXT authority. PASSED happens ONLY on Pro ACCEPT, and only
  when the deterministic invariant holds (regression ok + all checks
  ok + green/cumulative evidence — ``is_passed``).
* Deterministic regression re-check EVERY cycle: a fix that breaks the
  test command is *evidence* (``regression`` check ok=False), never
  approval material — the invariant makes ACCEPT impossible over it.
* The fixer is scope-bound at RUNTIME (OpenCode permissions are
  tool-granular only): any test-file change -> ``TestFileTampered``
  (imported from G1) -> REJECTED; any product change outside the
  checkpoint's ``relevant_files`` -> ``FixerEscapedScope`` -> REJECTED.
* Cycle budget: Pro REPAIR may trigger at most ``MAX_FIX_CYCLES``
  reviews/fixes. GateStatus (P9-frozen) has no BLOCKED, so exhaustion
  is REJECTED (actor "gatekeeper") carrying the deterministic check
  ``{"name": "fixer_cycles", "ok": False, "detail": "fixer cycle
  budget exhausted (3) — escalation pending (P11)"}`` — the record and
  engine state carry the escalation intent for P11.
* Fail-closed everywhere: malformed reviewer JSON raises
  ``AdversarialReviewUnavailable`` with the gate left ACTIVE and
  nothing terminal written — the attempt is resumable (re-call
  ``execute_checkpoint`` on the same checkpoint).

No network in unit tests: every external effect is an injectable seam
(``run_test``, ``dispatch_worker``, ``gatekeeper``, ``session_capturer``,
``repo_snapshot``, ``test_path_snapshot``, ``sentinel``) mirroring G1 so
the live factory (:func:`build_live_g3_executor`) reuses the same wiring.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from app.orchestration.checkpoint_planning.g1_executor import (
    G1Executor,
    GateConfig,
    GateRuntimeError,
    GatekeeperContextUnavailable,
    GatekeeperDecision,
    GatekeeperUnavailable,
    TestFileTampered,
    _changed_paths,
    _default_repo_snapshot,
    _default_test_path_snapshot,
    _extract_json_object,
    _is_tooling_artifact,
    _non_artifact_paths,
    _tail,
    _under_test_path,
    _utc_iso,
    build_implementation_evidence,
)
from app.orchestration.checkpoint_planning.gate_evidence import (
    GateExecutionRecord,
    evidence_is_sufficient,
)
from app.orchestration.checkpoint_planning.gate_engine import GateEngine
from app.orchestration.checkpoint_planning.planner import MAX_REVIEW_FIX_CYCLES
from app.orchestration.checkpoint_planning.schemas import (
    APPLICABILITY_REQUIRED,
    Checkpoint,
    GateStatus,
)

G3_GATE_ID = "G3_adversarial"

#: Worker roles double as the OpenCode agent names (docs/agy-adversarial-reviewer.agent.md).
ROLE_ADVERSARIAL_REVIEWER = "agy-adversarial-reviewer"
ROLE_FIXER = "agy-fixer"

AGENT_FOR_ROLE = {
    ROLE_ADVERSARIAL_REVIEWER: ROLE_ADVERSARIAL_REVIEWER,
    ROLE_FIXER: ROLE_FIXER,
}

#: Single source of truth: the planner's review/fix cycle budget.
MAX_FIX_CYCLES = MAX_REVIEW_FIX_CYCLES

#: Hard cap on reviewer findings (contract: "max 25 findings").
MAX_FINDINGS = 25

#: G3 context slices are capped tighter than G1's 8000-char internal bound.
CONTEXT_SLICE_MAX_CHARS = 4000

_SEVERITY_RANK = {"critical": 0, "major": 1, "minor": 2}

_ESCALATION_DETAIL = (
    f"fixer cycle budget exhausted ({MAX_FIX_CYCLES}) — "
    "escalation pending (P11)"
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AdversarialReviewUnavailable(GateRuntimeError):
    """The reviewer could not produce a schema-valid findings document.

    Fail-closed: the gate stays ACTIVE, nothing terminal is written, and
    the attempt is resumable (same discipline as ``GatekeeperUnavailable``).
    """


class FixerEscapedScope(GateRuntimeError):
    """The fixer worker wrote a product file outside the checkpoint's
    ``relevant_files`` — the template's scope rule enforced at runtime."""


class ReviewerMutatedRepo(GateRuntimeError):
    """The read-only reviewer changed the repo — its authority is
    strictly observational (G1 author-guard philosophy)."""


# ---------------------------------------------------------------------------
# Reviewer findings: extraction + strict validation + sorting
# ---------------------------------------------------------------------------


def _extract_balanced_objects(text: str) -> list[str]:
    """Top-level balanced ``{...}`` substrings, outermost first."""
    out: list[str] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(str(text or "")):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    out.append(str(text)[start : i + 1])
    return out


def _coerce_line(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("finding line must be an int or null")
    if isinstance(value, int):
        line = value
    elif isinstance(value, str) and value.strip().isdigit():
        line = int(value.strip())
    else:
        raise ValueError("finding line must be an int or null")
    if line < 1:
        raise ValueError("finding line must be >= 1")
    return line


def _validate_finding(entry: Any) -> dict:
    if not isinstance(entry, dict):
        raise ValueError("finding must be an object")
    severity = str(entry.get("severity") or "").strip().lower()
    if severity not in _SEVERITY_RANK:
        raise ValueError(
            f"finding severity must be one of {sorted(_SEVERITY_RANK)}, "
            f"got {entry.get('severity')!r}"
        )
    file_ = entry.get("file")
    if not isinstance(file_, str) or not file_.strip():
        raise ValueError("finding file must be a non-empty string")
    detail = entry.get("detail")
    if not isinstance(detail, str) or not detail.strip():
        raise ValueError("finding detail must be a non-empty string")
    return {
        "severity": severity,
        "file": file_.strip().replace("\\", "/"),
        "line": _coerce_line(entry.get("line")),
        "detail": detail,
    }


def sort_findings(findings: list[dict]) -> list[dict]:
    """Severity-sorted (critical > major > minor), stable within a tier."""
    return sorted(
        findings, key=lambda f: (_SEVERITY_RANK.get(f["severity"], 9), f["file"])
    )


def parse_review_document(text: str) -> dict:
    """Parse + validate the reviewer's JSON-only answer (fail closed).

    Contract: ``{"verdict": "PASS|FAIL", "findings": [{"severity":
    "critical|major|minor", "file": str, "line": int|null, "detail":
    str}], "summary": str}`` — max 25 findings, no prose. Every object
    in the text (outermost balanced braces first) is tried; the FIRST
    one that validates wins; none validating -> ``ValueError``. Findings
    come back severity-sorted and capped.
    """
    candidates = []
    try:
        candidates.append(json.loads(str(text or "")))
    except (ValueError, TypeError):
        pass
    for chunk in _extract_balanced_objects(text):
        try:
            candidates.append(json.loads(chunk))
        except (ValueError, TypeError):
            continue
    first_error: ValueError | None = None
    for cand in candidates:
        try:
            if not isinstance(cand, dict):
                raise ValueError("review document must be a JSON object")
            verdict = str(cand.get("verdict") or "").strip().upper()
            if verdict not in ("PASS", "FAIL"):
                raise ValueError(
                    f"review verdict must be PASS or FAIL, got {cand.get('verdict')!r}"
                )
            raw_findings = cand.get("findings")
            if raw_findings is None:
                raw_findings = []
            if not isinstance(raw_findings, list):
                raise ValueError("review findings must be a list")
            findings = sort_findings([_validate_finding(f) for f in raw_findings])
            summary = cand.get("summary")
            if summary is None:
                summary = ""
            if not isinstance(summary, str):
                raise ValueError("review summary must be a string")
            return {
                "verdict": verdict,
                "findings": findings[:MAX_FINDINGS],
                "summary": summary,
                "truncated": len(findings) > MAX_FINDINGS,
            }
        except ValueError as exc:
            if first_error is None:
                first_error = exc
            continue
    raise ValueError(
        "no schema-valid adversarial review document in reviewer output"
        + (f": {first_error}" if first_error else "")
    )


# ---------------------------------------------------------------------------
# Gatekeeper (MiMo Pro authority; reviewer verdict is only evidence)
# ---------------------------------------------------------------------------

G3_GATEKEEPER_ROLE_RULES = (
    "You are the AGY G3 Adversarial gatekeeper. You review STRUCTURED "
    "evidence only: the checkpoint objective, a bounded implementation "
    "diff, the adversarial reviewer's severity-sorted findings, the "
    "deterministic regression checks, and the repair instructions from "
    "prior cycles. Reply with ONLY a JSON object: "
    '{\"decision\": \"ACCEPT|REPAIR|REJECT|NEEDS_CONTEXT\", \"reason\": str, '
    '"repair_instructions\": str, \"context_request\": str}. The '
    "reviewer verdict is EVIDENCE, not the decision — YOU decide. ACCEPT "
    "only if no open critical/major finding remains AND every "
    "deterministic check is ok (a failed regression check can NEVER be "
    "accepted). REPAIR requires concrete repair_instructions naming the "
    "findings to fix within the checkpoint's relevant_files scope. REJECT "
    "when the findings are fatal or the fixer cycles produced no path to "
    "correctness. context_request is used ONLY with NEEDS_CONTEXT and must "
    "name exactly ONE bounded slice: a file path as '<path>:full' or a "
    "symbol as '<path>:symbol <name>'. Never request the whole repo. "
    "Never invent facts."
)


class G3Gatekeeper(Protocol):
    """Protocol-style base: structured evidence review -> decision."""

    def decide(
        self,
        evidence: GateExecutionRecord,
        prior_repairs: list[GateExecutionRecord],
    ) -> GatekeeperDecision: ...


class MiMoProG3Gatekeeper:
    """Default live G3 gatekeeper: MiMo Pro over the Helmholtz endpoint.

    Same transport pattern as ``MiMoProGatekeeper`` (injected ``http``
    callable, ``response_format: json_object``, fail-closed
    ``GatekeeperUnavailable`` on any transport/parse error) and the
    IDENTICAL decision JSON contract. The prompt is COMPACT structured
    JSON — bounded diffs and severity-sorted findings, never raw worker
    transcripts (the proxy has a ~60s upstream-read timeout on Pro).
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        http: Callable[..., tuple[int, str]] | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._http = http or self._urllib_http
        self._timeout = timeout

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
                {"role": "system", "content": G3_GATEKEEPER_ROLE_RULES},
                {"role": "user", "content": prompt},
            ],
        }
        url = self._base_url + "/chat/completions"
        try:
            status, body = self._http(url, headers, payload, self._timeout)
        except Exception as exc:  # noqa: BLE001 - adapter boundary, fail closed
            raise GatekeeperUnavailable(
                f"G3 gatekeeper transport failed: {exc}"
            ) from exc
        if status != 200:
            raise GatekeeperUnavailable(
                f"G3 gatekeeper transport failed: HTTP {status}: {str(body)[:300]}"
            )
        try:
            envelope = json.loads(body)
            text = envelope["choices"][0]["message"]["content"]
            parsed = _extract_json_object(text)
            decision = str(parsed["decision"]).strip().upper()
            if decision not in ("ACCEPT", "REPAIR", "REJECT", "NEEDS_CONTEXT"):
                raise ValueError(f"invalid decision {decision!r}")
            context_request = str(parsed.get("context_request") or "")
            if decision == "NEEDS_CONTEXT" and not context_request.strip():
                # fail closed: never guess what Pro wanted to see
                raise ValueError(
                    "NEEDS_CONTEXT without a context_request is invalid"
                )
        except Exception as exc:  # noqa: BLE001 - fail closed, never guess
            raise GatekeeperUnavailable(
                f"G3 gatekeeper response unparseable: {exc}"
            ) from exc
        session_id = envelope.get("id") if isinstance(envelope, dict) else None
        return GatekeeperDecision(
            decision=decision,
            reason=str(parsed.get("reason") or ""),
            repair_instructions=str(parsed.get("repair_instructions") or ""),
            session_id=str(session_id) if session_id else None,
            context_request=context_request,
        )

    # -- internals ---------------------------------------------------------

    def _build_prompt(
        self,
        evidence: GateExecutionRecord,
        prior_repairs: list[GateExecutionRecord],
    ) -> str:
        doc = evidence.to_dict()
        impl = doc.get("implementation_evidence")
        review_verdict = review_summary = None
        if isinstance(impl, dict):
            review_verdict = impl.get("review_verdict")
            review_summary = impl.get("review_summary")
            impl = {
                "git_stat": _tail(str(impl.get("git_stat") or ""), 2000),
                # HEAD of the diff (not the G1-style tail): the leading
                # lines are the per-file headers — where the change lives.
                "product_diff": str(impl.get("product_diff") or "")[:4000],
                "test_files": list(impl.get("test_files") or [])[:25],
                "changed_symbols": list(impl.get("changed_symbols") or [])[:50],
                "truncated": bool(impl.get("truncated")),
            }
            if isinstance(impl_slice := doc.get("implementation_evidence", {}).get("context_slice"), dict):
                impl["context_slice"] = {
                    "request": str(impl_slice.get("request") or ""),
                    "content": _tail(str(impl_slice.get("content") or ""), 4000),
                    "truncated": bool(impl_slice.get("truncated")),
                }
        findings = sort_findings([
            {
                "severity": str(f.get("severity") or "").strip().lower(),
                "file": f.get("file"),
                "line": f.get("line"),
                "detail": _tail(str(f.get("detail") or ""), 400),
            }
            for f in ((doc.get("implementation_evidence") or {}).get(
                "adversarial_findings"
            ) or [])[:MAX_FINDINGS]
            if isinstance(f, dict) and str(f.get("severity") or "").lower()
            in _SEVERITY_RANK
        ])
        compact = {
            "gate_id": doc["gate_id"],
            "checkpoint_id": doc["checkpoint_id"],
            "plan_id": doc["plan_id"],
            "attempt": doc["attempt"],
            "repair_lineage": doc["repair_lineage"],
            "objective": _tail(str(doc.get("requirements", {}).get("objective") or ""), 500),
            "review_verdict": review_verdict,
            "review_summary": _tail(str(review_summary or ""), 1500),
            "reviewer_findings": findings,
            "deterministic_checks": [
                {
                    "name": c.get("name"),
                    "ok": c.get("ok"),
                    "detail": _tail(str(c.get("detail") or ""), 300),
                }
                for c in doc["deterministic_checks"]
                if isinstance(c, dict)
            ],
            "changed_files": doc["changed_files"][:25],
            "implementation_evidence": impl,
            "green_evidence": self._compact_run(doc["green_evidence"]),
            "cumulative_gate": self._compact_run(doc["cumulative_gate"]),
            "prior_repairs": [
                {
                    "attempt": r.attempt,
                    "decision": r.supervisor_decision,
                    "reason": _tail(
                        str((r.requirements or {}).get("last_repair_reason", "")), 200
                    ),
                    "repair_instructions": _tail(
                        str((r.requirements or {}).get("last_repair_instructions", "")),
                        400,
                    ),
                }
                for r in prior_repairs[:3]
            ],
        }
        return json.dumps(compact, sort_keys=True, default=str)

    @staticmethod
    def _compact_run(run: dict | None) -> dict | None:
        if not isinstance(run, dict):
            return None
        return {
            "ok": run.get("ok"),
            "returncode": run.get("returncode"),
            "stdout_tail": _tail(run.get("stdout_tail") or run.get("stdout") or ""),
            "stderr_tail": _tail(run.get("stderr_tail") or run.get("stderr") or ""),
        }

    @staticmethod
    def _urllib_http(
        url: str, headers: dict, payload: dict, timeout: float
    ) -> tuple[int, str]:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# The executor
# ---------------------------------------------------------------------------


class G3Executor:
    """Runs the bounded adversarial review/fix loop for one checkpoint.

    Seams mirror ``G1Executor`` exactly (``run_test``,
    ``dispatch_worker``, ``gatekeeper``, ``session_capturer``,
    ``repo_snapshot``, ``test_path_snapshot``, ``clock``, plus the
    ``full_suite_command`` / ``test_command`` pair) so the live factory
    reuses the same wiring.
    """

    def __init__(
        self,
        repo: Path | str,
        *,
        test_path: str,
        test_command: list[str],
        full_suite_command: list[str] | None = None,
        max_cycles: int = MAX_FIX_CYCLES,
        long_run_threshold_s: float = 30.0,
        run_test: Callable[[list[str], str], tuple[int, str, str]] | None = None,
        dispatch_worker: Callable[[str, str, str], Any] | None = None,
        gatekeeper: G3Gatekeeper | None = None,
        sentinel: Any | None = None,
        session_capturer: Callable[[str, Any, int], tuple[str | None, str]] | None = None,
        session_read_back: Callable[..., str | None] | None = None,
        clock: Callable[[], float] = time.time,
        test_path_snapshot: Callable[[Path], Any] | None = None,
        repo_snapshot: Callable[[Path], Any] | None = None,
        gatekeeper_config: dict | None = None,
    ) -> None:
        self.config = GateConfig(
            repo=Path(repo),
            test_path=test_path,
            test_command=list(test_command),
            full_suite_command=list(full_suite_command or test_command),
            max_attempts=int(max_cycles),
            long_run_threshold_s=float(long_run_threshold_s),
            clock=clock,
        )
        self._max_cycles = int(max_cycles)
        self._run_test = run_test or self._default_run_test
        self._dispatch_worker = dispatch_worker or self._default_dispatch_worker
        self._gatekeeper = gatekeeper
        self._gatekeeper_config = gatekeeper_config
        self._sentinel = sentinel
        self._session_read_back = session_read_back
        self._session_capturer = session_capturer or self._default_session_capturer
        self._clock = clock
        self._test_path_snapshot = test_path_snapshot or _default_test_path_snapshot
        self._repo_snapshot = repo_snapshot or _default_repo_snapshot
        #: every record the executor emitted (terminal or per-cycle)
        self.emitted_records: list[GateExecutionRecord] = []
        #: ordered ``(gate_id, from, to, actor)`` transitions observed
        self.transition_log: list[tuple[str, GateStatus, GateStatus, str]] = []

    # -- seams: live defaults (NOT exercised in unit tests) ------------------

    def _default_run_test(self, cmd: list[str], cwd: str) -> tuple[int, str, str]:
        """Plain subprocess run; long runs hand off to the P7 sentinel
        exactly once (models never own wait loops — same as G1)."""
        proc = subprocess.run(
            list(cmd),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    def _default_dispatch_worker(self, role: str, prompt: str, repo: str) -> Any:
        """Bounded OpenCode worker via ``run_supervised`` (repeat_limit=3,
        per the P8.1 fix). Roles ARE the agent names (docs/agy-adversarial-reviewer.agent.md)."""
        from app.integrations.external_stack import run_supervised

        agent = AGENT_FOR_ROLE.get(role)
        if agent is None:
            raise GateRuntimeError(f"unknown worker role: {role!r}")
        return run_supervised(repo, prompt, agent=agent, repeat_limit=3)

    def _default_session_capturer(
        self, role: str, result: Any, since_ms: int
    ) -> tuple[str | None, str]:
        """P4-proven capture order (stdout -> server read-back -> none),
        G3 role table; capture never raises and never invents an id."""
        from app.orchestration import agy_session
        from app.integrations.external_stack import OPENCODE_SERVER

        text = str(getattr(result, "output", "") or "")
        try:
            stdout_id = agy_session.extract_session_from_stdout(text)
        except Exception:  # noqa: BLE001 - capture never raises
            stdout_id = None
        if stdout_id:
            return str(stdout_id), agy_session.SOURCE_STDOUT

        agent = AGENT_FOR_ROLE.get(role)
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

    def _resolve_gatekeeper(self) -> G3Gatekeeper:
        if self._gatekeeper is not None:
            return self._gatekeeper
        if self._gatekeeper_config is not None:
            self._gatekeeper = MiMoProG3Gatekeeper(**self._gatekeeper_config)
            return self._gatekeeper
        raise GatekeeperUnavailable(
            "no gatekeeper available: G3Executor requires an explicit "
            "gatekeeper or an explicit gatekeeper_config (fail closed — a "
            "gate is never silently skipped)"
        )

    # -- context slices (reuse G1's resolution, G3 cap applied here) ---------

    #: G1's resolver dispatches ``self._extract_symbol_block`` — alias the
    #: imported G1 static helper so the unbound reuse works verbatim.
    _extract_symbol_block = staticmethod(G1Executor._extract_symbol_block)

    def _resolve_context_slice(self, request: str) -> tuple[str, bool]:
        # Reuse G1's resolver UNBOUND (imported, not duplicated): its body
        # only reads ``self.config.repo`` — a structural dependency G3
        # satisfies identically via the shared GateConfig.
        content, truncated = G1Executor._resolve_context_slice(self, request)
        if len(content) > CONTEXT_SLICE_MAX_CHARS:
            return content[:CONTEXT_SLICE_MAX_CHARS], True
        return content, truncated

    # -- engine plumbing (identical bookkeeping to G1) ------------------------

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
        checkpoint.gate_records = dict(engine.records)
        return record

    def _emit(self, record: GateExecutionRecord) -> GateExecutionRecord:
        self.emitted_records.append(record)
        return record

    def _rejected_record(
        self,
        engine: GateEngine,
        checkpoint: Checkpoint,
        gate_id: str,
        base: dict,
        failed_check: str,
        detail: str,
    ) -> GateExecutionRecord:
        checks = [dict(c) for c in base["deterministic_checks"]]
        checks.append({"name": failed_check, "ok": False, "detail": _tail(detail, 800)})
        record = GateExecutionRecord(
            **{**base, "deterministic_checks": checks, "supervisor_decision": None},
        )
        self._emit(record)
        self._advance(engine, checkpoint, gate_id, GateStatus.REJECTED,
                      actor="runtime")
        return record

    # -- prompts ---------------------------------------------------------------

    def _reviewer_prompt(self, checkpoint: Checkpoint) -> str:
        scope = (
            f"ONLY these files (the checkpoint's relevant_files): "
            f"{json.dumps(checkpoint.relevant_files)}"
            if checkpoint.relevant_files
            else "the whole repository (the checkpoint declares no "
            "relevant_files boundary)"
        )
        focus = json.dumps(
            checkpoint.gates.G3_adversarial.focus
            or ["failure modes", "edge cases", "security"]
        )
        return (
            f"You are the AGY adversarial reviewer for checkpoint "
            f"{checkpoint.checkpoint_id}. Objective: {checkpoint.objective}\n"
            f"Adversarially review the implementation under {scope} for: "
            f"{focus}.\n"
            "You are READ-ONLY: never create, edit, or delete any file.\n"
            "Produce ONLY a JSON document — no prose, no markdown — of the "
            'form {"verdict": "PASS|FAIL", "findings": [{"severity": '
            '"critical|major|minor", "file": str, "line": int|null, '
            '"detail": str}], "summary": str}. At most 25 findings, most '
            "severe first. Every finding must be grounded in code you "
            "actually read — never invent file paths, line numbers, or "
            "behaviour. verdict FAIL iff at least one critical or major "
            "finding exists."
        )

    def _fixer_prompt(
        self,
        checkpoint: Checkpoint,
        instructions: str,
        findings: list[dict],
    ) -> str:
        blocking = [
            f for f in findings
            if f.get("severity") in ("critical", "major")
        ][:MAX_FINDINGS]
        scope = (
            json.dumps(checkpoint.relevant_files)
            if checkpoint.relevant_files
            else "the files the findings name (the checkpoint declares no "
            "relevant_files boundary)"
        )
        return (
            f"You are the AGY bounded fixer for checkpoint "
            f"{checkpoint.checkpoint_id}. Objective: {checkpoint.objective}\n"
            f"Apply EXACTLY these gatekeeper repair instructions:\n{instructions}\n"
            f"Open findings (critical/major only):\n{json.dumps(blocking)}\n"
            f"Modify ONLY files under {scope}. NEVER touch test files "
            f"(anything under {self.config.test_path}/ or named test_*.py). "
            "No new dependencies, no behaviour beyond what the findings "
            "require. Do not run or rewrite the test suite."
        )

    # -- the G3 sequence ---------------------------------------------------------

    def execute_checkpoint(
        self,
        plan_id: str,
        checkpoint: Checkpoint,
        engine: GateEngine | None = None,
    ) -> GateExecutionRecord:
        """Run the adversarial review/fix loop for ``checkpoint``'s G3
        gate; returns the FINAL (terminal) ``GateExecutionRecord``.

        The checkpoint must be SUBMITTED with seeded gate records. If
        ``engine`` is omitted a fresh one is built from the checkpoint's
        durable ``gate_records`` (pass one in to share state across
        gates). ACTIVE on entry = resume (the loop re-runs deterministically).
        """
        if G3_GATE_ID not in checkpoint.gate_records:
            raise GateRuntimeError(
                "checkpoint gate_records are not seeded — submit the plan "
                "(seed_gate_records) before executing gates"
            )
        if engine is None:
            engine = GateEngine(checkpoint)

        # -- 1. APPLICABILITY ------------------------------------------------
        expectations = checkpoint.gates.G3_adversarial
        if (
            not expectations.required
            or checkpoint.gate_records[G3_GATE_ID].applicability
            != APPLICABILITY_REQUIRED
        ):
            raise GateRuntimeError(
                f"{G3_GATE_ID}: plan says NOT_APPLICABLE — nothing for the "
                "adversarial executor to run"
            )
        current = engine.record(G3_GATE_ID).status
        if current == GateStatus.PENDING:
            self._advance(engine, checkpoint, G3_GATE_ID, GateStatus.ACTIVE,
                          actor="executor")
        elif current != GateStatus.ACTIVE:
            raise GateRuntimeError(
                f"{G3_GATE_ID} is {current.value} (terminal before the "
                "executor ran); nothing to execute"
            )

        requirements = expectations.model_dump(mode="json")
        requirements["objective"] = checkpoint.objective
        started_at = _utc_iso(self._clock())
        base: dict[str, Any] = {
            "plan_id": plan_id,
            "checkpoint_id": checkpoint.checkpoint_id,
            "gate_id": G3_GATE_ID,
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

        prior_repairs: list[GateExecutionRecord] = []
        cycle = 1
        while True:
            base["attempt"] = cycle

            # -- 2. REVIEW (read-only adversarial reviewer) --------------------
            reviewer_before = self._repo_snapshot(self.config.repo)
            reviewer_since_ms = int(self._clock() * 1000)
            reviewer_result = self._dispatch_worker(
                ROLE_ADVERSARIAL_REVIEWER,
                self._reviewer_prompt(checkpoint),
                str(self.config.repo),
            )
            reviewer_after = self._repo_snapshot(self.config.repo)
            reviewer_session, reviewer_source = self._session_capturer(
                ROLE_ADVERSARIAL_REVIEWER, reviewer_result, reviewer_since_ms
            )
            reviewer_ok = bool(getattr(reviewer_result, "ok", True))
            sessions: list[dict] = [
                {
                    "role": "adversarial-reviewer",
                    "session_id": reviewer_session,
                    "session_source": reviewer_source,
                    "ok": reviewer_ok,
                    "cycle": cycle,
                }
            ]
            if not reviewer_ok:
                raise AdversarialReviewUnavailable(
                    f"{G3_GATE_ID}: adversarial reviewer dispatch failed "
                    f"(cycle {cycle}) — gate stays ACTIVE, resumable"
                )
            try:
                review = parse_review_document(
                    getattr(reviewer_result, "output", "")
                )
            except ValueError as exc:
                # Fail-closed: gate stays ACTIVE, nothing terminal written.
                raise AdversarialReviewUnavailable(
                    f"{G3_GATE_ID}: {exc} (cycle {cycle}) — gate stays "
                    "ACTIVE, resumable"
                ) from exc

            # -- 3. REVIEWER GUARD (read-only authority) -----------------------
            mutated = _non_artifact_paths(_changed_paths(reviewer_before, reviewer_after))
            if mutated:
                self._rejected_record(
                    engine,
                    checkpoint,
                    G3_GATE_ID,
                    {**base, "worker_sessions": base["worker_sessions"] + sessions},
                    "reviewer_guard",
                    f"read-only reviewer changed the repo: {mutated}",
                )
                raise ReviewerMutatedRepo(
                    f"{G3_GATE_ID}: adversarial reviewer is read-only but "
                    f"changed: {mutated}"
                )

            # -- 4. DETERMINISTIC RE-CHECK every cycle (the fixer safety net) ---
            reg_rc, reg_out, reg_err = self._run_test(
                self.config.test_command, str(self.config.repo)
            )
            regression_ok = reg_rc == 0
            if list(self.config.full_suite_command) != list(self.config.test_command):
                cum_rc, cum_out, cum_err = self._run_test(
                    self.config.full_suite_command, str(self.config.repo)
                )
            else:
                cum_rc, cum_out, cum_err = reg_rc, reg_out, reg_err
            cumulative_ok = cum_rc == 0
            finding_counts: dict[str, int] = {}
            for f in review["findings"]:
                finding_counts[f["severity"]] = finding_counts.get(f["severity"], 0) + 1
            review_detail = (
                f"verdict {review['verdict']}: {review['summary']} "
                f"({len(review['findings'])} findings: "
                f"{json.dumps(finding_counts, sort_keys=True)})"
            )

            # -- 5. EVIDENCE record (per cycle) ----------------------------------
            cycle_checks = list(base["deterministic_checks"]) + [
                {"name": "reviewer_guard", "ok": True,
                 "detail": f"cycle {cycle}: reviewer made no repo changes"},
                {"name": "regression", "ok": regression_ok,
                 "detail": f"test command exit {reg_rc}: "
                           f"{_tail(reg_out, 400)} {_tail(reg_err, 200)}"},
                {"name": "adversarial_review",
                 "ok": review["verdict"] == "PASS",
                 "detail": _tail(review_detail, 800)},
            ]
            impl_evidence = build_implementation_evidence(
                self.config.repo,
                sorted(set(base["changed_files"])),
                test_path=self.config.test_path,
            )
            # Findings ride INSIDE implementation_evidence (the record
            # field set is P9-frozen and schema-strict — no new keys).
            impl_evidence = {
                **impl_evidence,
                "adversarial_findings": review["findings"],
                "review_verdict": review["verdict"],
                "review_summary": review["summary"],
            }
            run_evidence = {
                "ok": regression_ok,
                "returncode": int(reg_rc),
                "stdout_tail": _tail(reg_out),
                "stderr_tail": _tail(reg_err),
            }
            record = GateExecutionRecord(
                plan_id=plan_id,
                checkpoint_id=checkpoint.checkpoint_id,
                gate_id=G3_GATE_ID,
                attempt=cycle,
                requirements=dict(base["requirements"]),
                worker_sessions=list(base["worker_sessions"]) + sessions,
                changed_files=sorted(set(base["changed_files"])),
                deterministic_checks=cycle_checks,
                red_evidence=None,
                green_evidence=dict(run_evidence),
                cumulative_gate={
                    "ok": cumulative_ok,
                    "returncode": int(cum_rc),
                    "stdout_tail": _tail(cum_out),
                    "stderr_tail": _tail(cum_err),
                },
                implementation_evidence=impl_evidence,
                supervisor_session_id=None,
                supervisor_decision=None,
                repair_lineage=list(base["repair_lineage"]),
                timestamps={
                    **base["timestamps"],
                    "finished_at": _utc_iso(self._clock()),
                },
            )

            # -- 6. GATEKEEPER (MiMo Pro authority; verdict is evidence) ---------
            gatekeeper = self._resolve_gatekeeper()
            # Fail-closed: if decide() raises, nothing is written and the
            # exception propagates — the gate stays ACTIVE, resumable.
            decision = gatekeeper.decide(record, list(prior_repairs))

            # -- 6b. NEEDS_CONTEXT: ONE bounded slice per cycle ------------------
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
                                **impl_evidence,
                                "context_slice": {
                                    "request": decision.context_request,
                                    "content": content,
                                    "truncated": slice_truncated,
                                },
                            },
                        }
                    )
                    # exactly one re-review per cycle; its decision is final
                    decision = gatekeeper.decide(record, list(prior_repairs))
                if decision.decision == "NEEDS_CONTEXT":
                    detail = slice_err or (
                        "gatekeeper still needs context after the single "
                        f"bounded slice {decision.context_request!r}"
                        if decision.context_request
                        else "gatekeeper returned NEEDS_CONTEXT without a "
                        "resolvable context_request"
                    )
                    terminal_checks = cycle_checks + [
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
                        engine, checkpoint, G3_GATE_ID,
                        GateStatus.REJECTED, actor="gatekeeper",
                    )
                    raise GatekeeperContextUnavailable(
                        f"{G3_GATE_ID}: NEEDS_CONTEXT could not be satisfied "
                        f"with one bounded slice — gate REJECTED (fail "
                        f"closed): {detail}"
                    )

            stamped = GateExecutionRecord(
                **{
                    **record.to_dict(),
                    "supervisor_decision": decision.decision,
                    "supervisor_session_id": decision.session_id,
                }
            )
            if decision.decision == "ACCEPT":
                # -- 7a. ACCEPT -> PASSED (only path to PASSED) ------------------
                if not (
                    regression_ok
                    and cumulative_ok
                    and evidence_is_sufficient(G3_GATE_ID, stamped)
                ):
                    raise GateRuntimeError(
                        f"{G3_GATE_ID}: internal invariant violated — ACCEPT "
                        "decision without sufficient deterministic evidence "
                        "(regression + cumulative run and ALL checks ok)"
                    )
                self._emit(stamped)
                self._advance(
                    engine, checkpoint, G3_GATE_ID, GateStatus.PASSED,
                    evidence=stamped, actor="gatekeeper",
                )
                return stamped
            if decision.decision == "REJECT":
                self._emit(stamped)
                self._advance(engine, checkpoint, G3_GATE_ID,
                              GateStatus.REJECTED, actor="gatekeeper")
                return stamped
            if decision.decision != "REPAIR":
                raise GateRuntimeError(
                    f"gatekeeper returned unknown decision {decision.decision!r}"
                )

            # -- 7b. REPAIR (bounded fixer loop) ---------------------------------
            prior_repairs.append(stamped)
            if cycle >= self._max_cycles:
                # GateStatus has no BLOCKED (P9-frozen): exhaustion is
                # REJECTED carrying the escalation intent for P11.
                exhaustion = GateExecutionRecord(
                    **{
                        **stamped.to_dict(),
                        "supervisor_decision": "REJECT",
                        "requirements": {
                            **stamped.requirements,
                            "last_repair_reason": decision.reason,
                            "escalation": "pending (P11)",
                        },
                        "deterministic_checks": cycle_checks + [
                            {
                                "name": "fixer_cycles",
                                "ok": False,
                                "detail": _ESCALATION_DETAIL,
                            }
                        ],
                    }
                )
                self._emit(exhaustion)
                self._advance(engine, checkpoint, G3_GATE_ID,
                              GateStatus.REJECTED, actor="gatekeeper")
                return exhaustion

            self._emit(stamped)
            self._advance(engine, checkpoint, G3_GATE_ID,
                          GateStatus.REPAIR, actor="gatekeeper")
            self._advance(engine, checkpoint, G3_GATE_ID,
                          GateStatus.ACTIVE, actor="gatekeeper")
            base["repair_lineage"] = list(base["repair_lineage"]) + [cycle]
            base["requirements"] = {
                **base["requirements"],
                "last_repair_reason": decision.reason,
                "last_repair_instructions": decision.repair_instructions,
            }

            # -- 8. FIX (bounded fixer) -------------------------------------------
            fixer_test_before = self._test_path_snapshot(self.config.repo)
            fixer_repo_before = self._repo_snapshot(self.config.repo)
            fixer_since_ms = int(self._clock() * 1000)
            fixer_result = self._dispatch_worker(
                ROLE_FIXER,
                self._fixer_prompt(
                    checkpoint, decision.repair_instructions, review["findings"]
                ),
                str(self.config.repo),
            )
            fixer_session, fixer_source = self._session_capturer(
                ROLE_FIXER, fixer_result, fixer_since_ms
            )
            fixer_ok = bool(getattr(fixer_result, "ok", True))
            base["worker_sessions"].append(
                {
                    "role": "adversarial-reviewer",
                    "session_id": reviewer_session,
                    "session_source": reviewer_source,
                    "ok": reviewer_ok,
                    "cycle": cycle,
                }
            )
            base["worker_sessions"].append(
                {
                    "role": "fixer",
                    "session_id": fixer_session,
                    "session_source": fixer_source,
                    "ok": fixer_ok,
                    "cycle": cycle,
                }
            )

            # -- 9. FIXER GUARDS (test integrity + scope) ---------------------------
            fixer_test_after = self._test_path_snapshot(self.config.repo)
            tampered = [
                p for p in _non_artifact_paths(
                    _changed_paths(fixer_test_before, fixer_test_after)
                )
            ]
            if tampered:
                record_base = dict(base)
                self._rejected_record(
                    engine,
                    checkpoint,
                    G3_GATE_ID,
                    record_base,
                    "fixer_test_integrity",
                    f"fixer changed test files: {tampered}",
                )
                raise TestFileTampered(
                    f"{G3_GATE_ID}: fixer rewrote tests to fit: {tampered}"
                )

            fixer_repo_after = self._repo_snapshot(self.config.repo)
            fixer_changed = [
                p for p in _changed_paths(fixer_repo_before, fixer_repo_after)
                if not _under_test_path(p, self.config.test_path)
                and not _is_tooling_artifact(p)
            ]
            if checkpoint.relevant_files:
                allowed = {
                    str(p).replace("\\", "/").lstrip("./")
                    for p in checkpoint.relevant_files
                }
                outside = sorted(
                    p for p in fixer_changed
                    if p not in allowed
                    and not any(
                        p.startswith(a.rstrip("/") + "/") for a in allowed
                    )
                )
                if outside:
                    self._rejected_record(
                        engine,
                        checkpoint,
                        G3_GATE_ID,
                        dict(base),
                        "fixer_scope",
                        f"fixer changed files outside relevant_files: {outside}",
                    )
                    raise FixerEscapedScope(
                        f"{G3_GATE_ID}: fixer escaped the checkpoint scope "
                        f"(relevant_files): {outside}"
                    )
            if not fixer_ok:
                self._rejected_record(
                    engine, checkpoint, G3_GATE_ID, dict(base),
                    "fixer_dispatch", "fixer worker failed",
                )
                raise GateRuntimeError("fixer worker dispatch failed")

            base["changed_files"] = sorted(
                set(base["changed_files"]) | set(fixer_changed)
            )
            cycle += 1


# ---------------------------------------------------------------------------
# Live factory (real MiMo Pro gatekeeper + real session capture; no network
# at construction time — the endpoint is only contacted during decide()).
# ---------------------------------------------------------------------------


def default_gatekeeper_config() -> dict:
    """Wire the live MiMo Pro gatekeeper from AGY settings (supervisor
    role model on the Helmholtz provider). Pure data — no network."""
    from app.config import AgySettings

    settings = AgySettings.from_env()
    return {
        "base_url": settings.provider_base_url,
        "model": settings.supervisor_model,
        "api_key": settings.provider_api_key,
    }


def build_live_g3_executor(
    *,
    repo: Path | str,
    test_path: str = "tests",
    test_command: list[str] | None = None,
    full_suite_command: list[str] | None = None,
    gatekeeper_config: dict | None = None,
    **overrides: Any,
) -> G3Executor:
    """Construct a fully-wired live ``G3Executor`` (real ``MiMoProG3Gatekeeper``,
    real OpenCode dispatch + session capture via the G1-pattern defaults).

    Any seam can be overridden via ``**overrides`` (tests never call this
    with live effects — construction performs no network I/O)."""
    kwargs: dict[str, Any] = {
        "test_path": test_path,
        "test_command": list(test_command or ["pytest", "-q"]),
        "full_suite_command": list(full_suite_command or test_command or ["pytest", "-q"]),
        "gatekeeper_config": dict(gatekeeper_config or default_gatekeeper_config()),
    }
    kwargs.update(overrides)
    return G3Executor(repo, **kwargs)
