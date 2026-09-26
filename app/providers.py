"""
Model/provider abstraction for the AGY execution path.

Model-neutral, provider-neutral: an OpenAI-compatible chat
endpoint plus named model roles. AGY binds the roles to the
Helmholtz models (supervisor = plan/gate, dispatch = bounded
stage implementation); no provider, framework, or runtime
specific assumptions live here.

HTTP transport is injectable so tests run deterministically
with mocked HTTP and no network.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable


class ModelRole(str, Enum):
    """
    AGY model roles along the execution path
    (supervisor -> plan/stage -> dispatch -> gate).
    """

    SUPERVISOR = "supervisor"
    DISPATCH = "dispatch"


@dataclass(frozen=True)
class RoleModels:
    """Exact model names bound to each role (kept verbatim)."""

    supervisor_model: str
    dispatch_model: str

    def model_for(self, role: ModelRole) -> str:
        if role is ModelRole.SUPERVISOR:
            return self.supervisor_model

        if role is ModelRole.DISPATCH:
            return self.dispatch_model

        raise ValueError(f"Unknown model role: {role!r}")

    @classmethod
    def for_agy(cls, settings: Any) -> "RoleModels":
        """Bind roles from AgySettings without depending on config types."""
        return cls(
            supervisor_model=settings.supervisor_model,
            dispatch_model=settings.dispatch_model,
        )


@dataclass(frozen=True)
class ProviderEndpoint:
    base_url: str
    api_key: str | None = None
    timeout: float = 120.0

    @classmethod
    def for_agy(cls, settings: Any) -> "ProviderEndpoint":
        return cls(
            base_url=settings.provider_base_url,
            api_key=settings.provider_api_key,
        )


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class ChatResult:
    model: str
    text: str
    raw: Mapping[str, Any]


class ProviderError(RuntimeError):
    """HTTP, protocol, or payload failure at the model provider."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


# (request, timeout_seconds) -> (http_status, response_body)
Transport = Callable[
    [urllib.request.Request, float],
    tuple[int, bytes],
]


def urllib_transport(
    request: urllib.request.Request,
    timeout: float,
) -> tuple[int, bytes]:
    """Default stdlib transport; returns status + body even on HTTP errors."""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()

    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class OpenAICompatibleClient:
    """
    Minimal /chat/completions client against any
    OpenAI-compatible endpoint.
    """

    def __init__(
        self,
        endpoint: ProviderEndpoint,
        models: RoleModels,
        transport: Transport | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._models = models
        self._transport = transport or urllib_transport

    @property
    def models(self) -> RoleModels:
        return self._models

    def complete(
        self,
        role: ModelRole,
        messages: Sequence[ChatMessage | Mapping[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        model = self._models.model_for(role)
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                m.as_dict() if isinstance(m, ChatMessage) else dict(m)
                for m in messages
            ],
        }

        if temperature is not None:
            payload["temperature"] = temperature

        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-type": "application/json",
            "Accept": "application/json",
        }

        if self._endpoint.api_key:
            headers["Authorization"] = (
                "Bearer " + self._endpoint.api_key
            )

        request = urllib.request.Request(
            self._endpoint.base_url.rstrip("/") + "/chat/completions",
            data=body,
            headers=headers,
            method="POST",
        )

        try:
            status, response_body = self._transport(
                request,
                float(self._endpoint.timeout),
            )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"provider request failed: {exc}"
            ) from exc

        text = response_body.decode("utf-8", errors="replace")

        if status != 200:
            raise ProviderError(
                f"provider returned HTTP {status}: {text[:500]}",
                status=status,
                body=text,
            )

        try:
            data = json.loads(text)
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                f"provider returned an invalid payload: {text[:500]}",
                status=status,
                body=text,
            ) from exc

        if not isinstance(content, str):
            raise ProviderError(
                "provider message content is not text",
                status=status,
                body=text,
            )

        result_model = data.get("model")
        return ChatResult(
            model=result_model if isinstance(result_model, str) else model,
            text=content,
            raw=data,
        )