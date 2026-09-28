"""P9 — the G1 Behavior/TDD runtime executor.

Executes the deterministic ten-step G1 sequence for ONE checkpoint on top
of the generic :class:`~app.orchestration.checkpoint_planning.gate_engine.GateEngine`
and the first-class :class:`~app.orchestration.checkpoint_planning.gate_evidence.GateExecutionRecord`.

Design rules encoded here:

* The planner defines what the gate requires; this runtime proves whether
  it passed. The executor is the ONLY writer of gate status (via the
  engine), and it never writes PASSED except on a gatekeeper ACCEPT
  backed by sufficient deterministic evidence (RED + GREEN + cumulative
  + guards, ``g1_evidence_is_sufficient``).
* Two worker roles with runtime-enforced path scoping (OpenCode agent
  permissions are tool-granular only — the snapshot comparison is the
  authority, mirroring the P8 zero-mutation guard):
  - ``TEST_AUTHOR`` (agent ``agy-test-author``) may only touch files
    under ``test_path`` — anything else is ``TestAuthorEscapedTestPaths``
    and the gate is REJECTED (fail closed).
  - ``IMPLEMENTER`` (agent ``agy-dispatch``) may never touch test files —
    any test-file change is ``TestFileTampered`` and the gate is REJECTED.
* Every external effect is an injectable seam (``run_test``,
  ``dispatch_worker``, ``gatekeeper``, ``sentinel``, ``session_capturer`` / ``session_read_back``,
  ``test_path_snapshot``, ``repo_snapshot``); unit tests use fakes only.
  The defaults wire to ``run_supervised`` (workers), subprocess (tests),
  the P7 ``SentinelRuntime`` (long-run monitoring — models never own
  wait loops), and :class:`MiMoProGatekeeper` (the default live
  gatekeeper, same injected-http transport pattern as
  ``MiMoProFallbackPlanner``).
  Worker sessions are captured with the P4-proven resolution order
  (``agy_session.capture_session``): stdout first, then a single
  server read-back filtered by agent + directory + since-timestamp —
  ``opencode-supervise`` prints no ``Session ID:`` line (proven live in
  P3), so stdout alone can never satisfy the "persist the exact OpenCode
  session id" contract.
* The gatekeeper failing is fail-closed: the gate stays ACTIVE, nothing
  is written, ``GatekeeperUnavailable`` propagates, the attempt is
  resumable. REPAIR is bounded by ``max_attempts`` — exhaustion REJECTs.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from app.orchestration.checkpoint_planning.gate_evidence import (
    GateExecutionRecord,
    g1_evidence_is_sufficient,
)
from app.orchestration.checkpoint_planning.gate_engine import GateEngine
from app.orchestration.checkpoint_planning.schemas import (
    APPLICABILITY_REQUIRED,
    Checkpoint,
    GateStatus,
)

G1_GATE_ID = "G1_behavior"

ROLE_TEST_AUTHOR = "TEST_AUTHOR"
ROLE_IMPLEMENTER = "IMPLEMENTER"

#: Untracked, tooling-generated path prefixes that are NOT worker output.
#: OpenCode/Serena/Graphify/python materialize these directories as a side
#: effect of running ANY agent in a fresh repo (Serena project init,
#: Graphify graph, bytecode caches). The author guard must not treat their
#: appearance as an escape; it still catches every real source file.
TOOLING_ARTIFACT_PREFIXES = (
    "__pycache__",
    ".serena",
    "graphify-out",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
)

AGENT_FOR_ROLE = {
    ROLE_TEST_AUTHOR: "agy-test-author",
    ROLE_IMPLEMENTER: "agy-dispatch",
}

_TERMINAL_SENTINEL_STATES = (
    "EXITED_SUCCESS",
    "EXITED_FAILURE",
    "LOST",
    "KILLED",
    "TIMED_OUT",
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GateRuntimeError(RuntimeError):
    """Base error of the G1 runtime."""


class TestAuthorEscapedTestPaths(GateRuntimeError):
    """The test-author worker touched a file outside the test paths."""


class TestFileTampered(GateRuntimeError):
    """The implementer worker changed a test file."""


class RedNotDemonstrated(GateRuntimeError):
    """The planned tests did not FAIL before implementation."""


class GreenNotAchieved(GateRuntimeError):
    """The planned tests did not PASS after implementation."""


class CumulativeRegression(GateRuntimeError):
    """The full suite regressed."""


class GatekeeperUnavailable(RuntimeError):
    """The gatekeeper could not review (transport/parse failure).

    Fail-closed: the gate stays ACTIVE and the attempt is resumable.
    """


# ---------------------------------------------------------------------------
# Configuration + gatekeeper contract
# ---------------------------------------------------------------------------


@dataclass
class GateConfig:
    """Static configuration of one G1 execution (pure data, no behavior)."""

    repo: Path
    test_path: str
    test_command: list[str]
    full_suite_command: list[str] = field(default_factory=list)
    max_attempts: int = 3
    long_run_threshold_s: float = 30.0
    clock: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        if not self.full_suite_command:
            self.full_suite_command = list(self.test_command)


class G1Gatekeeper(Protocol):
    """Structured evidence review by the Pro model (or a fake in tests)."""

    def decide(
        self,
        evidence: GateExecutionRecord,
        prior_repairs: list[GateExecutionRecord],
    ) -> "GatekeeperDecision": ...


@dataclass
class GatekeeperDecision:
    decision: str  # "ACCEPT" | "REPAIR" | "REJECT"
    reason: str
    repair_instructions: str = ""
    session_id: str | None = None


# ---------------------------------------------------------------------------
# Snapshot helpers (runtime path-scoping authority)
# ---------------------------------------------------------------------------


def _default_test_path_snapshot(repo: Path) -> dict[str, str]:
    """Content hash of every tracked-looking file under the repo's test
    trees (``tests/`` and ``test_*.py`` at any level).

    Derived artifacts are excluded (bytecode caches, ``.pyc``/``.pyo``):
    running the test suite — which a worker legitimately does — rewrites
    bytecode and must NOT read as a test-file change to the implement
    guard. A change to a ``.py`` test source is always caught.
    """
    import hashlib

    snapshot: dict[str, str] = {}
    root = Path(repo)
    if not root.is_dir():
        return snapshot
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        parts = rel.parts
        if ".git" in parts or "__pycache__" in parts:
            continue
        if path.suffix in (".pyc", ".pyo"):
            continue
        if parts and (parts[0] == "tests"):
            pass
        elif not (parts and parts[-1].startswith("test_") and parts[-1].endswith(".py")):
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = "unreadable"
        snapshot[str(rel)] = digest
    return snapshot


def _default_repo_snapshot(repo: Path) -> str:
    """Git status + HEAD snapshot, same shape as the P8 zero-mutation
    guard in ``codebase_analysis.py``."""

    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        ).stdout

    return f"HEAD={run('rev-parse', 'HEAD')}\nSTATUS=BEGIN\n{run('status', '--porcelain')}STATUS=END\n"


def _paths_in_snapshot(snapshot: Any) -> set[str]:
    """Changed-file paths carried by a snapshot value (dict or git string)."""
    if isinstance(snapshot, dict):
        return {str(p) for p in snapshot.keys()}
    text = str(snapshot or "")
    if "STATUS=BEGIN" in text:
        text = text.split("STATUS=BEGIN", 1)[1].split("STATUS=END", 1)[0]
    paths: set[str] = set()
    for line in text.splitlines():
        entry = line[3:] if len(line) > 3 else ""
        if "->" in entry:
            entry = entry.split("->")[-1]
        entry = entry.strip().strip('"')
        if entry:
            paths.add(entry)
    return paths


def _changed_paths(before: Any, after: Any) -> list[str]:
    """Files added or content-changed between two snapshots of one kind."""
    if isinstance(before, dict) and isinstance(after, dict):
        changed = {
            str(p) for p in after if before.get(p) != after.get(p)
        }
        changed |= {str(p) for p in before if p not in after}
        return sorted(changed)
    return sorted(_paths_in_snapshot(after) - _paths_in_snapshot(before))


def _under_test_path(path: str, test_path: str) -> bool:
    normalized = str(path).replace("\\", "/").lstrip("./")
    prefix = str(test_path).replace("\\", "/").strip("/")
    if not prefix:
        return False
    return normalized == prefix or normalized.startswith(prefix + "/")


def _is_tooling_artifact(path: str) -> bool:
    """Top-level tooling-generated path (NOT worker output).

    Only a path whose FIRST segment is a known artifact dir qualifies —
    ``app/__pycache__/x.pyc`` is still reported (a real file under app/
    would be too), while ``__pycache__/mathutil.cpython-313.pyc`` at the
    repo root is bytecode noise.
    """
    normalized = str(path).replace("\\", "/").strip()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    top = normalized.split("/", 1)[0].rstrip("/")
    return top in TOOLING_ARTIFACT_PREFIXES


def _non_artifact_paths(paths) -> list[str]:
    return sorted(p for p in paths if not _is_tooling_artifact(p))


def _tail(text: str, limit: int = 1500) -> str:
    text = str(text or "")
    return text[-limit:]


def _utc_iso(clock_value: float | None = None) -> str:
    if clock_value is None:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(clock_value, timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Default live gatekeeper (MiMo Pro over the Helmholtz endpoint)
# ---------------------------------------------------------------------------

GATEKEEPER_ROLE_RULES = (
    "You are the AGY G1 Behavior/TDD gatekeeper. You review STRUCTURED "
    "evidence only. Reply with ONLY a JSON object: "
    '{"decision": "ACCEPT|REPAIR|REJECT", "reason": str, '
    '"repair_instructions": str}. ACCEPT only if the evidence proves RED '
    'before GREEN, GREEN after implementation, a passing cumulative suite, '
    "and all deterministic guards ok. Never invent facts."
)


def _extract_json_object(text: str) -> dict:
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, TypeError):
        pass
    stripped = str(text or "").strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(stripped[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass
    raise ValueError("no JSON object found in gatekeeper response")


class MiMoProGatekeeper:
    """Default live gatekeeper: MiMo Pro reviews the structured evidence.

    Same transport pattern as ``MiMoProFallbackPlanner`` (injected
    ``http`` callable, ``response_format: json_object``). The prompt is
    COMPACT structured JSON — bounded tails of run output, never raw
    worker transcripts — because the Helmholtz proxy has a ~60s
    upstream-read timeout on the Pro reasoning model. Any transport or
    parse failure raises ``GatekeeperUnavailable`` (fail closed).
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
                {"role": "system", "content": GATEKEEPER_ROLE_RULES},
                {"role": "user", "content": prompt},
            ],
        }
        url = self._base_url + "/chat/completions"
        try:
            status, body = self._http(url, headers, payload, self._timeout)
        except Exception as exc:  # noqa: BLE001 - adapter boundary, fail closed
            raise GatekeeperUnavailable(
                f"gatekeeper transport failed: {exc}"
            ) from exc
        if status != 200:
            raise GatekeeperUnavailable(
                f"gatekeeper transport failed: HTTP {status}: {str(body)[:300]}"
            )
        try:
            envelope = json.loads(body)
            text = envelope["choices"][0]["message"]["content"]
            parsed = _extract_json_object(text)
            decision = str(parsed["decision"]).strip().upper()
            if decision not in ("ACCEPT", "REPAIR", "REJECT"):
                raise ValueError(f"invalid decision {decision!r}")
        except Exception as exc:  # noqa: BLE001 - fail closed, never guess
            raise GatekeeperUnavailable(
                f"gatekeeper response unparseable: {exc}"
            ) from exc
        session_id = envelope.get("id") if isinstance(envelope, dict) else None
        return GatekeeperDecision(
            decision=decision,
            reason=str(parsed.get("reason") or ""),
            repair_instructions=str(parsed.get("repair_instructions") or ""),
            session_id=str(session_id) if session_id else None,
        )

    # -- internals ---------------------------------------------------------

    def _build_prompt(
        self,
        evidence: GateExecutionRecord,
        prior_repairs: list[GateExecutionRecord],
    ) -> str:
        doc = evidence.to_dict()
        compact = {
            "gate_id": doc["gate_id"],
            "checkpoint_id": doc["checkpoint_id"],
            "plan_id": doc["plan_id"],
            "attempt": doc["attempt"],
            "repair_lineage": doc["repair_lineage"],
            "requirements": doc["requirements"],
            "worker_sessions": doc["worker_sessions"],
            "changed_files": doc["changed_files"][:25],
            "deterministic_checks": [
                {
                    "name": c.get("name"),
                    "ok": c.get("ok"),
                    "detail": _tail(str(c.get("detail") or ""), 300),
                }
                for c in doc["deterministic_checks"]
                if isinstance(c, dict)
            ],
            "red_evidence": self._compact_run(doc["red_evidence"]),
            "green_evidence": self._compact_run(doc["green_evidence"]),
            "cumulative_gate": self._compact_run(doc["cumulative_gate"]),
            "prior_repairs": [
                {
                    "attempt": r.attempt,
                    "decision": r.supervisor_decision,
                    "reason": _tail(str((r.requirements or {}).get("last_repair_reason", "")), 200),
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
# Default seam implementations (live path; NOT exercised in unit tests)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Session capture (live path; unit tests inject session_capturer)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The executor
# ---------------------------------------------------------------------------


class G1Executor:
    """Runs the 10-step G1 Behavior/TDD sequence for one checkpoint."""

    def __init__(
        self,
        repo: Path | str,
        *,
        test_path: str,
        test_command: list[str],
        full_suite_command: list[str] | None = None,
        max_attempts: int = 3,
        long_run_threshold_s: float = 30.0,
        run_test: Callable[[list[str], str], tuple[int, str, str]] | None = None,
        dispatch_worker: Callable[[str, str, str], Any] | None = None,
        gatekeeper: G1Gatekeeper | None = None,
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
            max_attempts=int(max_attempts),
            long_run_threshold_s=float(long_run_threshold_s),
            clock=clock,
        )
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
        #: every record the executor emitted (terminal or per-attempt) — the
        #: durable evidence trail the engine references by evidence_refs.
        self.emitted_records: list[GateExecutionRecord] = []
        #: ordered ``(gate_id, from, to, actor)`` transitions observed.
        self.transition_log: list[tuple[str, GateStatus, GateStatus, str]] = []

    def _default_session_capturer(
        self, role: str, result: Any, since_ms: int
    ) -> tuple[str | None, str]:
        """Resolve the worker's real session id — P4 order: stdout -> read-back.

        ``opencode-supervise`` never prints a ``Session ID:`` line (proven
        live in P3), so stdout alone can never satisfy the "persist the exact
        OpenCode session id" contract by itself. The read-back is a single
        GET of the OpenCode session list filtered by agent + directory +
        ``since_ms``. Returns ``(session_id, source)`` with source in
        ``stdout|readback|none`` (persisted alongside the id so the capture
        path is auditable). Every failure yields ``(None, "none")`` — capture
        never fails the execution path and never invents an id.
        """
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

    # -- seams: live defaults --------------------------------------------------

    def _default_run_test(self, cmd: list[str], cwd: str) -> tuple[int, str, str]:
        """Run the test command; long runs hand off to the P7 sentinel.

        Models NEVER own wait loops: with a sentinel injected, once the
        clock crosses ``long_run_threshold_s`` the command is spawned
        monitored via ``sentinel.spawn_monitored_process`` (exactly once)
        and the runtime resumes on the terminal event snapshot — at most
        one snapshot read, never a busy-poll. Without a sentinel it is a
        plain (unbounded-by-default) subprocess.
        """
        started = self._clock()
        if self._sentinel is not None and (
            self._clock() - started
        ) >= self.config.long_run_threshold_s:
            return self._sentinel_run(cmd, cwd)
        proc = subprocess.run(
            list(cmd),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    def _sentinel_run(self, cmd: list[str], cwd: str) -> tuple[int, str, str]:
        """P7 seam: spawn monitored, resume on the terminal snapshot."""
        job_id = self._sentinel.spawn_monitored_process(
            list(cmd),
            str(cwd),
            deadline_s=max(1.0, self.config.long_run_threshold_s * 10.0),
        )
        predicate = (
            lambda rec: getattr(rec, "state", None) in _TERMINAL_SENTINEL_STATES
        )
        run_until = getattr(self._sentinel, "run_until", None)
        if callable(run_until):
            run_until(job_id, predicate)
        snapshot = self._sentinel.get_process_snapshot(job_id)
        state = str(snapshot.get("state", ""))
        returncode = snapshot.get("returncode")
        if returncode is None:
            returncode = 0 if state == "EXITED_SUCCESS" else 1
        return (
            int(returncode),
            str(snapshot.get("stdout") or ""),
            str(snapshot.get("stderr") or ""),
        )

    def _default_dispatch_worker(self, role: str, prompt: str, repo: str) -> Any:
        """Bounded OpenCode worker via ``run_supervised`` (repeat_limit=3,
        per the P8.1 fix)."""
        from app.integrations.external_stack import run_supervised

        agent = AGENT_FOR_ROLE.get(role)
        if agent is None:
            raise GateRuntimeError(f"unknown worker role: {role!r}")
        return run_supervised(
            repo,
            prompt,
            agent=agent,
            repeat_limit=3,
        )

    def _resolve_gatekeeper(self) -> G1Gatekeeper:
        if self._gatekeeper is not None:
            return self._gatekeeper
        if self._gatekeeper_config is not None:
            self._gatekeeper = MiMoProGatekeeper(**self._gatekeeper_config)
            return self._gatekeeper
        raise GatekeeperUnavailable(
            "no gatekeeper available: G1Executor requires an explicit "
            "gatekeeper or an explicit gatekeeper_config (fail closed — a "
            "gate is never silently skipped)"
        )

    # -- evidence plumbing ------------------------------------------------------

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

    # -- the 10-step sequence ---------------------------------------------------

    def execute_g1(self, checkpoint: Checkpoint, *, plan_id: str) -> GateExecutionRecord:
        """Run G1 for ``checkpoint``'s behavior gate; returns the FINAL
        (terminal) ``GateExecutionRecord``.

        The checkpoint must already be SUBMITTED with seeded gate records
        (``submission.seed_gate_records``). The checkpoint's own
        ``gate_records`` dict is updated with the engine's live state so
        the caller can persist it.
        """
        if G1_GATE_ID not in checkpoint.gate_records:
            raise GateRuntimeError(
                "checkpoint gate_records are not seeded — submit the plan "
                "(seed_gate_records) before executing gates"
            )
        engine = GateEngine(checkpoint)

        # -- 1. CONTRACT ------------------------------------------------------
        expectations = checkpoint.gates.G1_behavior
        if (
            not expectations.required
            or checkpoint.gate_records[G1_GATE_ID].applicability
            != APPLICABILITY_REQUIRED
        ):
            raise GateRuntimeError(
                f"{G1_GATE_ID}: plan says NOT_APPLICABLE — nothing for the "
                "TDD executor to run"
            )
        if not expectations.planned_tests:
            raise GateRuntimeError(
                f"{G1_GATE_ID}: required behavior gate without planned_tests "
                "(planner contract is empty)"
            )
        current = engine.record(G1_GATE_ID).status
        if current == GateStatus.PENDING:
            self._advance(engine, checkpoint, G1_GATE_ID, GateStatus.ACTIVE,
                          actor="runtime")
        elif current != GateStatus.ACTIVE:
            raise GateRuntimeError(
                f"{G1_GATE_ID} is {current.value} (terminal before the "
                "executor ran); nothing to execute"
            )

        requirements = expectations.model_dump(mode="json")
        started_at = _utc_iso(self._clock())
        base: dict[str, Any] = {
            "plan_id": plan_id,
            "checkpoint_id": checkpoint.checkpoint_id,
            "gate_id": G1_GATE_ID,
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

        # -- 2. TEST AUTHOR -----------------------------------------------------
        author_prompt = (
            "You are the AGY test author for checkpoint "
            f"{checkpoint.checkpoint_id}. Write failing tests ONLY under "
            f"{self.config.test_path}/ for this behavior contract:\n"
            f"objective: {checkpoint.objective}\n"
            f"planned_tests: {json.dumps(expectations.planned_tests)}\n"
            f"test_instructions: {json.dumps(expectations.test_instructions)}\n"
            f"focus: {json.dumps(expectations.focus)}\n"
            "Do NOT touch any file outside the test paths. Do NOT implement "
            "production code. Tests must fail for the RIGHT reason (missing "
            "behavior), not on import syntax errors unless that is the "
            "expected first RED."
        )
        repo_before = self._repo_snapshot(self.config.repo)
        author_since_ms = int(self._clock() * 1000)
        author_result = self._dispatch_worker(
            ROLE_TEST_AUTHOR, author_prompt, str(self.config.repo)
        )
        repo_after = self._repo_snapshot(self.config.repo)
        author_changed = _changed_paths(repo_before, repo_after)
        author_session, author_source = self._session_capturer(
            ROLE_TEST_AUTHOR, author_result, author_since_ms
        )
        author_ok = bool(getattr(author_result, "ok", True))
        base["worker_sessions"].append(
            {
                "role": "test-author",
                "session_id": author_session,
                "session_source": author_source,
                "ok": author_ok,
            }
        )

        # -- 3. AUTHOR GUARD ------------------------------------------------------
        author_changed = _non_artifact_paths(author_changed)
        escaped = sorted(
            p for p in author_changed if not _under_test_path(p, self.config.test_path)
        )
        if escaped:
            self._rejected_record(
                engine,
                checkpoint,
                G1_GATE_ID,
                base,
                "author_guard",
                f"test-author changed non-test files: {escaped}",
            )
            raise TestAuthorEscapedTestPaths(
                f"test-author escaped {self.config.test_path}/: {escaped}"
            )
        if not author_ok:
            self._rejected_record(
                engine, checkpoint, G1_GATE_ID, base, "author_dispatch",
                "test-author worker failed",
            )
            raise GateRuntimeError("test-author worker dispatch failed")
        base["changed_files"] = list(author_changed)
        base["deterministic_checks"].append(
            {"name": "author_guard", "ok": True, "detail": "all changes under test paths"}
        )

        # -- 4. RED ------------------------------------------------------------------
        red_rc, red_out, red_err = self._run_test(
            self.config.test_command, str(self.config.repo)
        )
        if red_rc == 0:
            self._rejected_record(
                engine,
                checkpoint,
                G1_GATE_ID,
                base,
                "red",
                "planned tests PASSED before implementation — nothing was "
                "proven to be missing (no RED)",
            )
            raise RedNotDemonstrated(
                f"{G1_GATE_ID}: planned tests passed at RED step; TDD contract "
                "violated (tests must fail before implementation)"
            )
        base["red_evidence"] = {
            "ok": True,
            "returncode": int(red_rc),
            "stdout_tail": _tail(red_out),
            "stderr_tail": _tail(red_err),
            "failing_targets": list(expectations.planned_tests),
        }
        base["deterministic_checks"].append(
            {"name": "red", "ok": True, "detail": f"exit {red_rc} before implementation"}
        )
        # Tests are frozen from here on (repair retries never re-run the author).
        frozen_test_snapshot = self._test_path_snapshot(self.config.repo)

        prior_repairs: list[GateExecutionRecord] = []
        repair_instructions = ""
        attempt = 1

        while True:
            base["attempt"] = attempt

            # -- 5. IMPLEMENT -----------------------------------------------
            impl_prompt = (
                f"Implement checkpoint {checkpoint.checkpoint_id} so the tests "
                f"under {self.config.test_path}/ pass.\n"
                f"objective: {checkpoint.objective}\n"
                f"relevant_files: {json.dumps(checkpoint.relevant_files)}\n"
                f"implementation_context: {checkpoint.implementation_context}\n"
                f"tests (FROZEN — you may not edit them): "
                f"{json.dumps(sorted(frozen_test_snapshot.keys()))}\n"
                + (
                    f"repair instructions from the gatekeeper: "
                    f"{repair_instructions}\n"
                    if attempt > 1 and repair_instructions
                    else ""
                )
                + "Do NOT modify any file under the test paths."
            )
            implementer_before = self._test_path_snapshot(self.config.repo)
            implementer_since_ms = int(self._clock() * 1000)
            implementer_result = self._dispatch_worker(
                ROLE_IMPLEMENTER, impl_prompt, str(self.config.repo)
            )
            implementer_session, implementer_source = self._session_capturer(
                ROLE_IMPLEMENTER, implementer_result, implementer_since_ms
            )
            implementer_ok = bool(getattr(implementer_result, "ok", True))
            sessions = [
                {
                    "role": "implementer",
                    "session_id": implementer_session,
                    "session_source": implementer_source,
                    "ok": implementer_ok,
                    "attempt": attempt,
                }
            ]
            if not implementer_ok:
                self._rejected_record(
                    engine,
                    checkpoint,
                    G1_GATE_ID,
                    {**base, "worker_sessions": base["worker_sessions"] + sessions},
                    "implement_dispatch",
                    "implementer worker failed",
                )
                raise GateRuntimeError("implementer worker dispatch failed")

            # -- 6. IMPLEMENT GUARD -------------------------------------------
            implementer_after = self._test_path_snapshot(self.config.repo)
            tampered = _changed_paths(frozen_test_snapshot, implementer_after)
            tampered += _changed_paths(implementer_before, implementer_after)
            tampered = sorted(set(tampered))
            if tampered:
                record_base = {
                    **base,
                    "worker_sessions": base["worker_sessions"] + sessions,
                }
                self._rejected_record(
                    engine,
                    checkpoint,
                    G1_GATE_ID,
                    record_base,
                    "implement_guard",
                    f"implementer changed test files: {tampered}",
                )
                raise TestFileTampered(
                    f"implementer rewrote tests to fit: {tampered}"
                )

            # -- 7. GREEN --------------------------------------------------------
            green_rc, green_out, green_err = self._run_test(
                self.config.test_command, str(self.config.repo)
            )
            if green_rc != 0:
                record_base = {
                    **base,
                    "worker_sessions": base["worker_sessions"] + sessions,
                }
                self._rejected_record(
                    engine,
                    checkpoint,
                    G1_GATE_ID,
                    record_base,
                    "green",
                    f"test command exit {green_rc}: {_tail(green_out)} {_tail(green_err)}",
                )
                raise GreenNotAchieved(
                    f"{G1_GATE_ID}: tests did not pass after implementation "
                    f"(exit {green_rc})"
                )

            # -- 8. CUMULATIVE ----------------------------------------------------
            cum_rc, cum_out, cum_err = self._run_test(
                self.config.full_suite_command, str(self.config.repo)
            )
            if cum_rc != 0:
                record_base = {
                    **base,
                    "worker_sessions": base["worker_sessions"] + sessions,
                }
                self._rejected_record(
                    engine,
                    checkpoint,
                    G1_GATE_ID,
                    record_base,
                    "cumulative",
                    f"full suite exit {cum_rc}: {_tail(cum_out)} {_tail(cum_err)}",
                )
                raise CumulativeRegression(
                    f"{G1_GATE_ID}: cumulative suite regressed (exit {cum_rc})"
                )

            # -- 9. GATEKEEPER -----------------------------------------------------
            attempt_checks = list(base["deterministic_checks"]) + [
                {"name": "implement_guard", "ok": True,
                 "detail": f"attempt {attempt}: test files unchanged"},
                {"name": "green", "ok": True, "detail": f"exit 0 (attempt {attempt})"},
                {"name": "cumulative", "ok": True,
                 "detail": f"full suite exit 0 (attempt {attempt})"},
            ]
            record = GateExecutionRecord(
                plan_id=plan_id,
                checkpoint_id=checkpoint.checkpoint_id,
                gate_id=G1_GATE_ID,
                attempt=attempt,
                requirements=requirements,
                worker_sessions=list(base["worker_sessions"]) + sessions,
                changed_files=sorted(
                    set(base["changed_files"]) | set(tampered)
                ),
                deterministic_checks=attempt_checks,
                red_evidence=base["red_evidence"],
                green_evidence={
                    "ok": True,
                    "returncode": 0,
                    "stdout_tail": _tail(green_out),
                    "stderr_tail": _tail(green_err),
                },
                cumulative_gate={
                    "ok": True,
                    "returncode": 0,
                    "stdout_tail": _tail(cum_out),
                    "stderr_tail": _tail(cum_err),
                },
                supervisor_session_id=None,
                supervisor_decision=None,
                repair_lineage=list(base["repair_lineage"]),
                timestamps={**base["timestamps"], "finished_at": _utc_iso(self._clock())},
            )
            gatekeeper = self._resolve_gatekeeper()
            # Fail-closed: if decide() raises, nothing is written and the
            # exception propagates — the gate stays ACTIVE, resumable.
            decision = gatekeeper.decide(record, list(prior_repairs))

            stamped = GateExecutionRecord(
                **{
                    **record.to_dict(),
                    "supervisor_decision": decision.decision,
                    "supervisor_session_id": decision.session_id,
                }
            )
            if decision.decision == "ACCEPT":
                # -- 10a. ACCEPT -> PASSED (only path to PASSED) --------------
                if not g1_evidence_is_sufficient(stamped):
                    raise GateRuntimeError(
                        "internal invariant violated: ACCEPT decision without "
                        "sufficient RED+GREEN+cumulative evidence"
                    )
                self._emit(stamped)
                self._advance(
                    engine, checkpoint, G1_GATE_ID, GateStatus.PASSED,
                    evidence=stamped, actor="gatekeeper",
                )
                return stamped
            if decision.decision == "REJECT":
                self._emit(stamped)
                self._advance(engine, checkpoint, G1_GATE_ID,
                              GateStatus.REJECTED, actor="gatekeeper")
                return stamped
            if decision.decision != "REPAIR":
                raise GateRuntimeError(
                    f"gatekeeper returned unknown decision {decision.decision!r}"
                )

            # -- 10c. REPAIR (bounded) ------------------------------------------
            self._emit(stamped)
            prior_repairs.append(stamped)
            if attempt >= self.config.max_attempts:
                self._advance(engine, checkpoint, G1_GATE_ID,
                              GateStatus.REJECTED, actor="gatekeeper")
                return stamped
            self._advance(engine, checkpoint, G1_GATE_ID,
                          GateStatus.REPAIR, actor="gatekeeper")
            self._advance(engine, checkpoint, G1_GATE_ID,
                          GateStatus.ACTIVE, actor="gatekeeper")
            base["repair_lineage"] = list(base["repair_lineage"]) + [attempt]
            repair_instructions = decision.repair_instructions
            attempt += 1
