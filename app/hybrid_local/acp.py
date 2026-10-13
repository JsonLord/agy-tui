from __future__ import annotations

import asyncio
import contextlib
import json

from .policy import HybridError, native_environment, permission_response
from .windows import OwnedWindowsJob, native_binary, process_options


def effective_model(response: dict) -> str:
    for option in response.get("configOptions", []):
        if option.get("category") == "model" or option.get("id") == "model":
            value = option.get("currentValue")
            if isinstance(value, str) and "/" in value:
                return value
    value = response.get("models", {}).get("currentModelId")
    if isinstance(value, str) and "/" in value:
        return value
    raise HybridError("MODEL_UNOBSERVABLE")


class OpenCodeACP:
    def __init__(self, binary, workspace, role, *, environment=None, audit=None):
        self.binary, self.workspace, self.role = binary, workspace, role
        self.environment = native_environment(workspace, role, environment)
        self.audit = audit or (lambda *_: None)
        self.process = self.reader = self.stderr = self.job = None
        self.pending, self.sequence = {}, 0
        self.session_id = self.model = self.text = ""
        self.prompting = False
        self.failure = None

    async def __aenter__(self):
        self.workspace.validate()
        try:
            self.process = await asyncio.create_subprocess_exec(
                native_binary(self.binary),
                "acp",
                "--pure",
                "--hostname",
                "127.0.0.1",
                "--port",
                "0",
                "--cwd",
                str(self.workspace.root),
                cwd=self.workspace.root,
                env=self.environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=2 * 1024 * 1024,
                **process_options(),
            )
            self.job = OwnedWindowsJob(self.process.pid)
            self.reader = asyncio.create_task(self._read())
            self.stderr = asyncio.create_task(self._drain())
            response = await self.request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                    },
                    "clientInfo": {"name": "hybrid-local", "version": "0.1.2"},
                },
            )
            self.capabilities = response.get("agentCapabilities", {})
            return self
        except (OSError, ValueError):
            await self.close()
            raise HybridError("OPENCODE_UNAVAILABLE") from None
        except BaseException:
            await self.close()
            raise

    async def __aexit__(self, *_):
        await self.close()

    async def _drain(self):
        while await self.process.stderr.read(8192):
            pass  # Provider errors can contain credentials; never persist stderr.

    async def _send(self, message):
        try:
            self.process.stdin.write((json.dumps(message) + "\n").encode())
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionError):
            raise HybridError("ACP_DISCONNECTED") from None

    async def request(self, method, params, timeout=30):
        if self.failure:
            raise self.failure
        self.sequence += 1
        key = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            await self._send(
                {"jsonrpc": "2.0", "id": key, "method": method, "params": params}
            )
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise HybridError("ACP_TIMEOUT") from None
        finally:
            self.pending.pop(key, None)

    async def _read(self):
        try:
            while raw := await self.process.stdout.readline():
                message = json.loads(raw)
                if "method" in message:
                    if "id" in message:
                        result, allowed = ({"outcome": {"outcome": "cancelled"}}, False)
                        if message["method"] == "session/request_permission":
                            result, allowed = permission_response(
                                self.workspace,
                                self.role,
                                message.get("params", {}),
                                self.session_id,
                            )
                        self.audit(
                            "permission", {"allowed": allowed, "role": self.role}
                        )
                        await self._send(
                            {"jsonrpc": "2.0", "id": message["id"], "result": result}
                        )
                    elif message["method"] == "session/update":
                        params = message.get("params", {})
                        if params.get("sessionId") != self.session_id:
                            continue
                        update = params.get("update", {})
                        if update.get("sessionUpdate") == "agent_message_chunk":
                            content = update.get("content", {})
                            if content.get("type") == "text":
                                self.text += content.get("text", "")
                                if len(self.text) > 2 * 1024 * 1024:
                                    raise HybridError("OUTPUT_LIMIT")
                        if "configOptions" in update:
                            observed = effective_model(update)
                            if self.model and observed != self.model:
                                raise HybridError("MODEL_CHANGED")
                elif message.get("id") in self.pending:
                    future = self.pending[message["id"]]
                    if not future.done():
                        if "error" in message:
                            future.set_exception(HybridError("ACP_REMOTE_ERROR"))
                        else:
                            future.set_result(message.get("result", {}))
            raise HybridError("ACP_DISCONNECTED")
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 -- controlled ACP transport failure, redact remote detail
            self.failure = (
                error
                if isinstance(error, HybridError)
                else HybridError("ACP_PROTOCOL_ERROR")
            )
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(self.failure)

    async def session(self, session_id="", expected_model=""):
        method = "session/load" if session_id else "session/new"
        params = {"cwd": str(self.workspace.root), "mcpServers": []}
        if session_id:
            if not self.capabilities.get("loadSession"):
                raise HybridError("SESSION_RESUME_UNSUPPORTED")
            params["sessionId"] = session_id
        response = await self.request(method, params)
        self.session_id = session_id or response.get("sessionId", "")
        self.model = effective_model(response)
        if not self.session_id or (expected_model and self.model != expected_model):
            raise HybridError("SESSION_MODEL_MISMATCH")
        role_id = f"hybrid-local-{self.role}"
        options = response.get("configOptions", [])
        mode = next(
            (
                o
                for o in options
                if o.get("category") == "mode" or o.get("id") == "mode"
            ),
            None,
        )
        if mode:
            changed = await self.request(
                "session/set_config_option",
                {
                    "sessionId": self.session_id,
                    "configId": mode["id"],
                    "value": role_id,
                },
            )
            if effective_model(changed) != self.model:
                raise HybridError("MODEL_CHANGED")
        elif role_id in {
            m["id"] for m in response.get("modes", {}).get("availableModes", [])
        }:
            await self.request(
                "session/set_mode", {"sessionId": self.session_id, "modeId": role_id}
            )
        else:
            raise HybridError("ROLE_MODE_UNAVAILABLE")
        return {
            "session_id": self.session_id,
            "model": self.model,
            "root": str(self.workspace.root),
            "role": self.role,
            "model_origin": "Native OpenCode resolution; precedence unobservable",
            "model_evidence": "ACP selection observed; provider invocation unverified",
        }

    async def prompt(self, text, timeout):
        self.prompting = True
        self.text = ""
        try:
            response = await self.request(
                "session/prompt",
                {
                    "sessionId": self.session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
                timeout=timeout,
            )
            self.prompting = False
            if response.get("stopReason") != "end_turn":
                raise HybridError("TURN_NOT_COMPLETED")
            return {
                "text": self.text,
                "model": self.model,
                "session_id": self.session_id,
                "stop_reason": "end_turn",
            }
        except BaseException:
            with contextlib.suppress(Exception):
                await self._send(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/cancel",
                        "params": {"sessionId": self.session_id},
                    }
                )
            raise

    async def close(self):
        if self.job:
            self.job.close()
        if self.process and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        for task in (self.reader, self.stderr):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
