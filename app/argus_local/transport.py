"""Owned, bounded stdio MCP transport with confirmed child cleanup."""

from __future__ import annotations

import asyncio
import os
import signal
from contextlib import asynccontextmanager

import anyio
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage
from pydantic import TypeAdapter

from app.factory_local.store import FactoryError
from app.hybrid_local.windows import OwnedWindowsJob, process_options

MAX_LINE = 1_000_000
RPC = TypeAdapter(JSONRPCMessage)


@asynccontextmanager
async def owned_stdio(command, args, env, timeout=5):
    process = None
    job = None
    reader_send, reader_receive = anyio.create_memory_object_stream(0)
    writer_send, writer_receive = anyio.create_memory_object_stream(0)
    tasks = []
    try:
        try:
            async with asyncio.timeout(timeout):
                process = await asyncio.create_subprocess_exec(
                    command,
                    *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=env,
                    limit=MAX_LINE + 1,
                    **process_options(),
                )
            if os.name == "nt":
                job = OwnedWindowsJob(process.pid)
        except (OSError, TimeoutError):
            raise FactoryError("ARGUS_START_FAILED") from None

        async def read():
            try:
                async with reader_send:
                    while line := await process.stdout.readline():
                        if len(line) > MAX_LINE:
                            raise ValueError()
                        await reader_send.send(SessionMessage(RPC.validate_json(line)))
            except (ValueError, OSError):
                await reader_send.aclose()

        async def write():
            async with writer_receive:
                async for message in writer_receive:
                    data = (
                        message.message.model_dump_json(
                            by_alias=True, exclude_unset=True
                        ).encode()
                        + b"\n"
                    )
                    process.stdin.write(data)
                    await process.stdin.drain()

        tasks = [asyncio.create_task(read()), asyncio.create_task(write())]
        yield reader_receive, writer_send
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        confirmed = True
        if process:
            if process.stdin:
                process.stdin.close()
            try:
                async with asyncio.timeout(timeout):
                    await process.wait()
            except TimeoutError:
                pass
            try:
                if job:
                    job.close()
                elif os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                elif process.returncode is None:
                    process.kill()
                    confirmed = False  # Without an assigned Windows job, descendants are unconfirmed.
                async with asyncio.timeout(timeout):
                    await process.wait()
                    await process.communicate()
            except (OSError, TimeoutError):
                confirmed = False
        await reader_receive.aclose()
        await writer_send.aclose()
        if not confirmed:
            raise FactoryError("ARGUS_PROCESS_STATE_UNCERTAIN")
