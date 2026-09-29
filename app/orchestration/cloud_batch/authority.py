"""P16 — Authority bridge: mutation classification, checkpoint lowering, dispatch routing.

P14 is ORCHESTRATION. The checkpoint runtime is AUTHORITY. A cloud-planned
MUTATING delegation may never be dispatched directly by the batch runtime:
it must first LOWER into a canonical :class:`CheckpointPlan` intent that
runs under G0 human approval + the G1-G4 gates. Only an all-READ-ONLY
batch dispatches directly (under the unchanged P13/P14 controls). The
enforcement guard itself lives at the real dispatch choke point
(:meth:`BatchDispatcher.run_batch`), so the invariant cannot be bypassed
by calling the dispatcher directly; this module provides the classifier,
the deterministic lowering, and :func:`dispatch_batch` — the routing
entry point callers should use.

Classification is derived from the ROLE_MANIFEST stated capabilities
(:mod:`roles.py`): read-only investigation/review roles vs "write
tests"/"edit"/"bounded repair" roles. Unknown roles FAIL CLOSED
(ValueError) — a never-seen role is never defaulted to read-only.

Lowering is PURE: :func:`lower_batch_to_checkpoint` never writes and
never submits. The lowered ``CheckpointPlan`` passes the canonical
``validate_checkpoint_plan`` or the lowering itself raises
:class:`LoweringError`. Gate-state writes / submission belong to the
human surface + canonical submission (P8-P13), not here.

Routing (:func:`dispatch_batch`) is crash-safe and idempotent: a batch's
lowered intent persists once under ``projects/<id>/authority/`` and is
reused (never re-lowered) on resume; mutating delegations receive durable
:class:`~app.orchestration.cloud_batch.schemas.WorkerOutcome` records with
status :data:`ROUTED_STATUS` so the cloud reentry sees them as terminal
("nothing ran; everything routed") while the checkpoint runtime remains
the sole authority for their acceptance.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import TYPE_CHECKING, Any

from app.orchestration.agy_runstate import safe_name
from app.orchestration.checkpoint_planning.schemas import (
    Checkpoint,
    CheckpointGates,
    CheckpointPlan,
    GateExpectations,
    PlannerProvenance,
)
from app.orchestration.checkpoint_planning.validation import (
    PlanValidationError,
    topological_order,
    validate_checkpoint_plan,
)
from app.orchestration.cloud_batch.outcome import build_batch_outcome
from app.orchestration.cloud_batch.schemas import (
    BatchPlan,
    ReentryGate,
    WorkerOutcome,
)

if TYPE_CHECKING:  # pragma: no cover - typing-only, avoids the import cycle
    from app.orchestration.cloud_batch.dispatcher import WorkerRunner
    from app.orchestration.cloud_batch.protocol import CloudProtocol
    from app.orchestration.cloud_batch.state import ProjectStore

# ---------------------------------------------------------------------------
# Mutation classification
# ---------------------------------------------------------------------------


class MutationClass(str, Enum):
    """Whether a delegation may write to the repository."""

    READ_ONLY = "READ_ONLY"
    MUTATING = "MUTATING"


#: Derived from ROLE_MANIFEST stated capabilities (roles.py):
#: read-only investigation/review vs "write tests"/"edit"/"bounded repair".
ROLE_CLASSIFICATION: dict[str, MutationClass] = {
    "codebase-investigator": MutationClass.READ_ONLY,
    "ui-reviewer": MutationClass.READ_ONLY,
    "adversarial-reviewer": MutationClass.READ_ONLY,
    "test-author": MutationClass.MUTATING,
    "implementer": MutationClass.MUTATING,
    "fixer": MutationClass.MUTATING,
}

#: Persisted WorkerOutcome status for a delegation routed to the checkpoint
#: runtime (lowered, awaiting G0/gates). Never a P13 TaskStatus — the
#: dispatcher tolerates and satisfies-maps it on resume (see dispatcher.py).
ROUTED_STATUS = "CHECKPOINT_ROUTED"


def _content_hash(batch: "BatchPlan") -> str:
    """Deterministic identity of a batch's CONTENT (review H-6).

    A lowering intent is per-batch-CONTENT: a re-issued ``batch_id`` with
    any different plan content must inherit nothing from the prior intent
    (fail closed — the prior intent may already have been human-submitted
    and executed, so silent reuse would drop the new mutation or corrupt
    the audit link). Canonical sorted-keys JSON of the full plan dump.
    """
    canonical = json.dumps(batch.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class MutationClassificationError(RuntimeError):
    """A direct dispatch was refused because the batch carries mutating
    delegations (fail closed — they must go through :func:`dispatch_batch`
    lowering instead). The message names the offending ids/roles."""


class LoweringError(RuntimeError):
    """A mutating batch could not be lowered into a valid checkpoint
    intent (fail closed — a partial/invalid intent is never persisted)."""


def classify_delegation(spec: Any) -> MutationClass:
    """Classify one delegation spec by its role.

    ``spec`` is duck-typed: anything with a ``.role`` attribute (a real
    :class:`~app.orchestration.cloud_batch.schemas.DelegationSpec` works).
    Unknown roles raise :class:`ValueError` — fail closed, never defaults.
    """
    try:
        return ROLE_CLASSIFICATION[spec.role]
    except KeyError as exc:
        raise ValueError(f"unknown role {spec.role!r}") from exc


# ---------------------------------------------------------------------------
# Lowering (pure + deterministic; never writes, never submits)
# ---------------------------------------------------------------------------


def _transitive_held_ids(
    batch: Any, mutating_ids: set[str], read_only_ids: list[str]
) -> list[str]:
    """Read-only ids that (transitively) depend on a mutating id.

    Held read-only delegations are NOT dispatched on the reduced batch:
    their premise is the mutation the checkpoint runtime has not yet
    authorized. They are listed in the lowering record (``held_ids``) and
    re-planned by the next cloud batch."""
    deps_by_id = {d.id: list(getattr(d, "deps", []) or []) for d in batch.delegations}

    def touches_mutating(tid: str, visited: set[str]) -> bool:
        for dep in deps_by_id.get(tid, ()):  # unknown deps already fail-closed
            if dep in mutating_ids:
                return True
            if dep not in visited:
                visited.add(dep)
                if touches_mutating(dep, visited):
                    return True
        return False

    return [rid for rid in read_only_ids if touches_mutating(rid, {rid})]


def lower_batch_to_checkpoint(
    *, project_id: str, batch: "BatchPlan", base_sha: str
) -> dict:
    """Pure + deterministic lowering of a batch's MUTATING delegations into
    ONE canonical CheckpointPlan intent. Never writes, never submits.

    Returns ``{"plan_id", "batch_id", "project_id", "base_sha",
    "read_only", "mutating", "held_ids", "checkpoint_plan"}`` where
    ``checkpoint_plan`` is a :class:`CheckpointPlan` that passes the
    canonical ``validate_checkpoint_plan`` (or the lowering raised
    :class:`LoweringError` first). A batch with NO mutating delegations
    lowers to ``checkpoint_plan=None`` (nothing to lower).
    """
    mutating: dict[str, Any] = {}
    read_only: list[str] = []
    for spec in batch.delegations:
        kind = classify_delegation(spec)
        if kind is MutationClass.MUTATING:
            mutating[spec.id] = spec
        else:
            read_only.append(spec.id)

    plan_id = f"p16-{project_id}-{batch.batch_id}"
    held_ids = _transitive_held_ids(batch, set(mutating), read_only)

    if not mutating:
        return {
            "plan_id": plan_id,
            "batch_id": batch.batch_id,
            "project_id": project_id,
            "base_sha": base_sha,
            "read_only": [d.id for d in batch.delegations],
            "mutating": [],
            "held_ids": [],
            "checkpoint_plan": None,
        }

    # Mutating subgraph: deps restricted to M. Unknown deps fail closed
    # (read-only deps are legitimately dropped — they never gate the
    # checkpoint execution order).
    all_ids = {d.id for d in batch.delegations}
    deps_by_id: dict[str, list[str]] = {}
    for mid in mutating:
        raw_deps = [dep for dep in (mutating[mid].deps or [])]
        unknown = [dep for dep in raw_deps if dep not in all_ids]
        if unknown:
            raise LoweringError(
                f"delegation {mid!r}: unknown dependency {unknown[0]!r}"
            )
        deps_by_id[mid] = [dep for dep in raw_deps if dep in mutating]

    try:
        order = topological_order(list(mutating), deps_by_id)
    except PlanValidationError as exc:
        raise LoweringError(f"mutating subgraph is not orderable: {exc}") from exc

    by_id = {d.id: d for d in batch.delegations}
    checkpoints: list[Checkpoint] = []
    for mid in order:
        spec = mutating[mid]
        verification = [str(v).strip() for v in (spec.verification or []) if str(v).strip()]
        if not verification:
            raise LoweringError(
                f"delegation {mid!r}: mutating delegation has no acceptance "
                "verification — cannot lower without a G1 acceptance criterion"
            )
        invariants = [str(i) for i in (spec.invariants or [])]
        context = "\n".join(
            [
                f"Cloud batch {batch.batch_id}: {batch.objective}",
                f"Delegation {mid} ({spec.role}): {spec.objective}",
                "Mutating dependencies: " + (", ".join(deps_by_id[mid]) or "none"),
                "Invariants: " + ("; ".join(invariants) if invariants else "none"),
            ]
        )
        checkpoints.append(
            Checkpoint(
                checkpoint_id=mid,
                title=spec.objective[:80],
                objective=spec.objective,
                dependencies=list(deps_by_id[mid]),
                implementation_context=context,
                relevant_files=list(spec.owned_files),
                stages=[by_id[mid].id],
                gates=CheckpointGates(
                    G1_behavior=GateExpectations(
                        required=True, planned_tests=verification
                    ),
                    G2_ui=GateExpectations(
                        required=False,
                        reason=(
                            "no UI surface declared by the batch lowering "
                            "(correct applicability, not forced)"
                        ),
                    ),
                    G3_adversarial=GateExpectations(required=True, focus=invariants),
                    G4_human=GateExpectations(
                        required=True,
                        reason=(
                            "mutating batch work requires human acceptance "
                            "before it becomes project work"
                        ),
                    ),
                ),
            )
        )

    checkpoint_plan = CheckpointPlan(
        plan_id=plan_id,
        feature_id=batch.batch_id,
        objective=batch.objective,
        planner=PlannerProvenance(
            role="cloud",
            provider="gemini",
            model=None,
            fallback_used=False,
            planner_session=None,
        ),
        checkpoints=checkpoints,
        g0=None,  # approval is human-owned, supplied at submit time (P13)
    )

    verdict = validate_checkpoint_plan(checkpoint_plan)
    if not verdict.ok:
        raise LoweringError(
            "lowered checkpoint plan failed canonical validation: "
            + "; ".join(verdict.errors)
        )

    return {
        "plan_id": plan_id,
        "batch_id": batch.batch_id,
        "project_id": project_id,
        "base_sha": base_sha,
        "read_only": read_only,
        "mutating": [d.id for d in batch.delegations if d.id in mutating],
        "held_ids": held_ids,
        "checkpoint_plan": checkpoint_plan,
    }


# ---------------------------------------------------------------------------
# Routing (the P16 dispatch entry point; all writes via ProjectStore)
# ---------------------------------------------------------------------------


def dispatch_batch(
    *,
    store: "ProjectStore",
    project_id: str,
    protocol: "CloudProtocol | None",
    batch_id: str,
    runner: "WorkerRunner",
    base_sha: str = "0" * 40,
    workdir: str = ".",
) -> dict:
    """THE dispatch entry point for a P14 batch under P16 authority rules.

    Returns ``{"routed": bool, "reason": str, "lowered": dict | None}``.

    * All-read-only batch -> the direct path (``routed=False``): a
      :class:`BatchDispatcher` built with ``max_fixers=0`` executes it
      (the direct path NEVER spawns fixers — a read-only batch must not
      gain a mutating repair; a failure escalates via the reentry reason).
    * Any mutating delegation -> lowered into ONE canonical checkpoint
      intent (persisted once, reused idempotently), the mutating ids get
      durable :data:`ROUTED_STATUS` outcomes (persisted BEFORE any reentry
      mark), only the read-only delegations that do NOT transitively
      depend on a mutating one run, under an IN-MEMORY reduced BatchPlan —
      the canonical ``plan.json`` is never rewritten — and the reduced
      batch's reentry reason is returned.

    Never invokes the cloud. All writes go through the ProjectStore.
    """
    # Lazy import: dispatcher imports this module at module level (guard);
    # this direction keeps the package import graph acyclic at load time.
    from app.orchestration.cloud_batch.dispatcher import BatchDispatcher

    plan = store.load_batch_plan(project_id, batch_id)

    mutating_ids: list[str] = []
    read_only_ids: list[str] = []
    for spec in plan.delegations:
        kind = classify_delegation(spec)
        if kind is MutationClass.MUTATING:
            mutating_ids.append(spec.id)
        else:
            read_only_ids.append(spec.id)

    if not mutating_ids:
        dispatcher = BatchDispatcher(
            store,
            project_id,
            runner=runner,
            protocol=protocol,
            base_sha=base_sha,
            workdir=workdir,
            max_fixers=0,
        )
        reason = dispatcher.run_batch(batch_id)
        return {"routed": False, "reason": reason, "lowered": None}

    # -- lowering path ---------------------------------------------------
    # Idempotent resume: an existing lowered intent is REUSED (never
    # re-lowered); the record carries plan_id. The durability ref the
    # routed outcomes cite is the record path under the project dir.
    record = store.load_lowered_intent(project_id, batch_id)
    plan_hash = _content_hash(plan)
    if not record:
        lowered = lower_batch_to_checkpoint(
            project_id=project_id, batch=plan, base_sha=base_sha
        )
        plan_id = str(lowered["plan_id"])
        record = {
            key: (
                value.model_dump(mode="json")
                if key == "checkpoint_plan" and value is not None
                else value
            )
            for key, value in lowered.items()
        }
        record["content_hash"] = plan_hash
        saved_path = store.save_lowered_intent(project_id, batch_id, record)
    else:
        # Content identity (review H-6): the persisted intent is bound to
        # the EXACT plan content that produced it. A re-issued batch_id
        # with different content must NOT inherit the stale intent (it may
        # already have been human-submitted and executed) — fail closed
        # before any state write. Identical content = idempotent resume.
        # Records predating content_hash carry None and are accepted.
        if record.get("content_hash") not in (None, plan_hash):
            raise LoweringError(
                f"batch {batch_id!r} already has a lowering intent for "
                "different content — a re-issued batch id must not inherit "
                "a stale checkpoint intent (content hash mismatch)"
            )
        plan_id = str(record.get("plan_id") or f"p16-{project_id}-{batch_id}")
        saved_path = (
            store.authority_dir(project_id) / f"{safe_name(batch_id)}-lowering.json"
        )
    record_path = str(saved_path.relative_to(store.project_dir(project_id)))

    mutating = [str(m) for m in record.get("mutating", [])]
    held = [str(h) for h in record.get("held_ids", [])]

    # R' = read-only ids that do NOT transitively depend on M (held ids
    # are NOT dispatched — they await the checkpoint path; the next cloud
    # batch re-plans them).
    held_set = set(held)
    r_prime_ids = [
        rid for rid in record.get("read_only", read_only_ids) if rid not in held_set
    ]

    # -- routed outcomes BEFORE any reentry mark (crash-safe ordering) ----
    persisted = store.load_worker_outcomes(project_id, batch_id)
    specs_by_id = {d.id: d for d in plan.delegations}
    for mid in mutating:
        if mid in persisted:
            continue  # idempotent resume: the routed marker already exists
        store.save_worker_outcome(
            project_id,
            batch_id,
            WorkerOutcome(
                delegation_id=mid,
                role=specs_by_id[mid].role,
                status=ROUTED_STATUS,
                conclusion=(
                    f"lowered to checkpoint plan {plan_id} — awaiting "
                    "G0/gates"
                ),
                evidence_refs=[record_path],
            ),
        )

    if r_prime_ids:
        r_set = set(r_prime_ids)
        reduced = BatchPlan(
            batch_id=plan.batch_id,
            objective=plan.objective,
            delegations=[
                specs_by_id[rid].model_copy(
                    update={"deps": [dep for dep in specs_by_id[rid].deps if dep in r_set]}
                )
                for rid in r_prime_ids
            ],
            parallelism=plan.parallelism,
            # Reduced gate: required ids pruned to R' — an empty set means
            # "all of R'" (ReentryGate semantics). The canonical plan.json
            # is NEVER rewritten: this is an in-memory projection only.
            reentry=ReentryGate(
                reason=plan.reentry.reason,
                required_ids=[
                    rid
                    for rid in plan.reentry.required_ids
                    if rid in r_set
                ],
            ),
        )
        dispatcher = BatchDispatcher(
            store,
            project_id,
            runner=runner,
            protocol=protocol,
            base_sha=base_sha,
            workdir=workdir,
            max_fixers=0,
        )
        # plan override: run the reduced (read-only-only) projection.
        reason = dispatcher.run_batch(batch_id, plan=reduced)
    else:
        # Nothing ran; everything is routed. The dispatcher did not run, so
        # the BatchOutcome is persisted here, then the reentry mark is
        # emitted (deterministic, no cloud). The outcome is REQUIRED:
        # request_reentry reserves the generation's single cloud call
        # BEFORE reading outcome.json — persisting it first is the only
        # ordering that lets a clean reentry (or a clean fail-closed) happen
        # instead of burning the call on a FileNotFoundError (review H-2).
        reason = "ALL_REQUIRED_DELEGATIONS_COMPLETE"
        routed = store.load_worker_outcomes(project_id, batch_id)
        outcome = build_batch_outcome(
            batch_id,
            plan.objective,
            [routed[tid] for tid in sorted(routed)],
            reentry_reason=reason,
        )
        store.save_batch_outcome(project_id, batch_id, outcome)
        if protocol is not None:
            protocol.mark_reentry_ready(batch_id, reason)

    return {"routed": True, "reason": reason, "lowered": record}


__all__ = [
    "MutationClass",
    "ROLE_CLASSIFICATION",
    "ROUTED_STATUS",
    "MutationClassificationError",
    "LoweringError",
    "classify_delegation",
    "lower_batch_to_checkpoint",
    "dispatch_batch",
]
