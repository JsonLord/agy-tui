import argparse
import asyncio
import sys
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(
        description="Antigravity hybrid coding harness"
    )

    parser.add_argument(
        "repo",
        help="Repository/workspace to modify",
    )

    parser.add_argument(
        "task",
        help="Coding task",
    )

    parser.add_argument(
        "--test-command",
        default=None,
        help="Independent validation command",
    )

    return parser.parse_args()


# ---------------------------------------------------------------------------
# P11 — checkpoint viewer subcommand (read-only canonical state, plain text)
# P12 — extended with `decide`: the human G4 decision CLI (thin wrapper)
# ---------------------------------------------------------------------------


def build_checkpoint_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m app.main checkpoint",
        description="Inspect canonical AGY checkpoint-planning state "
                    "(read-only; never touches the OpenCode Todo "
                    "projection)",
    )
    sub = parser.add_subparsers(dest="checkpoint_command", required=True)

    view = sub.add_parser(
        "view",
        help="render plan/gate status for one plan from canonical state",
    )
    view.add_argument("--plan-id", required=True, help="plan id to view")
    view.add_argument(
        "--state-root",
        default="state",
        help="canonical state root (default: ./state — the directory "
             "holding plans/<plan_id>/ and runs/<plan_id>/)",
    )

    decide = sub.add_parser(
        "decide",
        help="submit a human G4 decision (approve|needs-changes|cancel) "
             "through the canonical G4Executor — thin wrapper, no "
             "transition logic lives here",
    )
    decide.add_argument("--plan-id", required=True, help="plan id")
    decide.add_argument("--checkpoint-id", required=True, help="checkpoint id")
    decide.add_argument(
        "--decision",
        required=False,
        default=None,
        choices=("approve", "needs-changes", "cancel"),
        help="the human verdict (argparse rejects anything else); omit it "
             "with --interactive to be prompted (P15-A)",
    )
    decide.add_argument(
        "--interactive",
        action="store_true",
        help="collect the verdict + VERBATIM feedback through the human "
             "surface (only consulted when --decision is omitted)",
    )
    decide.add_argument(
        "--feedback",
        default="",
        help="VERBATIM human feedback (mandatory for needs-changes)",
    )
    decide.add_argument(
        "--revision",
        type=int,
        default=None,
        help="decision revision (default: decisions ledger max + 1; "
             "a stale/explicit lower revision fails via the engine)",
    )
    decide.add_argument(
        "--actor",
        default="human:cli",
        help="who is deciding (default: human:cli)",
    )
    decide.add_argument(
        "--state-root",
        default="state",
        help="canonical state root (default: ./state)",
    )

    admit = sub.add_parser(
        "admit",
        help="admit a validated P12 continuation through the canonical "
             "G0 APPROVE path (thin wrapper over the admission service; "
             "admission only — no gate dispatch, no model calls)",
    )
    admit.add_argument("--plan-id", required=True, help="plan id")
    admit.add_argument(
        "--request-id",
        required=True,
        help="source HumanPlannerInputRequest id (the continuation "
             "artifact name under plans/<plan-id>/continuations/)",
    )
    admit.add_argument(
        "--revision",
        type=int,
        required=True,
        help="G0 approval revision — must equal the continuation revision "
             "(the service fails closed on a mismatch)",
    )
    admit.add_argument(
        "--approver",
        default="human:cli",
        help="who is approving (default: human:cli)",
    )
    admit.add_argument(
        "--state-root",
        default="state",
        help="canonical state root (default: ./state)",
    )

    sub.add_parser(
        "pending",
        help="list every parked human action (G4 WAITING_FOR_HUMAN plans "
             "and P14 parked cloud projects) from canonical state — "
             "read-only, plain text (P15-A)",
    ).add_argument(
        "--state-root",
        default="state",
        help="canonical state root (default: ./state)",
    )
    return parser


def checkpoint_main(argv: list[str]) -> int:
    """Entry for `... checkpoint ...`; returns the process exit code.

    Fail closed: missing/malformed canonical state prints a clear error
    and exits non-zero (CheckpointViewerError is the viewer's contract).
    ``decide`` (P12) is a THIN wrapper: it loads canonical state, builds
    the HumanGateRecord, and delegates the ENTIRE transition to the
    canonical G4Executor.apply_human_decision — no gate logic here.
    ``pending`` (P15-A) is the read-only cross-surface discovery of every
    parked human action (G4 + P14), rendered through the human surface.
    """
    from app.orchestration.checkpoint_planning.checkpoint_viewer import (
        CheckpointViewerError,
        view_checkpoint,
    )

    args = build_checkpoint_parser().parse_args(argv)
    if args.checkpoint_command == "view":
        try:
            rendered = view_checkpoint(args.state_root, args.plan_id)
        except CheckpointViewerError as exc:
            print(f"checkpoint view failed: {exc}", file=sys.stderr)
            return 1
        print(rendered, end="")
        return 0
    if args.checkpoint_command == "decide":
        return _checkpoint_decide(args)
    if args.checkpoint_command == "admit":
        return _checkpoint_admit(args)
    if args.checkpoint_command == "pending":
        return _checkpoint_pending(args)
    return 2  # pragma: no cover - argparse required=True guards this


def _checkpoint_pending(args) -> int:
    """`checkpoint pending` (P15-A) — every parked human action (G4
    WAITING_FOR_HUMAN + P14 parked projects) rendered as plain text from
    canonical state. Read-only; exit 1 on malformed canonical state."""
    from app.orchestration.checkpoint_planning.checkpoint_viewer import (
        CheckpointViewerError,
        list_parked_human_actions,
    )

    from app.human_surface import render_pending

    try:
        entries = list_parked_human_actions(args.state_root)
    except CheckpointViewerError as exc:
        print(f"checkpoint pending failed: {exc}", file=sys.stderr)
        return 1
    print(render_pending(entries), end="")
    return 0


def _checkpoint_admit(args) -> int:
    """`checkpoint admit` — canonical G0 admission of a validated
    continuation (P13). Thin wrapper: the ENTIRE admission (provenance
    check, re-validation, duplicate detection, submission) lives in the
    admission service; this CLI only parses and reports. Exit 0 on
    admitted/idempotent, 1 on any fail-closed rejection."""
    from datetime import datetime, timezone

    from app.orchestration.checkpoint_planning.continuation_admission import (
        ContinuationAdmissionError,
        admit_continuation,
    )
    from app.orchestration.checkpoint_planning.schemas import G0Approval

    from app.human_surface import HumanAuthorityError, require_human_actor

    try:
        require_human_actor(args.approver)
    except HumanAuthorityError as exc:
        print(f"checkpoint admit failed: {exc}", file=sys.stderr)
        return 1

    approval = G0Approval(
        decision="APPROVE",
        revision=args.revision,
        approver=args.approver,
        approved_at=datetime.now(timezone.utc).isoformat(),
    )
    try:
        result = admit_continuation(
            args.state_root, args.plan_id, args.request_id, g0_approval=approval
        )
    except ContinuationAdmissionError as exc:
        print(f"checkpoint admit failed: {exc}", file=sys.stderr)
        return 1
    suffix = " (idempotent re-admission)" if result.idempotent else ""
    print(
        f"admitted {args.plan_id} (request {args.request_id}, "
        f"revision {result.revision}): next gate {result.next_gate}{suffix}"
    )
    return 0


def _decisions_ledger_max(human_dir) -> int:
    """Highest revision present in the per-checkpoint decisions ledger
    (``decisions/<revision>.json`` file names); 0 when no ledger."""
    latest = 0
    if human_dir.is_dir():
        for path in (human_dir / "decisions").glob("*.json"):
            try:
                latest = max(latest, int(path.stem))
            except ValueError:
                continue
    return latest


def _collect_interactive_decision(args) -> int | None:
    """`checkpoint decide --interactive` (P15-A): prompt the human on
    stdin through the canonical pending payload.

    The question + legal decisions are built EXACTLY as the canonical
    ``G4Executor.wait`` does — ``wait`` runs on an in-memory deep copy of
    the checkpoint's gate state, so it is a pure render (no transition,
    no persistence, no I/O beyond the reader). Returns None to continue
    into ``_checkpoint_decide`` (with ``args.decision`` /
    ``args.feedback`` filled), or an exit code (1) when canonical state
    is unusable or the human's input is invalid.
    """
    import copy

    from app.orchestration.checkpoint_planning.checkpoint_viewer import (
        CheckpointViewerError,
        load_plan,
    )
    from app.orchestration.checkpoint_planning.g4_executor import (
        build_live_g4_executor,
    )
    from app.orchestration.checkpoint_planning.gate_engine import (
        GateTransitionError,
    )

    from app.human_surface import collect_g4_decision

    try:
        plan = load_plan(args.state_root, args.plan_id)
        checkpoint = next(
            (cp for cp in plan.checkpoints
             if cp.checkpoint_id == args.checkpoint_id),
            None,
        )
        if checkpoint is None:
            print(
                f"checkpoint decide failed: plan {args.plan_id!r} has no "
                f"checkpoint {args.checkpoint_id!r}",
                file=sys.stderr,
            )
            return 1
        payload = build_live_g4_executor(args.state_root).wait(
            copy.deepcopy(checkpoint), plan_id=plan.plan_id
        )
        decision, feedback = collect_g4_decision(
            input, payload["question"], payload["legal_decisions"]
        )
    except (
        CheckpointViewerError,
        GateTransitionError,
        ValueError,
        EOFError,
    ) as exc:
        print(f"checkpoint decide failed: {exc}", file=sys.stderr)
        return 1
    args.decision = decision.value.lower().replace("_", "-")
    args.feedback = feedback
    return None


def _checkpoint_decide(args) -> int:
    """`checkpoint decide` — canonical G4 human decision, thin wrapper."""
    import json
    from datetime import datetime, timezone

    from app.orchestration.checkpoint_planning.g4_executor import (
        G4_GATE_ID,
        build_live_g4_executor,
    )
    from app.orchestration.checkpoint_planning.gate_engine import (
        GateTransitionError,
    )
    from app.orchestration.checkpoint_planning.checkpoint_viewer import (
        CheckpointViewerError,
        find_plan_document,
        load_plan,
    )
    from app.orchestration.checkpoint_planning.schemas import (
        HumanDecision,
        HumanGateRecord,
    )

    # P15-A: --interactive may omit --decision; the verdict + VERBATIM
    # feedback are collected through the human surface and then flow
    # through the SAME path below (non-interactive behavior unchanged).
    if args.decision is None:
        if not args.interactive:
            build_checkpoint_parser().error(
                "the following arguments are required: --decision "
                "(or pass --interactive to be prompted)"
            )
        exit_code = _collect_interactive_decision(args)
        if exit_code is not None:
            return exit_code

    decision = HumanDecision(args.decision.replace("-", "_").upper())

    # Canonical state only — fail closed on anything missing/malformed.
    try:
        plan = load_plan(args.state_root, args.plan_id)
    except CheckpointViewerError as exc:
        print(f"checkpoint decide failed: {exc}", file=sys.stderr)
        return 1
    checkpoint = next(
        (cp for cp in plan.checkpoints
         if cp.checkpoint_id == args.checkpoint_id),
        None,
    )
    if checkpoint is None:
        print(
            f"checkpoint decide failed: plan {args.plan_id!r} has no "
            f"checkpoint {args.checkpoint_id!r}",
            file=sys.stderr,
        )
        return 1

    revision = args.revision
    if revision is None:
        # Default: next after the decisions ledger the executor persists
        # (G4Executor._human_dir convention) — first decision = 1. The
        # ENGINE still enforces its monotonic rule; a stale value fails.
        from pathlib import Path

        from app.orchestration.agy_runstate import safe_name

        human_dir = (
            Path(args.state_root) / "runs" / safe_name(args.plan_id)
            / "human" / safe_name(args.checkpoint_id)
        )
        revision = _decisions_ledger_max(human_dir) + 1

    record = HumanGateRecord(
        plan_id=plan.plan_id,
        checkpoint_id=checkpoint.checkpoint_id,
        gate_id=G4_GATE_ID,
        decision=decision,
        feedback=args.feedback,  # VERBATIM human text
        submitted_at=datetime.now(timezone.utc).isoformat(),
        actor=args.actor,
        provenance="cli",
        revision=revision,
    )

    # The canonical G4 transition — ALL validation/ledger/request
    # creation lives in the executor + engine, not in this CLI.
    try:
        executor = build_live_g4_executor(args.state_root)
        result = executor.apply_human_decision(checkpoint, record)
    except (GateTransitionError, ValueError) as exc:
        print(f"checkpoint decide failed: {exc}", file=sys.stderr)
        return 1
    except CheckpointViewerError as exc:
        print(f"checkpoint decide failed: {exc}", file=sys.stderr)
        return 1

    # Reflect the new durable gate state into the canonical plan doc so
    # the viewer (and the next decide) read truth, not a stale snapshot.
    try:
        plan_path = find_plan_document(args.state_root, plan.plan_id)
        doc = json.loads(plan_path.read_text(encoding="utf-8"))
        gate_records = {
            gate_id: rec.model_dump(mode="json")
            for gate_id, rec in checkpoint.gate_records.items()
        }
        for cp in doc.get("checkpoints", []):
            if cp.get("checkpoint_id") == checkpoint.checkpoint_id:
                cp["gate_records"] = gate_records
        from app.orchestration.agy_runstate import _atomic_write_json

        _atomic_write_json(plan_path, doc)
    except (OSError, ValueError) as exc:  # pragma: no cover - defensive
        print(
            f"checkpoint decide: decision accepted but plan doc not "
            f"updated: {exc}",
            file=sys.stderr,
        )
        return 1

    print(
        f"G4 {result['checkpoint_id']}: {result['decision']} -> "
        f"{result['status']} (revision {result['revision']})"
    )
    return 0


def project_main(argv: list[str]) -> int:
    """Entry for ``python3 -m app.main project …`` (P18).

    The ONE obvious production entrypoint. It drives the
    :class:`ProjectSupervisor` — the single lifecycle coordinator — and
    prints a compact, human-readable view. It performs only DETERMINISTIC
    work per invocation: no wait loops, no model-owned polling. ``advance``
    makes at most ONE cloud call and then reports the next park (human /
    workers / reentry / terminal). G0 admission + G4 are human-only
    (fail closed on any non-human actor, mirroring the engine).
    """
    parser = argparse.ArgumentParser(
        prog="agy-hybrid project",
        description="Run a project through the AGY Hybrid supervisor "
                    "(P18).",
    )
    sub = parser.add_subparsers(dest="project_command", required=True)

    p_start = sub.add_parser(
        "start", help="create a NEW project (no cloud call yet)"
    )
    p_start.add_argument("--project-id", required=True)
    p_start.add_argument("--prompt", required=True,
                         help="the initial user request")
    p_start.add_argument("--objective", required=True,
                         help="one-line project objective")
    p_start.add_argument("--state-root", default="state")
    p_start.add_argument("--workdir", default=".",
                         help="repo dir the batch workers operate on")

    p_status = sub.add_parser(
        "status", help="read-only: state, park, event feed, checkpoint"
    )
    p_status.add_argument("--project-id", required=True)
    p_status.add_argument("--state-root", default="state")
    p_status.add_argument("--workdir", default=".")

    p_advance = sub.add_parser(
        "advance", help="deterministic advance; at most ONE cloud call"
    )
    p_advance.add_argument("--project-id", required=True)
    p_advance.add_argument("--state-root", default="state")
    p_advance.add_argument("--workdir", default=".")

    p_answer = sub.add_parser(
        "answer", help="human answer to a parked CLARIFY / NEEDS_HUMAN"
    )
    p_answer.add_argument("--project-id", required=True)
    p_answer.add_argument("--text", required=True)
    p_answer.add_argument("--actor", default="human:cli",
                          help="must be human:* (fail closed otherwise)")
    p_answer.add_argument("--state-root", default="state")
    p_answer.add_argument("--workdir", default=".")

    p_approve = sub.add_parser(
        "approve", help="G4 HUMAN decision on a lowered checkpoint "
                        "(APPROVE | NEEDS_CHANGES | CANCEL)"
    )
    p_approve.add_argument("--project-id", required=True)
    p_approve.add_argument("--decision", required=True,
                           choices=["APPROVE", "NEEDS_CHANGES", "CANCEL"])
    p_approve.add_argument("--feedback", default="")
    p_approve.add_argument("--actor", default="human:cli")
    p_approve.add_argument("--state-root", default="state")
    p_approve.add_argument("--workdir", default=".")

    p_g0 = sub.add_parser(
        "admit", help="G0 HUMAN admission of a P16-lowered checkpoint "
                      "plan (the only route to submission)"
    )
    p_g0.add_argument("--project-id", required=True)
    p_g0.add_argument("--plan-id", required=True,
                      help="p16-<project>-<batch>")
    p_g0.add_argument("--approver", default="human:cli")
    p_g0.add_argument("--state-root", default="state")
    p_g0.add_argument("--workdir", default=".")

    p_cancel = sub.add_parser("cancel", help="operator cancel (terminal)")
    p_cancel.add_argument("--project-id", required=True)
    p_cancel.add_argument("--reason", default="cancelled by operator")
    p_cancel.add_argument("--state-root", default="state")
    p_cancel.add_argument("--workdir", default=".")

    p_resume = sub.add_parser(
        "resume", help="re-derive from persisted state then advance "
                       "(restart recovery; no duplicated work)"
    )
    p_resume.add_argument("--project-id", required=True)
    p_resume.add_argument("--state-root", default="state")
    p_resume.add_argument("--workdir", default=".")

    args = parser.parse_args(argv)
    return _project_dispatch(args)


def _build_production_supervisor(
    state_root: str, project_id: str, workdir: str
) -> "ProjectSupervisor":
    """Production wiring: tool-free P17 planner (P14 quota router) +
    supervised OpenCode workers + the canonical P11–P13 checkpoint
    executor. No implicit agent defaults (P9.2)."""
    from app.orchestration.cloud_batch.dispatcher import (
        OpenCodeWorkerRunner,
    )
    from app.orchestration.project_supervisor import (
        ProjectSupervisor,
        SupervisorConfig,
    )
    from app.orchestration.project_supervisor_checkpoint import (
        CanonicalCheckpointExecutor,
    )

    return ProjectSupervisor(
        SupervisorConfig(
            state_root=state_root,
            project_id=project_id,
            runner=OpenCodeWorkerRunner(),
            checkpoint_executor=CanonicalCheckpointExecutor(
                repo=workdir, state_root=state_root
            ),
            base_sha=_git_head(workdir),
            workdir=workdir,
        )
    )


def _git_head(workdir: str) -> str:
    """Best-effort HEAD sha of the workdir (deterministic fallback)."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "0" * 40


def _project_dispatch(args) -> int:
    from app.orchestration.cloud_batch.planner import CloudPlannerUnavailable
    from app.orchestration.project_supervisor import ProjectSupervisorError
    from app.orchestration.project_supervisor_checkpoint import (
        CheckpointExecutorError,
    )

    def _view(sup) -> str:
        v = sup.get_project()
        lines = [
            f"project:  {v['project_id']}",
            f"state:    {v['state']}",
            f"gen:      {v['generation']}",
            f"objective:{v['objective']}",
            f"cloud_calls: {v['cloud_calls']}",
        ]
        park = v.get("park")
        if park:
            lines.append(f"park:     {park.get('kind')}")
            if park.get("question"):
                lines.append(f"  question: {park['question']}")
        if v.get("checkpoint_batch"):
            lines.append(f"checkpoint batch: {v['checkpoint_batch']}")
            if v.get("g4_status"):
                lines.append(f"  g4_status: {v['g4_status']}")
        if v.get("events"):
            last = v["events"][-1]
            lines.append(f"last event: {last}")
        return "\n".join(lines)

    try:
        if args.project_command == "start":
            sup = _build_production_supervisor(
                args.state_root, args.project_id, args.workdir
            )
            sup.create_project(
                initial_prompt=args.prompt, objective=args.objective
            )
            print(_view(sup))
            print("\nNext: project advance (makes the first cloud call — "
                  "CLARIFY)")
            return 0

        if args.project_command == "status":
            sup = _build_production_supervisor(
                args.state_root, args.project_id, args.workdir
            )
            sup.open_project(args.project_id)
            print(_view(sup))
            return 0

        if args.project_command in ("advance", "resume"):
            sup = _build_production_supervisor(
                args.state_root, args.project_id, args.workdir
            )
            sup.open_project(args.project_id)
            result = sup.advance_project()
            print(_view(sup))
            print(f"\nadvance -> {result.get('action') or 'done'}")
            return 0

        if args.project_command == "answer":
            sup = _build_production_supervisor(
                args.state_root, args.project_id, args.workdir
            )
            sup.open_project(args.project_id)
            res = sup.submit_human_input(text=args.text, actor=args.actor)
            print(f"answered ({res.get('kind')}); project now parked or "
                  f"resumable:")
            print(_view(sup))
            return 0

        if args.project_command == "approve":
            sup = _build_production_supervisor(
                args.state_root, args.project_id, args.workdir
            )
            sup.open_project(args.project_id)
            sup.submit_g4_decision(
                decision=args.decision,
                feedback=args.feedback,
                actor=args.actor,
            )
            print(_view(sup))
            return 0

        if args.project_command == "admit":
            from app.orchestration.project_supervisor import (
                load_checkpoint_plan,
            )
            from app.orchestration.project_supervisor_checkpoint import (
                CheckpointExecutorError,
                CanonicalCheckpointExecutor,
            )

            sup = _build_production_supervisor(
                args.state_root, args.project_id, args.workdir
            )
            sup.open_project(args.project_id)
            batch = sup.checkpoint_batch()
            if batch is None:
                raise ProjectSupervisorError("no checkpoint plan found")
            plan = load_checkpoint_plan(
                sup.store, args.project_id, batch
            )
            executor = CanonicalCheckpointExecutor(
                repo=args.workdir, state_root=args.state_root
            )
            result = executor.admit(plan, actor=args.approver)
            print(f"G0 admission: {result['status']} "
                  f"(idempotent={result['idempotent']})")
            return 0

        if args.project_command == "cancel":
            sup = _build_production_supervisor(
                args.state_root, args.project_id, args.workdir
            )
            sup.open_project(args.project_id)
            sup.cancel_project(reason=args.reason)
            print(_view(sup))
            return 0

        return 2  # pragma: no cover
    except ProjectSupervisorError as exc:
        print(f"project {args.project_command} failed: {exc}",
              file=sys.stderr)
        return 1
    except CheckpointExecutorError as exc:
        print(f"checkpoint admission/execution failed: {exc}",
              file=sys.stderr)
        return 1
    except CloudPlannerUnavailable as exc:
        # The tool-free planner needs Gemini/Vertex credentials (P17).
        # Fail closed with a clean message — the project state is
        # untouched and the call is not counted.
        print(f"project {args.project_command} failed: cloud planner "
              f"unavailable — {exc}", file=sys.stderr)
        return 1


async def async_main():
    # P11: lazy so `python3 -m app.main checkpoint …` runs in environments
    # without the google.antigravity host runtime (the viewer is pure
    # stdlib + domain schemas). The run-task path is unchanged.
    from app.config import Settings
    from app.orchestration.loop import run_task

    args = parse_args()

    repo = Path(args.repo).expanduser().resolve()

    if not repo.is_dir():
        raise SystemExit(
            f"Repository does not exist: {repo}"
        )

    settings = Settings.from_env()

    success = await run_task(
        settings=settings,
        repo=repo,
        task=args.task,
        test_command=args.test_command,
    )

    raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "checkpoint":
        # P11 viewer path — synchronous, read-only, no run loop.
        raise SystemExit(checkpoint_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "project":
        # P18 supervisor path — synchronous, deterministic per call,
        # at most one cloud call per advance.
        raise SystemExit(project_main(sys.argv[2:]))
    asyncio.run(async_main())
