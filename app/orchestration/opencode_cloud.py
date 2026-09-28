from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from app.integrations.external_stack import (
    effective_status,
    run_supervised,
    supervisor_status,
)
from app.schemas import WorkPlan
from app.validation.runner import validate


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNS_ROOT = PROJECT_ROOT / "state" / "runs"


def _build_execution_brief(
    task: str,
    plan: WorkPlan,
    test_command: str | None,
) -> str:
    """
    Convert the cloud's strategic WorkPlan into a compact local
    execution brief.

    File names from the cloud are hints only. Local Graphify/Serena
    discovery remains authoritative.
    """
    sections: list[str] = [
        "USER TASK:",
        task.strip(),
        "",
        "CLOUD STRATEGY:",
        f"Goal: {plan.goal}",
    ]

    if plan.steps:
        sections.extend(
            [
                "",
                "Steps:",
                *[
                    f"{i}. {step}"
                    for i, step in enumerate(
                        plan.steps,
                        start=1,
                    )
                ],
            ]
        )

    if plan.files:
        sections.extend(
            [
                "",
                "Possible files from cloud context "
                "(hints only; verify locally):",
                *[
                    f"- {path}"
                    for path in plan.files
                ],
            ]
        )

    if plan.constraints:
        sections.extend(
            [
                "",
                "Constraints:",
                *[
                    f"- {item}"
                    for item in plan.constraints
                ],
            ]
        )

    if plan.verification:
        sections.extend(
            [
                "",
                "Verification expectations:",
                *[
                    f"- {item}"
                    for item in plan.verification
                ],
            ]
        )

    if plan.worker_instruction:
        sections.extend(
            [
                "",
                "Worker instruction:",
                plan.worker_instruction,
            ]
        )

    if test_command:
        sections.extend(
            [
                "",
                "MANDATORY FINAL VALIDATION:",
                test_command,
            ]
        )

    sections.extend(
        [
            "",
            "Use local repository evidence rather than assuming "
            "the cloud's file hints are complete.",
        ]
    )

    return "\n".join(sections)


def _write_json(
    path: Path,
    value: Any,
) -> None:
    path.write_text(
        json.dumps(
            value,
            indent=2,
            default=str,
        )
        + "\n"
    )


async def run_opencode_cloud_task(
    *,
    repo: Path,
    task: str,
    cloud_plan: WorkPlan,
    test_command: str | None,
) -> dict[str, Any]:
    """
    Cloud strategy -> isolated OpenCode local stack ->
    deterministic outer validation.

    Deterministic validation is authoritative over the
    supervisor process return code.
    """
    repo = repo.expanduser().resolve()

    run_id = uuid.uuid4().hex[:12]

    run_dir = RUNS_ROOT / run_id
    run_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    execution_brief = _build_execution_brief(
        task,
        cloud_plan,
        test_command,
    )

    _write_json(
        run_dir / "task.json",
        {
            "run_id": run_id,
            "backend": "opencode-supervised",
            "repo": str(repo),
            "task": task,
            "test_command": test_command,
        },
    )

    _write_json(
        run_dir / "cloud-plan.json",
        cloud_plan.model_dump(),
    )

    (
        run_dir / "execution-brief.txt"
    ).write_text(execution_brief + "\n")

    context_path = run_dir / "context.json"

    context = {
        "run_id": run_id,
        "backend": "opencode-supervised",
        "phase": "EXECUTING",
        "repo": str(repo),
        "task": task,
        "test_command": test_command,
        "current_plan": cloud_plan.model_dump(),
        "failure": "",
        "cloud_escalations": 0,
        "supervisor_returncode": None,
    }

    _write_json(
        context_path,
        context,
    )

    try:
        supervised = await asyncio.to_thread(
            run_supervised,
            repo,
            execution_brief,
            # LEGACY pre-P3 cloud-coding path; explicit gemma-local
            # preserves prior behavior until a deliberate role migration.
            agent="gemma-local",
            cloud_led=True,
        )

    except Exception as exc:
        result = {
            "status": "LOCAL_ERROR",
            "run_id": run_id,
            "error": (
                "opencode-supervise invocation failed: "
                f"{exc}"
            ),
        }

        _write_json(
            run_dir / "final.json",
            result,
        )

        return result

    (
        run_dir / "opencode-supervise.txt"
    ).write_text(
        supervised.output + "\n"
    )

    context.update(
        {
            "phase": "VALIDATING",
            "supervisor_returncode": (
                supervised.returncode
            ),
        }
    )

    _write_json(
        context_path,
        context,
    )

    validation = await asyncio.to_thread(
        validate,
        repo,
        test_command,
    )

    (
        run_dir / "validation.txt"
    ).write_text(
        validation.output + "\n"
    )

    final_status = effective_status(
        supervised.returncode,
        validation_ok=validation.ok,
    )

    result: dict[str, Any] = {
        "status": final_status,
        "run_id": run_id,
        "supervisor": {
            "returncode": supervised.returncode,
            "status": supervisor_status(
                supervised.returncode
            ),
        },
        "validation": {
            "ok": validation.ok,
            "output": validation.output,
        },
    }

    if final_status == "SUCCESS":
        result["message"] = (
            "Local OpenCode execution produced a "
            "deterministically valid repository state."
        )

    elif final_status == "NEEDS_CLOUD_INPUT":
        result["failure"] = validation.output
        result["message"] = (
            "Local OpenCode/Nemotron recovery was exhausted "
            "and deterministic validation still fails."
        )

        # Keep cloud payload bounded. Full transcript stays local.
        result["local_execution_tail"] = (
            supervised.output[-6000:]
        )

    else:
        result["failure"] = validation.output
        result["message"] = (
            "The local coding process terminated unexpectedly "
            "and deterministic validation failed."
        )

        result["local_execution_tail"] = (
            supervised.output[-4000:]
        )

    context.update(
        {
            "phase": final_status,
            "failure": (
                ""
                if validation.ok
                else validation.output
            ),
            "supervisor_returncode": (
                supervised.returncode
            ),
        }
    )

    _write_json(
        context_path,
        context,
    )

    _write_json(
        run_dir / "final.json",
        result,
    )

    return result


async def resume_opencode_cloud_task(
    *,
    run_id: str,
    cloud_advice: str,
) -> dict[str, Any]:
    """
    Resume an OpenCode-backed hybrid run after the cloud model
    provides technical advice.

    The cloud supplies strategy only. Local implementation still
    runs through opencode-supervise, followed by authoritative
    deterministic validation.
    """
    run_dir = RUNS_ROOT / run_id
    context_path = run_dir / "context.json"

    if not context_path.exists():
        return {
            "run_id": run_id,
            "status": "ERROR",
            "error": (
                "Unknown run_id or no resumable "
                "OpenCode context."
            ),
        }

    context = json.loads(
        context_path.read_text()
    )

    repo = Path(
        context["repo"]
    ).expanduser().resolve()

    task = context["task"]
    test_command = context.get(
        "test_command"
    )

    cloud_plan = WorkPlan.model_validate(
        context["current_plan"]
    )

    # A previous MCP/client interruption may have occurred after
    # the worker successfully modified the repository but before
    # the orchestration result was persisted. Validation is the
    # authoritative source of truth, so check current state first.
    pre_validation = await asyncio.to_thread(
        validate,
        repo,
        test_command,
    )

    if pre_validation.ok:
        result = {
            "run_id": run_id,
            "status": "SUCCESS",
            "backend": "opencode-supervised",
            "recovered_from_phase": context.get(
                "phase",
                "UNKNOWN",
            ),
            "validation": {
                "ok": True,
                "output": pre_validation.output,
            },
        }

        context.update(
            {
                "phase": "SUCCESS",
                "failure": "",
            }
        )

        context.pop(
            "active_escalation",
            None,
        )

        _write_json(
            context_path,
            context,
        )

        _write_json(
            run_dir / "final.json",
            result,
        )

        return result

    escalation_number = (
        int(
            context.get(
                "cloud_escalations",
                0,
            )
        )
        + 1
    )

    advice_path = (
        run_dir
        / f"cloud-advice-{escalation_number:02d}.txt"
    )

    advice_path.write_text(
        cloud_advice.rstrip() + "\n"
    )

    previous_failure = context.get(
        "failure",
        "",
    )

    execution_brief = _build_execution_brief(
        task,
        cloud_plan,
        test_command,
    )

    resume_brief = (
        execution_brief
        + "\n\n"
        + "CLOUD RECOVERY ADVICE:\n"
        + cloud_advice.strip()
        + "\n\n"
        + "PREVIOUS DETERMINISTIC VALIDATION FAILURE:\n"
        + previous_failure.strip()
        + "\n\n"
        + "Continue from the CURRENT WORKING TREE. "
        + "Preserve useful existing work. "
        + "Apply the cloud advice only where supported by "
        + "local repository evidence. "
        + "Finish by running the mandatory validation."
    )

    (
        run_dir
        / f"resume-brief-{escalation_number:02d}.txt"
    ).write_text(
        resume_brief + "\n"
    )

    context.update(
        {
            "phase": "RESUMING",
            "active_escalation": escalation_number,
        }
    )

    _write_json(
        context_path,
        context,
    )

    try:
        supervised = await asyncio.to_thread(
            run_supervised,
            repo,
            resume_brief,
            # LEGACY pre-P3 cloud-coding path; explicit gemma-local
            # preserves prior behavior until a deliberate role migration.
            agent="gemma-local",
            cloud_led=True,
            timeout=150,
        )

    except Exception as exc:
        result = {
            "run_id": run_id,
            "status": "LOCAL_ERROR",
            "cloud_escalations": escalation_number,
            "error": (
                "OpenCode resume invocation failed: "
                f"{exc}"
            ),
        }

        _write_json(
            run_dir / "final.json",
            result,
        )

        return result

    (
        run_dir
        / f"opencode-resume-{escalation_number:02d}.txt"
    ).write_text(
        supervised.output + "\n"
    )

    validation = await asyncio.to_thread(
        validate,
        repo,
        test_command,
    )

    (
        run_dir
        / f"validation-resume-{escalation_number:02d}.txt"
    ).write_text(
        validation.output + "\n"
    )

    status = effective_status(
        supervised.returncode,
        validation_ok=validation.ok,
    )

    if status == "SUCCESS":
        result = {
            "run_id": run_id,
            "status": "SUCCESS",
            "backend": "opencode-supervised",
            "cloud_escalations": escalation_number,
            "supervisor": {
                "returncode": supervised.returncode,
                "status": supervisor_status(
                    supervised.returncode
                ),
            },
            "validation": {
                "ok": True,
                "output": validation.output,
            },
        }

        _write_json(
            run_dir / "final.json",
            result,
        )

        return result

    context.update(
        {
            "phase": "NEEDS_CLOUD_INPUT",
            "failure": validation.output,
            "cloud_escalations": escalation_number,
            "supervisor_returncode": (
                supervised.returncode
            ),
        }
    )

    context.pop(
        "active_escalation",
        None,
    )

    _write_json(
        context_path,
        context,
    )

    if escalation_number >= 2:
        context.update(
            {
                "phase": (
                    "FAILED_AFTER_CLOUD_ESCALATIONS"
                ),
                "cloud_escalations": escalation_number,
            }
        )

        context.pop(
            "active_escalation",
            None,
        )

        _write_json(
            context_path,
            context,
        )

        result = {
            "run_id": run_id,
            "status": (
                "FAILED_AFTER_CLOUD_ESCALATIONS"
            ),
            "backend": "opencode-supervised",
            "cloud_escalations": escalation_number,
            "failure": validation.output,
        }

        _write_json(
            run_dir / "final.json",
            result,
        )

        return result

    result = {
        "run_id": run_id,
        "status": "NEEDS_CLOUD_INPUT",
        "backend": "opencode-supervised",
        "reason": (
            "failure_persisted_after_cloud_advice"
        ),
        "cloud_escalations": escalation_number,
        "failure": validation.output,
        "supervisor": {
            "returncode": supervised.returncode,
            "status": supervisor_status(
                supervised.returncode
            ),
        },
        "local_execution_tail": (
            supervised.output[-6000:]
        ),
        "question_for_cloud": (
            "The previous cloud advice was applied through "
            "the local OpenCode stack, but deterministic "
            "validation still fails. Reassess the failure "
            "and provide a substantially different concrete "
            "technical strategy."
        ),
    }

    _write_json(
        run_dir
        / f"escalation-{escalation_number + 1:02d}.json",
        result,
    )

    _write_json(
        run_dir / "final.json",
        result,
    )

    return result
