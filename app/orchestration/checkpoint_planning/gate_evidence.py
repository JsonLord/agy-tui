"""P9 — first-class gate execution evidence: :class:`GateExecutionRecord`.

Mirrors the ``CommitGateRecord`` discipline from
``app/orchestration/agy_commitgate.py``: a frozen value object with a
strict ``from_dict``/``to_dict`` (``additionalProperties: false``
semantics — any extra or missing key raises ``ValueError`` naming it).

This is the ONLY evidence shape the gate engine consults before it will
allow a PASSED transition. ``is_passed()`` is the deterministic
predicate: supervisor ACCEPT + GREEN evidence + cumulative-suite run +
every deterministic check ok. For G1 specifically,
:func:`g1_evidence_is_sufficient` additionally requires RED evidence —
a behavior gate that never proved the tests failing before the
implementation does not pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.orchestration.checkpoint_planning.schemas import GATE_KINDS

RECORD_FIELDS = (
    "plan_id",
    "checkpoint_id",
    "gate_id",
    "attempt",
    "requirements",
    "worker_sessions",
    "changed_files",
    "deterministic_checks",
    "red_evidence",
    "green_evidence",
    "cumulative_gate",
    "implementation_evidence",
    "supervisor_session_id",
    "supervisor_decision",
    "repair_lineage",
    "timestamps",
)

# P9.2: NEEDS_CONTEXT is persistable ONLY as the terminal decision written
# by the executor's fail-closed path (the gate REJECTs — a record ending in
# NEEDS_CONTEXT is never a pass and never a pending review).
SUPERVISOR_DECISIONS = (None, "ACCEPT", "REPAIR", "REJECT", "NEEDS_CONTEXT")

#: Fields ``from_dict`` accepts as absent (treated as the field default).
#: The ONE deliberate schema relaxation: P9.2's ``implementation_evidence``
#: must not break live records persisted before it existed (base 0dd5567).
#: Every other missing key still raises.
OPTIONAL_IN_INPUT_FIELDS = frozenset({"implementation_evidence"})


@dataclass(frozen=True)
class GateExecutionRecord:
    """Exact-field-set evidence entry for one gate execution attempt."""

    plan_id: str
    checkpoint_id: str
    gate_id: str
    attempt: int
    requirements: dict = field(default_factory=dict)
    worker_sessions: list = field(default_factory=list)
    changed_files: list = field(default_factory=list)
    deterministic_checks: list = field(default_factory=list)
    red_evidence: dict | None = None
    green_evidence: dict | None = None
    cumulative_gate: dict | None = None
    implementation_evidence: dict | None = None
    supervisor_session_id: str | None = None
    supervisor_decision: str | None = None
    repair_lineage: list = field(default_factory=list)
    timestamps: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.gate_id not in GATE_KINDS:
            raise ValueError(
                f"gate_id must be one of {GATE_KINDS}, got {self.gate_id!r}"
            )
        if not isinstance(self.attempt, int) or self.attempt < 1:
            raise ValueError(f"attempt must be an int >= 1, got {self.attempt!r}")
        if self.supervisor_decision not in SUPERVISOR_DECISIONS:
            raise ValueError(
                "supervisor_decision must be one of "
                f"{SUPERVISOR_DECISIONS}, got {self.supervisor_decision!r}"
            )
        if self.supervisor_decision == "ACCEPT" and self.green_evidence is None:
            raise ValueError(
                "FAIL-CLOSED: supervisor_decision='ACCEPT' requires "
                "green_evidence (a gate cannot be accepted without a proven "
                "GREEN run)"
            )

    # -- schema-strict (de)serialization ----------------------------------------

    def to_dict(self) -> dict:
        """The exact field set — stable key order, default=str safe."""
        return {
            "plan_id": self.plan_id,
            "checkpoint_id": self.checkpoint_id,
            "gate_id": self.gate_id,
            "attempt": self.attempt,
            "requirements": dict(self.requirements),
            "worker_sessions": [dict(entry) for entry in self.worker_sessions],
            "changed_files": list(self.changed_files),
            "deterministic_checks": [
                dict(entry) for entry in self.deterministic_checks
            ],
            "red_evidence": (
                dict(self.red_evidence) if self.red_evidence is not None else None
            ),
            "green_evidence": (
                dict(self.green_evidence) if self.green_evidence is not None else None
            ),
            "cumulative_gate": (
                dict(self.cumulative_gate)
                if self.cumulative_gate is not None
                else None
            ),
            "implementation_evidence": (
                dict(self.implementation_evidence)
                if self.implementation_evidence is not None
                else None
            ),
            "supervisor_session_id": self.supervisor_session_id,
            "supervisor_decision": self.supervisor_decision,
            "repair_lineage": list(self.repair_lineage),
            "timestamps": dict(self.timestamps),
        }

    @classmethod
    def from_dict(cls, data) -> "GateExecutionRecord":
        """Validate + rebuild one record.

        Strict: an unexpected key OR a missing key raises ``ValueError``
        naming the key — corrupt evidence is never silently repaired.
        The one deliberate relaxation (P9.2): a key in
        ``OPTIONAL_IN_INPUT_FIELDS`` may be absent (treated as its
        default) so records persisted before ``implementation_evidence``
        existed stay loadable; ``to_dict`` ALWAYS emits it.
        """
        if not isinstance(data, dict):
            raise ValueError(
                f"gate execution record must be a JSON object, got "
                f"{type(data).__name__}"
            )
        keys = set(data.keys())
        expected = set(RECORD_FIELDS)
        extra = sorted(keys - expected)
        missing = sorted(expected - keys - OPTIONAL_IN_INPUT_FIELDS)
        if extra:
            raise ValueError(
                "gate execution record has unexpected field(s): "
                + ", ".join(extra)
            )
        if missing:
            raise ValueError(
                "gate execution record is missing field(s): " + ", ".join(missing)
            )

        if not isinstance(data["attempt"], int) or isinstance(data["attempt"], bool):
            raise ValueError("attempt must be an int")
        for key in ("requirements", "cumulative_gate", "red_evidence",
                    "green_evidence", "implementation_evidence", "timestamps"):
            value = data.get(key)
            if value is not None and not isinstance(value, dict):
                raise ValueError(f"{key} must be a dict or None")
        for key in ("worker_sessions", "changed_files",
                    "deterministic_checks", "repair_lineage"):
            if not isinstance(data[key], list):
                raise ValueError(f"{key} must be a list")
        session_id = data["supervisor_session_id"]
        if session_id is not None and not isinstance(session_id, str):
            raise ValueError("supervisor_session_id must be a str or None")
        decision = data["supervisor_decision"]
        if decision is not None and not isinstance(decision, str):
            raise ValueError("supervisor_decision must be a str or None")

        return cls(
            plan_id=str(data["plan_id"]),
            checkpoint_id=str(data["checkpoint_id"]),
            gate_id=str(data["gate_id"]),
            attempt=data["attempt"],
            requirements=dict(data["requirements"]),
            worker_sessions=[dict(e) for e in data["worker_sessions"]],
            changed_files=[str(p) for p in data["changed_files"]],
            deterministic_checks=[
                dict(e) for e in data["deterministic_checks"]
            ],
            red_evidence=(
                dict(data["red_evidence"]) if data["red_evidence"] is not None else None
            ),
            green_evidence=(
                dict(data["green_evidence"])
                if data["green_evidence"] is not None
                else None
            ),
            cumulative_gate=(
                dict(data["cumulative_gate"])
                if data["cumulative_gate"] is not None
                else None
            ),
            implementation_evidence=(
                dict(data["implementation_evidence"])
                if data.get("implementation_evidence") is not None
                else None
            ),
            supervisor_session_id=session_id,
            supervisor_decision=decision,
            repair_lineage=[int(a) for a in data["repair_lineage"]],
            timestamps=dict(data["timestamps"]),
        )

    # -- evidence predicates ------------------------------------------------------

    def is_passed(self) -> bool:
        """True iff ACCEPT + GREEN + cumulative + all deterministic checks ok.

        This is the ONLY predicate the engine uses to allow a PASSED
        transition.
        """
        if self.supervisor_decision != "ACCEPT":
            return False
        if self.green_evidence is None:
            return False
        if self.cumulative_gate is None:
            return False
        for check in self.deterministic_checks:
            if not isinstance(check, dict) or not check.get("ok", False):
                return False
        return True


def g1_evidence_is_sufficient(rec: GateExecutionRecord) -> bool:
    """G1 (Behavior/TDD) needs is_passed() AND proven RED before GREEN."""
    return rec.is_passed() and rec.red_evidence is not None


def evidence_is_sufficient(gate_id: str, rec: GateExecutionRecord) -> bool:
    """Gate-kind-aware sufficiency used by the engine's PASSED invariant."""
    if rec.gate_id != gate_id:
        return False
    if gate_id == "G1_behavior":
        return g1_evidence_is_sufficient(rec)
    return rec.is_passed()
