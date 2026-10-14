"""Synthetic Argus contract server, never imports or executes real Argus."""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp.server import MCPServer

state = Path(sys.argv[1])
mcp = MCPServer("fake-argus")
Path(str(state) + ".pid").write_text(str(os.getpid()))


def config():
    return json.loads(state.read_text())


@mcp.tool()
async def argus_project_list(workdir: str = "") -> dict:
    cfg = config()
    if cfg.get("mode") == "timeout-read":
        await asyncio.sleep(10)
    return {"ok": True, "projects": cfg.get("projects", [])}


@mcp.tool()
async def argus_status(project_id: str) -> dict:
    cfg = config()
    if cfg.get("mode") == "not-found":
        return {
            "ok": False,
            "error_code": "project_not_found",
            "error": "private details",
        }
    return {
        "ok": True,
        "status": {
            "daemon": {"alive": False},
            "active_role": "manager",
            "journal": ["private never export"],
        },
    }


@mcp.tool()
async def argus_artifacts(project_id: str) -> dict:
    return {
        "ok": True,
        "artifacts": [
            {
                "path": "plan.json",
                "kind": "json",
                "storage_path": "/private/never-export",
                "preview": "private never export",
            }
        ],
    }


if config().get("mode") != "missing-tool":

    @mcp.tool()
    async def argus_message(project_id: str, text: str) -> dict:
        cfg = config()
        with Path(str(state) + ".calls").open("a") as handle:  # noqa: ASYNC230 -- tiny synthetic test journal
            handle.write(json.dumps({"project_id": project_id, "text": text}) + "\n")
        if cfg.get("mode") == "timeout-message":
            await asyncio.sleep(10)
        if cfg.get("mode") == "crash-message":
            os._exit(9)
        if cfg.get("mode") == "task":
            return {"ok": True, "kind": "task", "item": {"id": "must-not-import"}}
        if cfg.get("mode") == "bad-payload":
            return {"ok": True, "kind": "chat", "reply": 42}
        return {"ok": True, "kind": "chat", "reply": cfg.get("reply", "prose")}


if config().get("mode") == "malformed-stdio":
    sys.stdout.write("this is not JSON\n")
    sys.stdout.flush()
    sys.exit(1)

if __name__ == "__main__":
    mcp.run()
