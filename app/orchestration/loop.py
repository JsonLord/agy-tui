from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any

from app.agents.planner import create_plan
from app.agents.worker import execute_plan
from app.context.repository import build_snapshot
from app.schemas import WorkPlan
from app.validation.runner import validate


ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / "state"
RUNS_DIR = STATE_DIR / "runs"

STATE_DIR.mkdir(parents=True, exist_ok=True)
RUNS_DIR.mkdir(parents=True, exist_ok=True)


def _save_plan(plan: WorkPlan) -> None:
    (STATE_DIR / "last_plan.json").write_text(
        json.dumps(plan.model_dump(), indent=2) + "\n"
    )


def _save_validation(text: str) -> None:
    (STATE_DIR / "last_validation.txt").write_text(text)


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n")


def _write_text(path: Path, text: str) -> None:
    path.write_text(text or "")


def _failure_fingerprint(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _run_dir(run_id: str) -> Path:
    path = RUNS_DIR / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _save_final(run_dir: Path, result: dict[str, Any]) -> None:
    _write_json(run_dir / "final.json", result)


async def run_task(
    settings,
    repo: Path,
    task: str,
    test_command: str | None,
) -> bool:
    """
    Legacy local CLI flow.

    Preserved so existing:
        python -m app.main ...
    behavior keeps working.
    """

    snapshot = build_snapshot(repo, task)

    plan = await create_plan(
        settings=settings,
        task=task,
        snapshot=snapshot,
    )
    _save_plan(plan)

    await execute_plan(
        settings=settings,
        repo=repo,
        task=task,
        plan=plan,
    )

    validation = validate(repo, test_command)
    _save_validation(validation.output)

    if validation.ok:
        return True

    # Existing fast Gemma repair attempt.
    await execute_plan(
        settings=settings,
        repo=repo,
        task=task,
        plan=plan,
        repair_context=validation.output,
    )

    validation = validate(repo, test_command)
    _save_validation(validation.output)

    if validation.ok:
        return True

    # Existing Nemotron replan fallback.
    snapshot = build_snapshot(repo, task)

    revised_plan = await create_plan(
        settings=settings,
        task=task,
        snapshot=snapshot,
        previous_plan=plan,
        failure=validation.output,
    )
    _save_plan(revised_plan)

    await execute_plan(
        settings=settings,
        repo=repo,
        task=task,
        plan=revised_plan,
        repair_context=validation.output,
    )

    validation = validate(repo, test_command)
    _save_validation(validation.output)

    return validation.ok


async def run_cloud_task(
    settings,
    repo: Path,
    task: str,
    cloud_plan: WorkPlan,
    test_command: str | None,
    max_supervisor_cycles: int = 2,
) -> dict[str, Any]:
    """
    Cloud-led execution path.

    Cloud model provides the first plan.
    Gemma executes.
    Nemotron only enters after validation failure.
    Persistent local failures are returned to the cloud model.
    """

    run_id = _new_run_id()
    run_dir = _run_dir(run_id)

    _write_json(
        run_dir / "task.json",
        {
            "run_id": run_id,
            "repo": str(repo),
            "task": task,
            "test_command": test_command,
            "max_supervisor_cycles": max_supervisor_cycles,
        },
    )

    _write_json(
        run_dir / "cloud-plan.json",
        cloud_plan.model_dump(),
    )

    _save_plan(cloud_plan)

    worker_output = await execute_plan(
        settings=settings,
        repo=repo,
        task=task,
        plan=cloud_plan,
    )

    _write_text(
        run_dir / "gemma-00.txt",
        worker_output,
    )

    validation = validate(repo, test_command)

    _save_validation(validation.output)
    _write_text(
        run_dir / "validation-00.txt",
        validation.output,
    )

    if validation.ok:
        result = {
            "run_id": run_id,
            "status": "SUCCESS",
            "attempts": 1,
            "supervisor_cycles": 0,
            "validation": validation.output,
        }

        _save_final(run_dir, result)
        return result

    current_plan = cloud_plan
    fingerprints = [
        _failure_fingerprint(validation.output)
    ]

    attempts = 1
    escalation_reason = "supervisor_cycles_exhausted"

    for cycle in range(1, max_supervisor_cycles + 1):
        snapshot = build_snapshot(repo, task)

        supervisor_plan = await create_plan(
            settings=settings,
            task=task,
            snapshot=snapshot,
            previous_plan=current_plan,
            failure=validation.output,
        )

        _write_json(
            run_dir / f"nemotron-{cycle:02d}.json",
            supervisor_plan.model_dump(),
        )

        _save_plan(supervisor_plan)
        current_plan = supervisor_plan

        worker_output = await execute_plan(
            settings=settings,
            repo=repo,
            task=task,
            plan=current_plan,
            repair_context=(
                f"Nemotron supervisor cycle {cycle}.\n\n"
                f"Previous validation failure:\n"
                f"{validation.output}"
            ),
        )

        attempts += 1

        _write_text(
            run_dir / f"gemma-{cycle:02d}.txt",
            worker_output,
        )

        validation = validate(repo, test_command)

        _save_validation(validation.output)

        _write_text(
            run_dir / f"validation-{cycle:02d}.txt",
            validation.output,
        )

        if validation.ok:
            result = {
                "run_id": run_id,
                "status": "SUCCESS",
                "attempts": attempts,
                "supervisor_cycles": cycle,
                "validation": validation.output,
            }

            _save_final(run_dir, result)
            return result

        fingerprint = _failure_fingerprint(
            validation.output
        )

        if fingerprint in fingerprints:
            escalation_reason = (
                "persistent_same_failure_after_supervision"
            )
            break

        fingerprints.append(fingerprint)

    context = {
        "run_id": run_id,
        "repo": str(repo),
        "task": task,
        "test_command": test_command,
        "current_plan": current_plan.model_dump(),
        "failure": validation.output,
        "failure_fingerprint": _failure_fingerprint(
            validation.output
        ),
        "cloud_escalations": 0,
    }

    _write_json(
        run_dir / "context.json",
        context,
    )

    escalation = {
        "run_id": run_id,
        "status": "NEEDS_CLOUD_INPUT",
        "reason": escalation_reason,
        "attempts": attempts,
        "failure": validation.output,
        "question_for_cloud": (
            "The local Gemma/Nemotron loop could not resolve "
            "this failure. Diagnose the underlying cause and "
            "provide concrete technical advice: what assumption "
            "is probably wrong, what files or symbols should be "
            "re-inspected, and what implementation change should "
            "be attempted next."
        ),
    }

    _write_json(
        run_dir / "escalation-01.json",
        escalation,
    )

    return escalation


async def resume_cloud_task(
    settings,
    run_id: str,
    cloud_advice: str,
) -> dict[str, Any]:
    """
    Resume a run after the outer AGY cloud model has supplied advice.

    Cloud advice -> Gemma retry -> validation -> one Nemotron
    supervision cycle -> optional second cloud escalation.
    """

    run_dir = RUNS_DIR / run_id
    context_path = run_dir / "context.json"

    if not context_path.exists():
        return {
            "run_id": run_id,
            "status": "ERROR",
            "error": "Unknown run_id or no resumable context.",
        }

    context = json.loads(context_path.read_text())

    repo = Path(context["repo"])
    task = context["task"]
    test_command = context.get("test_command")
    current_plan = WorkPlan.model_validate(
        context["current_plan"]
    )

    escalation_number = (
        int(context.get("cloud_escalations", 0)) + 1
    )

    _write_text(
        run_dir
        / f"cloud-advice-{escalation_number:02d}.txt",
        cloud_advice,
    )

    worker_output = await execute_plan(
        settings=settings,
        repo=repo,
        task=task,
        plan=current_plan,
        repair_context=(
            "Cloud supervisor advice:\n"
            f"{cloud_advice}\n\n"
            "Previous validation failure:\n"
            f"{context['failure']}"
        ),
    )

    _write_text(
        run_dir
        / f"gemma-cloud-{escalation_number:02d}.txt",
        worker_output,
    )

    validation = validate(repo, test_command)

    _save_validation(validation.output)

    _write_text(
        run_dir
        / f"validation-cloud-{escalation_number:02d}.txt",
        validation.output,
    )

    if validation.ok:
        result = {
            "run_id": run_id,
            "status": "SUCCESS",
            "cloud_escalations": escalation_number,
            "validation": validation.output,
        }

        _save_final(run_dir, result)
        return result

    # Give Nemotron one fresh chance after cloud advice.
    snapshot = build_snapshot(repo, task)

    supervisor_plan = await create_plan(
        settings=settings,
        task=task,
        snapshot=snapshot,
        previous_plan=current_plan,
        failure=(
            "Cloud supervisor advice:\n"
            f"{cloud_advice}\n\n"
            "Validation after cloud-guided Gemma retry:\n"
            f"{validation.output}"
        ),
    )

    _write_json(
        run_dir
        / f"nemotron-cloud-{escalation_number:02d}.json",
        supervisor_plan.model_dump(),
    )

    _save_plan(supervisor_plan)

    worker_output = await execute_plan(
        settings=settings,
        repo=repo,
        task=task,
        plan=supervisor_plan,
        repair_context=validation.output,
    )

    _write_text(
        run_dir
        / f"gemma-cloud-supervised-{escalation_number:02d}.txt",
        worker_output,
    )

    validation = validate(repo, test_command)

    _save_validation(validation.output)

    _write_text(
        run_dir
        / f"validation-cloud-supervised-{escalation_number:02d}.txt",
        validation.output,
    )

    if validation.ok:
        result = {
            "run_id": run_id,
            "status": "SUCCESS",
            "cloud_escalations": escalation_number,
            "validation": validation.output,
        }

        _save_final(run_dir, result)
        return result

    context.update(
        {
            "current_plan": supervisor_plan.model_dump(),
            "failure": validation.output,
            "failure_fingerprint": _failure_fingerprint(
                validation.output
            ),
            "cloud_escalations": escalation_number,
        }
    )

    _write_json(
        context_path,
        context,
    )

    if escalation_number >= 2:
        result = {
            "run_id": run_id,
            "status": "FAILED_AFTER_CLOUD_ESCALATIONS",
            "cloud_escalations": escalation_number,
            "failure": validation.output,
        }

        _save_final(run_dir, result)
        return result

    escalation = {
        "run_id": run_id,
        "status": "NEEDS_CLOUD_INPUT",
        "reason": "failure_persisted_after_cloud_advice",
        "cloud_escalations": escalation_number,
        "failure": validation.output,
        "question_for_cloud": (
            "The previous cloud advice was applied, then "
            "Nemotron supervised another retry, but validation "
            "still fails. Reassess the diagnosis using the new "
            "failure and provide a substantially different "
            "technical strategy."
        ),
    }

    _write_json(
        run_dir
        / f"escalation-{escalation_number + 1:02d}.json",
        escalation,
    )

    return escalation
