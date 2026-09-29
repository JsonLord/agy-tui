"""P15-B — Human provenance bridge for parked cloud projects.

The bridge is the ONLY component that turns a human-submitted response
into a canonical :class:`CloudProtocol` resume, and it adds no cloud
capability of its own: the protocol already refuses cloud calls from
``WAITING_FOR_USER`` and ``NEEDS_HUMAN``, and every path here is
deterministic (zero model calls, zero network, zero sleeps, zero
polling).

Non-negotiable invariants (all fail closed):

* HUMAN-ORIGIN ONLY. A response must be a :class:`HumanResponse` whose
  ``actor``/``origin`` provenance was schema-validated (actor
  fullmatches ``human:[a-z0-9_-]+``). Anything else raises
  :class:`NotHumanOrigin` BEFORE any state is touched — no surface can
  launder a model payload through a different ``origin`` field.
* RESUME EXACTLY ONCE (idempotent). Each response is bound to a park
  generation (clarify: the single WAITING_FOR_USER episode per project;
  needs_human: the latest ``NNNN-needs_human.json`` artifact seq). A
  persisted answer already bound to the current generation makes any
  later/replayed submission raise :class:`DuplicateHumanResponse`.
* DURABLE BEFORE RESUME. The answer artifact is persisted BEFORE the
  protocol transition: a crash after persist leaves the park visible
  plus the recorded answer; a crash before leaves nothing.
* DATA, NOT INSTRUCTIONS. ``response.text`` is stored VERBATIM (as the
  protocol's ``record_user_answer`` already guarantees) and is never
  interpreted here.

Scope boundary: G4 approval gates live in the separate checkpoint
runtime; this module imports nothing from it and cannot reach a gate
engine (the protocol argument is type-checked: a cloud project only).
The park view and the durable answer files are cloud-project scoped.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.orchestration.cloud_batch.protocol import CloudProtocol
from app.orchestration.cloud_batch.schemas import CloudProjectState, HumanResponse

__all__ = [
    "HumanBridgeError",
    "NotHumanOrigin",
    "ParkNotAwaiting",
    "DuplicateHumanResponse",
    "park_view",
    "record_human_response",
]

#: The two parks a human may answer, and the state that makes each visible.
_PARK_KINDS = ("clarify", "needs_human")
_EXPECTED_STATE = {
    "clarify": CloudProjectState.WAITING_FOR_USER,
    "needs_human": CloudProjectState.NEEDS_HUMAN,
}


class HumanBridgeError(RuntimeError):
    """Base error for P15 human-bridge failures (fail closed)."""


class NotHumanOrigin(HumanBridgeError):
    """A non-human payload was presented — rejected before any state is
    touched (a model payload can never resume a project)."""


class ParkNotAwaiting(HumanBridgeError):
    """The project is not parked in the state the presented kind needs."""


class DuplicateHumanResponse(HumanBridgeError):
    """This park generation already received its durable response — a
    duplicate/late/replayed submission can never resume twice."""


def _require_kind(kind: Any) -> str:
    if kind not in _PARK_KINDS:
        raise HumanBridgeError(
            f"unknown park kind {kind!r} (expected one of {_PARK_KINDS})"
        )
    return str(kind)


def _require_protocol(protocol: Any) -> CloudProtocol:
    # Type gate: the bridge speaks ONLY the cloud project protocol. A
    # gate engine or any other object has no path through here.
    if not isinstance(protocol, CloudProtocol):
        raise HumanBridgeError(
            f"bridge accepts a CloudProtocol only, got {type(protocol).__name__}"
        )
    return protocol


def _latest_artifact_payload(store: Any, project_id: str, mode: str) -> dict:
    """Latest ``NNNN-<mode>.json`` cloud artifact read back FROM DISK
    ({} when absent/unreadable — the view degrades, never crashes)."""
    seq = store.latest_cloud_seq_for_mode(project_id, mode)
    if seq == 0:
        return {}
    path = store.cloud_dir(project_id) / f"{seq:04d}-{mode}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _park_generation(store: Any, project_id: str, kind: str) -> int:
    """The generation a response for ``kind`` binds to. The persisted
    answer envelope uses the same rule (see ProjectStore)."""
    if kind == "clarify":
        # The clarify park is the SINGLE WAITING_FOR_USER episode per
        # project (CLARIFY is only legal from NEW) -> generation 1.
        return 1
    return store.latest_cloud_seq_for_mode(project_id, "needs_human")


def park_view(protocol: CloudProtocol) -> dict:
    """What the human surface (local TUI OR remote cloud session) must
    show. Built from persisted state ONLY (contract + latest matching
    cloud artifact); read-only; never invokes the cloud.

    ``kind`` is None when the project is not awaiting a human.
    """
    protocol = _require_protocol(protocol)
    store = protocol.store
    project_id = protocol.project_id
    state = protocol.state()
    view: dict[str, Any] = {
        "project_id": project_id,
        "state": state.value,
        "kind": None,
        "questions": [],
        "question": None,
        "context": "",
        "options": [],
    }
    if state is CloudProjectState.WAITING_FOR_USER:
        artifact = _latest_artifact_payload(store, project_id, "clarify")
        questions = artifact.get("questions")
        view["kind"] = "clarify"
        view["questions"] = (
            [str(q) for q in questions if isinstance(q, str)]
            if isinstance(questions, list)
            else []
        )
    elif state is CloudProjectState.NEEDS_HUMAN:
        artifact = _latest_artifact_payload(store, project_id, "needs_human")
        question = artifact.get("question")
        options = artifact.get("options")
        view["kind"] = "needs_human"
        view["question"] = str(question).strip() if question else None
        view["context"] = str(artifact.get("context", "") or "")
        view["options"] = (
            [str(o) for o in options if isinstance(o, str)]
            if isinstance(options, list)
            else []
        )
    return view


def record_human_response(
    protocol: CloudProtocol, response: HumanResponse, kind: str
) -> dict:
    """Record ONE durable human response and resume the project exactly
    ONCE (all deterministic, NO cloud).

    Returns ``{"project_id", "kind", "resumed_to", "response_path",
    "actor"}``. Raises :class:`NotHumanOrigin` (before any state is
    touched), :class:`DuplicateHumanResponse` or
    :class:`ParkNotAwaiting` otherwise; the protocol itself performs
    the only state transition.
    """
    kind = _require_kind(kind)
    protocol = _require_protocol(protocol)

    # (1) Human provenance first — BEFORE any state is touched. A dict,
    # a model payload, or a mutated instance that no longer validates
    # all fail here; nothing downstream runs.
    if not isinstance(response, HumanResponse):
        raise NotHumanOrigin(
            f"payload is {type(response).__name__}, not a HumanResponse — "
            "only schema-validated human: actors may answer a park"
        )
    try:
        HumanResponse.model_validate(response.model_dump())
    except ValidationError as exc:
        raise NotHumanOrigin(
            f"response failed human-provenance revalidation: {exc}"
        ) from exc

    store = protocol.store
    project_id = protocol.project_id

    # (2) Idempotency: this park generation already has its durable
    # answer -> the park was (or is being) resumed once already.
    generation = _park_generation(store, project_id, kind)
    for prior in store.load_human_responses(project_id, kind):
        if int(prior.get("park_generation", -1)) == generation:
            raise DuplicateHumanResponse(
                f"{kind} park generation {generation} already received a "
                f"durable human answer (actor {prior.get('actor')!r}) — "
                "refusing to resume twice"
            )

    # (3) The park must actually be awaiting this kind.
    state = protocol.state()
    expected = _EXPECTED_STATE[kind]
    if state is not expected:
        raise ParkNotAwaiting(
            f"project {project_id!r} is {state.value}, not {expected.value} — "
            f"there is no {kind} park to answer"
        )

    # (4) Durable record BEFORE the transition (crash-safe ordering).
    response_path = store.save_human_response(project_id, response, kind)

    # (5) Resume exactly ONCE through the canonical protocol path.
    if kind == "clarify":
        protocol.record_user_answer(response.text)
    else:
        protocol.resume_from_needs_human()

    # (6) Receipt.
    return {
        "project_id": project_id,
        "kind": kind,
        "resumed_to": protocol.state().value,
        "response_path": str(response_path),
        "actor": response.actor,
    }


def _bridge_import_targets() -> list[str]:
    """Module names this bridge imports (for the boundary self-check)."""
    source = Path(__file__).read_text(encoding="utf-8")
    targets: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            targets.append(node.module)
    return targets
