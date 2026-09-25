#!/usr/bin/env python3

import json
import os
import urllib.request
from pathlib import Path
import time
import subprocess
import re
import hashlib

from mcp.server import MCPServer


ROOT = Path("/home/leon/antigravity-hybrid")
STATE = ROOT / "state"
RUNS = STATE / "runs"


def load_env(path: Path) -> None:
    if not path.exists():
        return

    for raw in path.read_text().splitlines():
        line = raw.strip()

        if not line or line.startswith("#"):
            continue

        if "=" not in line:
            continue

        key, value = line.split("=", 1)

        os.environ.setdefault(
            key.strip(),
            value.strip().strip('"').strip("'"),
        )


load_env(ROOT / ".env")


from app.config import Settings
from app.orchestration.opencode_cloud import (
    resume_opencode_cloud_task,
    run_opencode_cloud_task,
)
from app.schemas import WorkPlan


SETTINGS = Settings.from_env()

mcp = MCPServer("antigravity-hybrid")


def local_chat(
    base_url: str,
    model: str,
    prompt: str,
    api_key: str | None = None,
) -> str:
    url = (
        base_url.rstrip("/")
        + "/chat/completions"
    )

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
        "temperature": 0.2,
    }

    headers = {
        "Content-Type": "application/json",
    }

    if api_key:
        headers["Authorization"] = (
            f"Bearer {api_key}"
        )

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )

    with urllib.request.urlopen(
        request,
        timeout=600,
    ) as response:
        data = json.loads(response.read())

    return data["choices"][0]["message"]["content"]



GRAPHIFY_BIN = "/home/leon/.local/bin/graphify"
BRIEFS_ROOT = Path(__file__).resolve().parent / "state" / "briefs"


def _compact_graphify_result(text: str) -> dict:
    nodes = []
    edges = []
    relevant_files = []
    summary = None

    for raw in text.splitlines():
        line = raw.strip()

        if line.startswith("Graph:"):
            summary = line

        elif line.startswith("NODE "):
            item = line[5:]
            nodes.append(item)

            match = re.search(r"\[src=([^\s\]]+)", item)
            if match:
                path = match.group(1)
                if path not in relevant_files:
                    relevant_files.append(path)

        elif line.startswith("EDGE "):
            edges.append(line[5:])

    return {
        "graph_summary": summary,
        "relevant_files": relevant_files[:12],
        "relevant_nodes": nodes[:16],
        "relationships": edges[:16],
        "node_matches": len(nodes),
        "relationship_matches": len(edges),
    }


def _run_graphify(repo: Path, task: str) -> tuple[str, str]:
    if not Path(GRAPHIFY_BIN).exists():
        raise RuntimeError(
            f"Graphify executable not found at {GRAPHIFY_BIN}"
        )

    graph_path = repo / "graphify-out" / "graph.json"

    # Rebuild for now so the graph is always fresh.
    # We can replace this with git-fingerprint caching next.
    extract = subprocess.run(
        [GRAPHIFY_BIN, "."],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=180,
    )

    extract_log = (
        (extract.stdout or "")
        + ("\n" if extract.stdout and extract.stderr else "")
        + (extract.stderr or "")
    )

    if extract.returncode != 0 or not graph_path.exists():
        raise RuntimeError(
            "Graphify extraction failed:\n" + extract_log[-4000:]
        )

    query = subprocess.run(
        [GRAPHIFY_BIN, "query", task],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
    )

    query_log = (
        (query.stdout or "")
        + ("\n" if query.stdout and query.stderr else "")
        + (query.stderr or "")
    )

    if query.returncode != 0:
        raise RuntimeError(
            "Graphify query failed:\n" + query_log[-4000:]
        )

    return extract_log, query_log


@mcp.tool()
def hybrid_prepare(repo: str, task: str) -> dict:
    """Prepare a compact local repository brief before cloud planning.

    Uses Graphify locally for structural discovery. Full evidence stays
    on the Debian host; only a compact structural brief is returned.
    """
    repo_path = Path(repo).expanduser().resolve()

    if not repo_path.is_dir():
        raise ValueError(f"Repository does not exist: {repo_path}")

    if not task.strip():
        raise ValueError("task must not be empty")

    extract_log, graph_output = _run_graphify(repo_path, task)

    compact = _compact_graphify_result(graph_output)

    brief_id = hashlib.sha256(
        f"{repo_path}\0{task}\0{time.time_ns()}".encode()
    ).hexdigest()[:12]

    run_dir = BRIEFS_ROOT / brief_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Keep full local evidence out of the cloud context.
    (run_dir / "graphify-extract.log").write_text(extract_log)
    (run_dir / "graphify-query.txt").write_text(graph_output)

    uncertainties = []
    if not compact["relevant_nodes"]:
        uncertainties.append(
            "Graphify found no directly relevant structural nodes."
        )

    brief = {
        "brief_id": brief_id,
        "repo": str(repo_path),
        "task": task,
        "source": "graphify-local",
        "relevant_files": compact["relevant_files"],
        "relevant_nodes": compact["relevant_nodes"],
        "relationships": compact["relationships"],
        "graph_summary": compact["graph_summary"],
        "uncertainties": uncertainties,
        "local_evidence_available": True,
        "planning_instruction": (
            "Plan from this structural brief. Do not read repository "
            "files from the cloud unless unresolved evidence is genuinely "
            "required."
        ),
    }

    import json
    (run_dir / "brief-cloud.json").write_text(
        json.dumps(brief, indent=2) + "\n"
    )

    return brief


@mcp.tool()
async def hybrid_start(
    repo: str,
    task: str,
    cloud_plan_json: str,
    test_command: str = "",
) -> str:
    """
    Start a cloud-led coding run.

    The outer cloud model must provide the initial WorkPlan as
    cloud_plan_json.

    Required JSON fields:
      goal
      files
      steps
      constraints
      verification
      risk
      worker_instruction

    The cloud model supplies the initial strategy.

    Local execution is delegated to the isolated OpenCode stack:
    Graphify/Serena -> Gemma -> bounded Nemotron recovery.

    Deterministic validation is authoritative.
    Persistent validated failures return NEEDS_CLOUD_INPUT.
    """

    repo_path = Path(repo).expanduser().resolve()

    if not repo_path.exists():
        return json.dumps(
            {
                "status": "ERROR",
                "error": (
                    f"Repository does not exist: "
                    f"{repo_path}"
                ),
            },
            indent=2,
        )

    try:
        raw_plan = json.loads(cloud_plan_json)
        cloud_plan = WorkPlan.model_validate(raw_plan)

    except Exception as exc:
        return json.dumps(
            {
                "status": "ERROR",
                "error": (
                    "Invalid cloud_plan_json: "
                    f"{exc}"
                ),
            },
            indent=2,
        )

    result = await run_opencode_cloud_task(
        repo=repo_path,
        task=task,
        cloud_plan=cloud_plan,
        test_command=(
            test_command or None
        ),
    )

    return json.dumps(
        result,
        indent=2,
    )


@mcp.tool()
async def hybrid_resume(
    run_id: str,
    cloud_advice: str,
) -> str:
    """
    Resume a run that returned NEEDS_CLOUD_INPUT.

    cloud_advice should contain the outer cloud model's technical
    diagnosis and concrete next implementation strategy.
    """

    result = await resume_opencode_cloud_task(
        run_id=run_id,
        cloud_advice=cloud_advice,
    )

    return json.dumps(
        result,
        indent=2,
    )


@mcp.tool()
def hybrid_status(
    run_id: str = "",
) -> str:
    """
    Inspect a specific hybrid run or the latest legacy state.
    """

    if run_id:
        run_dir = RUNS / run_id

        if not run_dir.exists():
            return json.dumps(
                {
                    "status": "ERROR",
                    "error": "Unknown run_id",
                },
                indent=2,
            )

        result = {
            "run_id": run_id,
        }

        final = run_dir / "final.json"
        context = run_dir / "context.json"

        if final.exists():
            result["final"] = json.loads(
                final.read_text()
            )

        if context.exists():
            result["context"] = json.loads(
                context.read_text()
            )

        result["artifacts"] = sorted(
            p.name
            for p in run_dir.iterdir()
            if p.is_file()
        )

        return json.dumps(
            result,
            indent=2,
        )

    result = {}

    plan = STATE / "last_plan.json"
    validation = STATE / "last_validation.txt"

    if plan.exists():
        result["last_plan"] = json.loads(
            plan.read_text()
        )

    if validation.exists():
        result["last_validation"] = (
            validation.read_text()
        )

    if not result:
        result["status"] = (
            "No hybrid run state exists yet."
        )

    return json.dumps(
        result,
        indent=2,
    )


@mcp.tool()
def gemma_chat(prompt: str) -> str:
    """
    Direct diagnostic access to local Gemma.
    """

    return local_chat(
        os.environ["WORKER_BASE_URL"],
        os.environ["WORKER_MODEL"],
        prompt,
        api_key="local",
    )


@mcp.tool()
def nemotron_chat(prompt: str) -> str:
    """
    Direct diagnostic access to local Nemotron.
    """

    return local_chat(
        os.environ["PLANNER_BASE_URL"],
        os.environ["PLANNER_MODEL"],
        prompt,
    )


if __name__ == "__main__":
    mcp.run()
