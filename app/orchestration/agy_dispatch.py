"""
AGY DISPATCH-role seam (MiMo dispatch role, model chosen by role).

``dispatch_stage`` is the single API through which ONE bounded task
prompt is sent on the ``ModelRole.DISPATCH`` role. The role maps to
the concrete model through ``RoleModels``/``AgySettings``, so no model
name appears here — only roles.

P2 is a thin, testable seam: the HTTP transport is whatever the
client was built with (tests inject a fake transport; no network, no
live model call). P3/P4 binds the real dispatch execution loop and
optional Needle tool routing behind this function.
"""

from __future__ import annotations

from app.providers import (
    ChatMessage,
    ChatResult,
    ModelRole,
    OpenAICompatibleClient,
    Transport,
)


DISPATCH_SYSTEM = """You are the implementation worker for exactly ONE bounded task of a staged development plan.

Rules:
- implement only the bounded task supplied in the prompt: its files, its steps, its acceptance conditions;
- sibling stages, unrelated files and the rest of the plan are out of scope unless the prompt names them;
- do the work directly; do not merely describe how to do it;
- finish by running the task verification command and leave it passing;
- your prose is not acceptance evidence: the acceptance commands decide completion;
- do not commit or push; do not invent files, tests, or APIs outside this task's scope.
""".strip()


def _with_transport(
    client: OpenAICompatibleClient,
    transport: Transport | None,
) -> OpenAICompatibleClient:
    """
    Optional transport override for this dispatch.

    The transport normally lives on the client (tests build the
    client with a fake transport). When an explicit ``transport`` is
    given and differs from the client's, rebind it through the public
    constructor so the default urllib transport can never be reached
    by accident. Duck-typed stand-in clients without a transport are
    passed through untouched.
    """
    if transport is None:
        return client

    if not isinstance(client, OpenAICompatibleClient):
        return client

    if client._transport is transport:
        return client

    # providers.py is fixed for this milestone and exposes no public
    # endpoint accessor; the endpoint itself is an immutable value.
    return OpenAICompatibleClient(
        client._endpoint,
        client.models,
        transport=transport,
    )


async def dispatch_stage(
    client: OpenAICompatibleClient,
    *,
    role: ModelRole,
    prompt: str,
    transport: Transport | None = None,
) -> ChatResult:
    """
    Send ONE bounded-stage prompt as the DISPATCH role.

    ``role`` must be ``ModelRole.DISPATCH``: stage implementation
    runs on the dispatch role only, never on the supervisor role
    (plan/gate), so a wrong role is a programming error and raises
    ``ValueError`` before any request is built.

    ``transport`` is an optional explicit override (see
    ``_with_transport``); in the usual path the injected client
    carries endpoint and transport already.
    """
    if role is not ModelRole.DISPATCH:
        raise ValueError(
            "dispatch_stage dispatches only ModelRole.DISPATCH, "
            f"got {role!r}"
        )

    client = _with_transport(client, transport)

    return client.complete(
        ModelRole.DISPATCH,
        [
            ChatMessage("system", DISPATCH_SYSTEM),
            ChatMessage("user", prompt),
        ],
    )