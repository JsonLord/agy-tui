"""P8 checkpoint-plan validation (worker A).

Deterministic, closed-world validation with EIGHT ordered checks. Every
error string is prefixed with the failing checkpoint/gate id so callers
can attribute failures without re-parsing.

``topological_order`` is exposed standalone (worker B's todo projection
reuses it); ``checkpoint_execution_order`` derives the plan-level
deterministic order (stable: ties broken by checkpoint_id sort).

No network, no repo mutation, no host runtime import.
"""

from __future__ import annotations

from typing import Iterable

from pydantic import ValidationError

from app.orchestration.checkpoint_planning.schemas import (
    CHECKPOINT_ID_RE,
    GATE_KINDS,
    CheckpointPlan,
)


class PlanValidationError(Exception):
    """Raised by :func:`topological_order` on unknown dep, self-dep, or cycle."""


class ValidationResult:
    """Aggregate result of :func:`validate_checkpoint_plan`."""

    __slots__ = ("ok", "errors")

    def __init__(self, ok: bool, errors: list[str] | None = None) -> None:
        self.ok = ok
        self.errors: list[str] = list(errors or [])

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.ok

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ValidationResult(ok={self.ok}, errors={self.errors!r})"


# ---------------------------------------------------------------------------
# Standalone DAG helper
# ---------------------------------------------------------------------------


def topological_order(ids: Iterable[str], deps: dict[str, Iterable[str]]) -> list[str]:
    """Deterministic Kahn topological sort.

    ``ids``: node ids. ``deps[id]``: set/list of prerequisite ids.
    Ties broken by lexicographic id sort. Raises :class:`PlanValidationError`
    for self-dependencies, unknown dependencies, or cycles (naming the
    remaining cycle members).
    """
    node_ids = list(ids)
    known = set(node_ids)
    remaining: dict[str, set[str]] = {}

    for node in node_ids:
        node_deps = set(deps.get(node, ()) or ())
        if node in node_deps:
            raise PlanValidationError(f"{node}: self-dependency on {node}")
        unknown = sorted(d for d in node_deps if d not in known)
        if unknown:
            raise PlanValidationError(
                f"{node}: unknown dependency {unknown[0]}"
                + (f" (also {', '.join(unknown[1:])})" if len(unknown) > 1 else "")
            )
        remaining[node] = node_deps

    order: list[str] = []
    while remaining:
        ready = sorted(n for n, d in remaining.items() if not d)
        if not ready:
            cycle_members = ", ".join(sorted(remaining))
            raise PlanValidationError(f"dependency cycle among: {cycle_members}")
        first = ready[0]
        order.append(first)
        del remaining[first]
        for d in remaining.values():
            d.discard(first)

    return order


def checkpoint_execution_order(plan: CheckpointPlan) -> list[str]:
    """Deterministic checkpoint order for a plan (ties -> id sort)."""
    ids = [cp.checkpoint_id for cp in plan.checkpoints]
    deps: dict[str, Iterable[str]] = {
        cp.checkpoint_id: list(cp.dependencies) for cp in plan.checkpoints
    }
    return topological_order(ids, deps)


# ---------------------------------------------------------------------------
# The eight ordered checks
# ---------------------------------------------------------------------------


def validate_checkpoint_plan(plan: CheckpointPlan) -> ValidationResult:
    """Run all eight ordered checks; collect every error (fail-loud).

    1. checkpoint_id regex + uniqueness
    2. every dependency exists
    3. no self-dependency; no cycle (cycle reported)
    4. deterministic execution order derivable (topological sort)
    5. every gate kind explicitly accounted for (reason when required=False)
    6. required G1 needs non-empty planned_tests
    7. planner provenance valid and internally consistent
    8. revision is an int >= 1
    """
    errors: list[str] = []

    # -- 1. id format + uniqueness -----------------------------------------
    seen: set[str] = set()
    for cp in plan.checkpoints:
        cid = cp.checkpoint_id
        if not CHECKPOINT_ID_RE.match(cid):
            errors.append(f"{cid or '<empty>'}: checkpoint_id must match "
                          "^[a-z0-9][a-z0-9-]*$")
        if cid in seen:
            errors.append(f"{cid}: duplicate checkpoint_id")
        seen.add(cid)

    # -- 2/3/4. dependency graph --------------------------------------------
    for cp in plan.checkpoints:
        for dep in cp.dependencies:
            if dep == cp.checkpoint_id:
                errors.append(f"{cp.checkpoint_id}: self-dependency on {dep}")
            elif dep not in seen:
                errors.append(f"{cp.checkpoint_id}: unknown dependency {dep}")

    try:
        checkpoint_execution_order(plan)
    except PlanValidationError as exc:
        message = str(exc)
        # Avoid double-reporting self-deps already caught above.
        if "self-dependency" not in message:
            errors.append(f"PLAN: {message}")

    # -- 5. gate completeness (schema guarantees presence of all four kinds;
    #       this catches a missing attribute / structural tampering) ---------
    for cp in plan.checkpoints:
        present = {}
        for kind in GATE_KINDS:
            gate = getattr(cp.gates, kind, None) if hasattr(cp.gates, kind) else None
            if gate is None:
                present[kind] = None
                errors.append(
                    f"{cp.checkpoint_id}:{kind}: missing gate configuration "
                    "(every gate must be explicitly accounted for)"
                )
            else:
                present[kind] = gate
        for kind, gate in present.items():
            if gate is None:
                continue
            if not gate.required and not (gate.reason or "").strip():
                errors.append(
                    f"{cp.checkpoint_id}:{kind}: required=False needs a reason "
                    "(silent omission rejected)"
                )

    # -- 6. required G1 needs planned tests ----------------------------------
    for cp in plan.checkpoints:
        g1 = getattr(cp.gates, "G1_behavior", None)
        if g1 is None:
            continue  # missing G1 already reported by check 5
        if g1.required and not [t for t in g1.planned_tests if t.strip()]:
            errors.append(
                f"{cp.checkpoint_id}:G1_behavior: required G1 needs non-empty "
                "planned_tests"
            )

    # -- 7. planner provenance ------------------------------------------------
    planner = plan.planner
    planner_errors: list[str] = []
    if planner.provider not in ("gemini", "mimo"):
        planner_errors.append(
            f"PLAN:planner: provider must be 'gemini' or 'mimo', got {planner.provider!r}"
        )
    if planner.role not in ("cloud", "fallback"):
        planner_errors.append(
            f"PLAN:planner: role must be 'cloud' or 'fallback', got {planner.role!r}"
        )
    # fallback_used consistency: fallback_used <=> provider 'mimo'
    if planner.fallback_used and planner.provider != "mimo":
        planner_errors.append(
            "PLAN:planner: fallback_used=True requires provider 'mimo'"
        )
    if not planner.fallback_used and planner.provider == "mimo":
        planner_errors.append(
            "PLAN:planner: provider 'mimo' is the fallback planner; "
            "fallback_used must be True"
        )
    errors.extend(planner_errors)

    # -- 8. revision -----------------------------------------------------------
    if not isinstance(plan.revision, int) or isinstance(plan.revision, bool) \
            or plan.revision < 1:
        errors.append(f"PLAN:revision: must be an int >= 1, got {plan.revision!r}")

    return ValidationResult(ok=not errors, errors=errors)


def plan_from_payload(payload: dict) -> CheckpointPlan:
    """Parse a provider JSON payload into a CheckpointPlan.

    Wraps pydantic errors in :class:`PlanValidationError` so adapters can
    map them onto ``PlannerUnavailable`` without leaking tracebacks.
    """
    try:
        return CheckpointPlan.model_validate(payload)
    except ValidationError as exc:
        raise PlanValidationError(f"plan payload invalid: {exc}") from exc
