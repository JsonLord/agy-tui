"""
Needle3 tool routing — optional seam.

AGY asks Needle (`POST /v1/route`) which of a small candidate
tool set fits the current subtask, then runs tools itself.
Needle never executes tools and never repairs tool calls; when
routing is unavailable or not configured, callers fall back to
running without routing.

Candidate tool sets are kept to ``MAX_CANDIDATE_TOOLS`` (<= 5),
matching the router's validation contract (tools follow Needle's
shape: ``{"name", "description", "parameters": <JSON Schema>}``;
the client passes them through verbatim). Invalid tool sets are
rejected as ``ToolSetRejected`` before any HTTP.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from app.providers import Transport, ProviderError, urllib_transport

MAX_CANDIDATE_TOOLS = 5


class RouteOutcome(str, Enum):
    """Needle /v1/route outcome values."""

    SELECTED = "selected"
    CONFIRM = "confirm"
    ABSTAIN = "abstain"


@dataclass(frozen=True)
class FunctionCall:
    """One routed tool call (raw shape is Needle-defined)."""

    name: str
    arguments: Mapping[str, Any]
    raw: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True)
class RouteDecision:
    outcome: RouteOutcome
    function_calls: tuple[FunctionCall, ...]
    suppressed_calls: tuple[Mapping[str, Any], ...]
    confidence: float | None
    reasoning: str | None
    validation: Mapping[str, Any] | None
    model: str

    @property
    def selected(self) -> bool:
        return self.outcome is RouteOutcome.SELECTED

    @property
    def abstained(self) -> bool:
        return self.outcome is RouteOutcome.ABSTAIN


class RoutingValidationError(ValueError):
    """The routing request failed contract validation (client-side
    checks or server-side 422)."""

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.details = details


class ToolSetRejected(RoutingValidationError):
    """The candidate tool set is invalid (must hold 1..<=5 tools)."""


class NeedleUnavailable(RuntimeError):
    """Transport or server failure; routing could not be decided."""


class ToolRouter(Protocol):
    def route(
        self,
        input: str,
        tools: Sequence[Mapping[str, Any]],
        *,
        system: str | None = None,
    ) -> RouteDecision: ...


class NeedleToolRouter:
    """Needle3-backed ToolRouter (``POST /v1/route``)."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout: float = 30.0,
        transport: Transport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport or urllib_transport

    def route(
        self,
        input: str,
        tools: Sequence[Mapping[str, Any]],
        *,
        system: str | None = None,
    ) -> RouteDecision:
        # Contract enforcement before any HTTP: 1..5 candidate tools.
        if not tools:
            raise ToolSetRejected(
                "candidate tool set must not be empty"
            )

        if len(tools) > MAX_CANDIDATE_TOOLS:
            raise ToolSetRejected(
                "candidate tool set exceeds "
                f"{MAX_CANDIDATE_TOOLS} tools: {len(tools)}"
            )

        if not isinstance(input, str) or not input.strip():
            raise RoutingValidationError(
                "routing input must be nonempty text"
            )

        payload: dict[str, Any] = {
            "input": input,
            "tools": [dict(t) for t in tools],
        }

        if system is not None:
            payload["system"] = system

        headers = {
            "Content-type": "application/json",
            "Accept": "application/json",
        }

        if self._api_key:
            headers["Authorization"] = "Bearer " + self._api_key

        request = urllib.request.Request(
            self._base_url + "/v1/route",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            status, body = self._transport(request, self._timeout)
        except Exception as exc:
            raise NeedleUnavailable(
                f"needle request failed: {exc}"
            ) from exc

        text = body.decode("utf-8", errors="replace")

        if status == 422:
            # Server-side validation error (invalid request or
            # invalid tool set).
            try:
                details = json.loads(text).get("detail")
            except ValueError:
                details = text
            raise ToolSetRejected(
                f"needle rejected the route request: {text[:500]}",
                details=details,
            )

        if status != 200:
            raise NeedleUnavailable(
                f"needle returned HTTP {status}: {text[:500]}"
            )

        return _parse_response(text)


def _parse_response(text: str) -> RouteDecision:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise NeedleUnavailable(
            f"needle returned invalid JSON: {text[:500]}"
        ) from exc

    try:
        outcome = RouteOutcome(data["outcome"])
    except (KeyError, ValueError) as exc:
        raise NeedleUnavailable(
            f"needle returned an unknown outcome: {text[:500]}"
        ) from exc

    calls = tuple(
        _parse_call(c) for c in data.get("function_calls") or ()
        if isinstance(c, Mapping)
    )
    suppressed = tuple(
        dict(c) for c in data.get("suppressed_calls") or ()
        if isinstance(c, Mapping)
    )
    confidence = data.get("confidence")
    validation = data.get("validation")

    return RouteDecision(
        outcome=outcome,
        function_calls=calls,
        suppressed_calls=suppressed,
        confidence=(
            confidence if isinstance(confidence, (int, float)) else None
        ),
        reasoning=(
            data.get("reasoning")
            if isinstance(data.get("reasoning"), str)
            else None
        ),
        validation=validation if isinstance(validation, Mapping) else None,
        model=data.get("model") if isinstance(data.get("model"), str) else "",
    )


def _parse_call(raw: Mapping[str, Any]) -> FunctionCall:
    name = raw.get("name")
    arguments = raw.get("arguments")

    return FunctionCall(
        name=name if isinstance(name, str) else "",
        arguments=arguments if isinstance(arguments, Mapping) else {},
        raw=dict(raw),
    )


def route_or_none(
    router: ToolRouter | None,
    input: str,
    tools: Sequence[Mapping[str, Any]],
    *,
    system: str | None = None,
) -> RouteDecision | None:
    """
    Clean optional seam: with no configured router, returns None
    and the caller proceeds without routing. With a router, Needle
    decides.
    """
    if router is None:
        return None

    return router.route(input, tools, system=system)