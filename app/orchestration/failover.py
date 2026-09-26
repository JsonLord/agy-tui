"""P3 role-preserving provider failover (wrapper around the fixed
``OpenAICompatibleClient``).

Contract summary:

* :func:`is_transient_provider_failure` — pure TRANSIENT classifier.
  True iff HTTP status is one of 429/502/503/504/520..529, or the
  status is ``None`` and the error message matches a rate-limit /
  quota / overload / capacity / busy pattern. Everything else (4xx
  auth/schema/permission, generic 500, deterministic test/coding
  failures) is NOT transient and is re-raised immediately with no
  retry and no failover.
* :func:`classify_reason` — maps a transient condition onto exactly
  one of the four ``fallback_reason`` enum values
  (``rate_limit_exhausted``, ``quota_exhausted``,
  ``provider_overload``, ``transient_busy``).
* ``PRIMARY_MAX_ATTEMPTS = 4`` (initial attempt + 3 retries), then
  exactly ONE fallback-model call. Same role throughout: only the
  bound model changes, never the role semantics.
* :func:`complete_with_failover` produces at most one schema-shaped
  failover event record (see
  ``state/autopilot/agy-failover-state.schema.json``). ``session_id``
  is ``None`` at this layer: OpenCode-layer failover (see
  ``agy_runtime``) owns the real session continuity and fills it in.

:class:`app.providers.OpenAICompatibleClient` is unchanged — this
module only wraps it. No network in tests: the client/transport is
always injected.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable, Sequence

from app.config import AgySettings
from app.providers import (
    ChatResult,
    ModelRole,
    OpenAICompatibleClient,
    ProviderError,
    RoleModels,
    Transport,
)

# Primary budget: initial attempt + 3 retries = max 4 attempts on
# the explicitly classified transient failures only.
PRIMARY_MAX_ATTEMPTS = 4

# Deterministic backoff step (seconds) requested from the injected
# sleep callable between primary retries. The default sleep callable
# is a no-op, so nothing ever really sleeps unless a caller wires a
# real one in.
PRIMARY_BACKOFF_SECONDS = 0.25

TRANSIENT_STATUSES = frozenset({429, 502, 503, 504} | set(range(520, 530)))

OVERLOAD_STATUSES = frozenset({502, 503, 504} | set(range(520, 530)))

_TRANSIENT_MESSAGE_RE = re.compile(
    r"rate|quota|overload|capacity|too many requests|server busy",
    re.IGNORECASE,
)

REASON_RATE_LIMIT = "rate_limit_exhausted"
REASON_PROVIDER_OVERLOAD = "provider_overload"
REASON_QUOTA = "quota_exhausted"
REASON_TRANSIENT_BUSY = "transient_busy"

FAILOVER_REASONS = (
    REASON_RATE_LIMIT,
    REASON_PROVIDER_OVERLOAD,
    REASON_QUOTA,
    REASON_TRANSIENT_BUSY,
)


def is_transient_provider_failure(err: ProviderError) -> bool:
    """
    Pure TRANSIENT classifier.

    True iff the HTTP status is a known transient condition (429,
    502, 503, 504, 520..529) OR the status is ``None`` and the error
    message matches the transient rate-limit/quota/overload/capacity/
    busy pattern. A malformed-but-200 payload carries status 200 and
    is never transient. 4xx auth/schema/permission errors, generic
    500s, test failures and deterministic coding failures never fail
    over.
    """
    if err.status is not None:
        return err.status in TRANSIENT_STATUSES

    return bool(_TRANSIENT_MESSAGE_RE.search(str(err)))


def classify_reason(
    status_or_none: int | None,
    body_or_none: str | None,
) -> str:
    """
    Classify ONE transient condition into the ``fallback_reason``
    enum (ordered, deterministic):

    1. ``'quota'`` anywhere in the body -> ``quota_exhausted``;
    2. HTTP 429 or a ``rate``/'rate limit' body ->
       ``rate_limit_exhausted``;
    3. 502/503/504/520..529 or an overload/capacity body ->
       ``provider_overload``;
    4. anything else transient -> ``transient_busy``.
    """
    text = (body_or_none or "").lower()

    if "quota" in text:
        return REASON_QUOTA

    if status_or_none == 429 or "rate" in text:
        return REASON_RATE_LIMIT

    if (
        status_or_none is not None and status_or_none in OVERLOAD_STATUSES
    ) or "overload" in text or "capacity" in text:
        return REASON_PROVIDER_OVERLOAD

    return REASON_TRANSIENT_BUSY


def _noop_sleep(_seconds: float) -> None:
    """Default backoff sleeper: deterministic no-op, never sleeps."""


def fallback_models(role: ModelRole, settings: AgySettings) -> RoleModels:
    """
    RoleModels for the fallback of ONE role: only the named role's
    model is swapped to its fallback binding; the other role keeps
    its primary binding (callers only ever complete on ``role``).
    """
    primary = RoleModels.for_agy(settings)

    if role is ModelRole.SUPERVISOR:
        return RoleModels(
            supervisor_model=settings.supervisor_fallback_model,
            dispatch_model=primary.dispatch_model,
        )

    if role is ModelRole.DISPATCH:
        return RoleModels(
            supervisor_model=primary.supervisor_model,
            dispatch_model=settings.dispatch_fallback_model,
        )

    raise ValueError(f"Unknown model role: {role!r}")


def build_failover_event(
    *,
    role: ModelRole,
    primary_model: str,
    fallback_model: str,
    fallback_reason: str,
    session_id: str | None = None,
    parent_session_id: str | None = None,
    fallback_count: int = 1,
    primary_attempts: int = PRIMARY_MAX_ATTEMPTS,
    ts: str | None = None,
) -> dict[str, Any]:
    """
    Schema record for ``agy-failover-state.schema.json`` (exactly the
    schema's property set; the file itself is the append-only array
    of these records). ``active_model`` is the fallback model because
    an event only exists after a successful failover.
    """
    if fallback_reason not in FAILOVER_REASONS:
        raise ValueError(
            f"fallback_reason must be one of {FAILOVER_REASONS}, "
            f"got {fallback_reason!r}"
        )

    return {
        "role": role.value,
        "primary_model": primary_model,
        "fallback_model": fallback_model,
        "active_model": fallback_model,
        "primary_attempts": primary_attempts,
        "fallback_reason": fallback_reason,
        "session_id": session_id,
        "fallback_count": fallback_count,
        "parent_session_id": parent_session_id,
        "ts": ts or datetime.now().astimezone().isoformat(),
    }


def _make_fallback_client(
    client: OpenAICompatibleClient,
    models: RoleModels,
    transport: Transport | None,
) -> Any:
    """
    Second client over the SAME endpoint/transport with the role's
    fallback model bound. Duck-typed test doubles may expose a
    ``with_models(models)`` construction hook instead of the private
    endpoint; anything else is a wiring bug and raises.
    """
    endpoint = getattr(client, "_endpoint", None)

    if endpoint is None:
        hook = getattr(client, "with_models", None)

        if callable(hook):
            return hook(models)

        raise TypeError(
            "client provides neither an endpoint nor a "
            f"with_models() fallback seam: {type(client).__name__}"
        )

    if transport is None:
        transport = getattr(client, "_transport", None)

    return OpenAICompatibleClient(endpoint, models, transport=transport)


async def complete_with_failover(
    client: OpenAICompatibleClient,
    *,
    role: ModelRole,
    messages: Sequence[Any],
    settings: AgySettings,
    transport: Transport | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    sleep: Callable[[float], None] | None = None,
) -> tuple[ChatResult, dict[str, Any] | None]:
    """
    One provider call with the role-preserving failover runtime.

    Behavior (deterministic):

    1. Attempt 1..4 on the role's PRIMARY model via
       ``client.complete(role, ...)``.
    2. On :class:`ProviderError`: NOT transient -> re-raise
       immediately (zero failover, zero retry). Transient -> retry the
       same model (provider calls are stateless; same-session
       continuity lives at the OpenCode layer, see ``agy_runtime``)
       with a tiny deterministic backoff handed to ``sleep`` (default
       no-op — tests never sleep for real).
    3. After the 4th failed primary attempt: exactly ONE call on the
       role's FALLBACK model through a second client over the SAME
       endpoint/transport. If that also raises -> re-raise (no
       infinite loop).
    4. On successful failover: build the schema-shaped event, hand it
       to ``on_event`` once, return ``(result, event)``. No failover
       happened -> ``(result, None)``.
    """
    sleeper = sleep if sleep is not None else _noop_sleep
    primary_models = RoleModels.for_agy(settings)

    complete_kwargs: dict[str, Any] = {}

    if temperature is not None:
        complete_kwargs["temperature"] = temperature

    if max_tokens is not None:
        complete_kwargs["max_tokens"] = max_tokens

    last_error: ProviderError | None = None

    for attempt in range(1, PRIMARY_MAX_ATTEMPTS + 1):
        try:
            result = client.complete(role, messages, **complete_kwargs)
            return result, None

        except ProviderError as err:
            if not is_transient_provider_failure(err):
                raise

            last_error = err

            if attempt < PRIMARY_MAX_ATTEMPTS:
                sleeper(PRIMARY_BACKOFF_SECONDS * attempt)

    assert last_error is not None  # loop always ran at least once

    fb_models = fallback_models(role, settings)
    fb_client = _make_fallback_client(client, fb_models, transport)

    # Exactly 4 primary attempts + 1 fallback attempt have now been
    # issued; a failing fallback call raises out of here directly.
    result = fb_client.complete(role, messages, **complete_kwargs)

    event = build_failover_event(
        role=role,
        primary_model=primary_models.model_for(role),
        fallback_model=fb_models.model_for(role),
        fallback_reason=classify_reason(
            last_error.status,
            last_error.body or str(last_error),
        ),
        session_id=None,  # the OpenCode layer fills the real session
        parent_session_id=None,
        fallback_count=1,
        primary_attempts=PRIMARY_MAX_ATTEMPTS,
    )

    if on_event is not None:
        on_event(event)

    return result, event