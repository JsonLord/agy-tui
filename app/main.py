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
        required=True,
        choices=("approve", "needs-changes", "cancel"),
        help="the human verdict (argparse rejects anything else)",
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
    return parser


def checkpoint_main(argv: list[str]) -> int:
    """Entry for `... checkpoint ...`; returns the process exit code.

    Fail closed: missing/malformed canonical state prints a clear error
    and exits non-zero (CheckpointViewerError is the viewer's contract).
    ``decide`` (P12) is a THIN wrapper: it loads canonical state, builds
    the HumanGateRecord, and delegates the ENTIRE transition to the
    canonical G4Executor.apply_human_decision — no gate logic here.
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
    return 2  # pragma: no cover - argparse required=True guards this


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
    asyncio.run(async_main())
