"""Typed external MCP calls only; /ask always precedes generative turns."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession

from app.factory_local.store import FactoryError

from .protocol import unique_object
from .transport import owned_stdio

TOOLS = {
    "argus_project_list": {"workdir": "string"},
    "argus_status": {"project_id": "string"},
    "argus_artifacts": {"project_id": "string"},
    "argus_message": {"project_id": "string", "text": "string"},
}


class ArgusMcpClient:
    def __init__(
        self,
        executable="argus-plugin-server",
        *,
        args=(),
        timeout=120,
        startup_timeout=10,
    ):
        if not 0 < timeout <= 600 or not 0 < startup_timeout <= 30:
            raise FactoryError("ARGUS_CONFIGURATION_INVALID")
        self.executable, self.args = executable, tuple(args)
        self.timeout, self.startup_timeout = timeout, startup_timeout

    def resolve(self):
        resolved = shutil.which(self.executable)
        path = Path(resolved or self.executable)
        if not path.is_file():
            raise FactoryError("ARGUS_NOT_INSTALLED")
        if os.name == "nt" and path.suffix.casefold() != ".exe":
            raise FactoryError("ARGUS_CONFIGURATION_INVALID")
        return str(path.absolute())

    @asynccontextmanager
    async def _session(self):
        command = self.resolve()
        try:
            async with (
                owned_stdio(command, self.args, dict(os.environ)) as streams,
                ClientSession(*streams) as session,
            ):
                async with asyncio.timeout(self.startup_timeout):
                    await session.initialize()
                    available = await session.list_tools()
                    tools = {tool.name: tool for tool in available.tools}
                    if len(tools) != len(available.tools):
                        raise FactoryError("ARGUS_PROTOCOL_ERROR")
                    for name, expected in TOOLS.items():
                        tool = tools.get(name)
                        if (
                            tool is None
                            or tool.model_dump(by_alias=True)["inputSchema"].get("type")
                            != "object"
                        ):
                            raise FactoryError("ARGUS_PROTOCOL_ERROR")
                        props = tool.model_dump(by_alias=True)["inputSchema"].get(
                            "properties", {}
                        )
                        if any(
                            props.get(key, {}).get("type") != kind
                            for key, kind in expected.items()
                        ):
                            raise FactoryError("ARGUS_PROTOCOL_ERROR")
                        if set(
                            tool.model_dump(by_alias=True)["inputSchema"].get(
                                "required", []
                            )
                        ) - set(expected):
                            raise FactoryError("ARGUS_PROTOCOL_ERROR")
                yield session
        except FactoryError:
            raise
        except TimeoutError:
            raise FactoryError("ARGUS_TIMEOUT") from None
        except Exception as error:  # noqa: BLE001 -- redact MCP/process exceptions
            pending = [error]
            while pending:
                item = pending.pop()
                if isinstance(item, FactoryError):
                    raise FactoryError(item.code) from None
                if isinstance(item, TimeoutError):
                    raise FactoryError("ARGUS_TIMEOUT") from None
                if isinstance(item, BaseExceptionGroup):
                    pending.extend(item.exceptions)
            raise FactoryError("ARGUS_PROTOCOL_ERROR") from None

    @staticmethod
    def _payload(result):
        wire = result.model_dump(by_alias=True)
        if wire.get("isError"):
            raise FactoryError("ARGUS_BAD_RESPONSE")
        # FastMCP returns JSON text (and may additionally return structuredContent).
        if len(result.content) != 1 or result.content[0].type != "text":
            raise FactoryError("ARGUS_BAD_RESPONSE")
        text = result.content[0].text
        if len(text.encode()) > 500_000:
            raise FactoryError("ARGUS_BAD_RESPONSE")
        try:
            value = json.loads(text, object_pairs_hook=unique_object)
        except (ValueError, RecursionError):
            raise FactoryError("ARGUS_BAD_RESPONSE") from None
        if not isinstance(value, dict) or value.get("ok") is not True:
            if (
                isinstance(value, dict)
                and value.get("error_code") == "project_not_found"
            ):
                raise FactoryError("ARGUS_PROJECT_NOT_FOUND")
            raise FactoryError("ARGUS_BAD_RESPONSE")
        if (
            wire.get("structuredContent") is not None
            and wire["structuredContent"] != value
        ):
            raise FactoryError("ARGUS_BAD_RESPONSE")
        return value

    async def _read(self, name, args):
        async with self._session() as session:
            async with asyncio.timeout(self.timeout):
                return self._payload(await session.call_tool(name, args))

    async def project_list(self):
        value = await self._read("argus_project_list", {"workdir": ""})
        rows = value.get("projects")
        if not isinstance(rows, list) or len(rows) > 2000:
            raise FactoryError("ARGUS_BAD_RESPONSE")
        return rows

    async def status(self, project_id):
        value = await self._read("argus_status", {"project_id": project_id})
        if not isinstance(value.get("status"), dict):
            raise FactoryError("ARGUS_BAD_RESPONSE")
        return value["status"]

    async def artifacts(self, project_id):
        value = await self._read("argus_artifacts", {"project_id": project_id})
        if (
            not isinstance(value.get("artifacts"), list)
            or len(value["artifacts"]) > 200
        ):
            raise FactoryError("ARGUS_BAD_RESPONSE")
        return value["artifacts"]

    async def message(self, project_id, request):
        if (
            not isinstance(request, str)
            or not request.strip()
            or len(request.encode()) > 250_000
        ):
            raise FactoryError("ARGUS_CONFIGURATION_INVALID")
        sent = False
        try:
            async with self._session() as session:
                async with asyncio.timeout(self.timeout):
                    sent = True
                    value = self._payload(
                        await session.call_tool(
                            "argus_message",
                            {"project_id": project_id, "text": "/ask " + request},
                        )
                    )
                    if (
                        value.get("kind") != "chat"
                        or not isinstance(value.get("reply"), str)
                        or len(value["reply"].encode()) > 250_000
                    ):
                        raise FactoryError("ARGUS_BAD_RESPONSE")
                    return value["reply"]
        except (FactoryError, asyncio.CancelledError) as error:
            if sent:
                raise FactoryError("ARGUS_OUTCOME_UNCERTAIN") from error
            raise
