"""Planning/approval references in AGY, live execution truth in Automaker."""

from __future__ import annotations

import hashlib

from app.automaker_local.schemas import JobCreateInput

from .schemas import FactoryApprovalStatus, FactoryExecutionState, normalize_execution
from .store import FactoryError


class FactoryService:
    def __init__(self, store, client):
        self.store = store
        self.client = client

    def register_plan(self, plan):
        return self.store.register(plan)

    def record_approval(self, approval):
        return self.store.approve(approval)

    @staticmethod
    def key(plan, task):
        return hashlib.sha256(
            f"agy-factory-v1:{plan.plan_id}:{plan.revision()}:{task.task_id}".encode()
        ).hexdigest()

    @staticmethod
    def feature(plan, task, deps):
        marker = f"[agy-factory-v1:{FactoryService.key(plan, task)}]"
        return JobCreateInput(
            title=task.title,
            description=task.description + "\n\n" + marker,
            dependencies=deps,
        ).to_feature()

    @staticmethod
    def validate_feature(feature, expected):
        if (
            not isinstance(feature, dict)
            or not isinstance(feature.get("id"), str)
            or not 1 <= len(feature["id"]) <= 100
        ):
            raise FactoryError("AUTOMAKER_BAD_RESPONSE")
        if any(
            feature.get(k, [] if k == "dependencies" else None)
            != expected.get(k, [] if k == "dependencies" else None)
            for k in ("title", "description", "dependencies")
        ):
            raise FactoryError("AUTOMAKER_CONFLICT")
        return feature["id"]

    def materialize_approved_plan(self, plan_id):
        plan = self.store.plan(plan_id)
        approval = self.store.approval(plan_id)
        if not approval or approval.status != FactoryApprovalStatus.APPROVED:
            raise FactoryError("APPROVAL_REQUIRED")
        if approval.plan_revision != plan.revision():
            raise FactoryError("APPROVAL_REVISION_MISMATCH")
        # Observe only. Never stop/start auto-mode, workers, or another scheduler.
        paths = {t.project_path for t in plan.tasks}
        projects = self.client.projects_overview().get("projects")
        if not isinstance(projects, list):
            raise FactoryError("AUTOMAKER_BAD_RESPONSE")
        if not paths.issubset(
            {p.get("projectPath") for p in projects if isinstance(p, dict)}
        ):
            raise FactoryError("AUTOMAKER_PROJECT_NOT_REGISTERED")
        for path in paths:
            autonomy = self.client.auto_mode_status(path)
            if (
                autonomy.get("isRunning") is not False
                or autonomy.get("isAutoLoopRunning", False) is not False
            ):
                raise FactoryError("AUTOMAKER_AUTONOMY_NOT_IDLE")
        mapping = {}
        for task in plan.ordered_tasks():
            deps = [
                mapping[d.requires_task_id]
                for d in plan.dependencies
                if d.task_id == task.task_id
            ]
            expected = self.feature(plan, task, deps)
            link, claimed = self.store.claim(plan, task, self.key(plan, task), approval)
            if link.get("job_id"):
                payload = self.client.feature_get(task.project_path, link["job_id"])
                job_id = self.validate_feature(payload.get("feature"), expected)
                if job_id != link["job_id"]:
                    raise FactoryError("JOB_MAPPING_CONFLICT")
            elif not claimed:
                payload = self.client.features_list(task.project_path)
                features = payload.get("features")
                if not isinstance(features, list):
                    raise FactoryError("AUTOMAKER_BAD_RESPONSE")
                marker = f"[agy-factory-v1:{link['key']}]"
                matches = [
                    f
                    for f in features
                    if isinstance(f, dict) and marker in str(f.get("description", ""))
                ]
                if len(matches) > 1:
                    raise FactoryError("AUTOMAKER_CONFLICT")
                if not matches:
                    raise FactoryError("MATERIALIZATION_UNCERTAIN")
                job_id = self.validate_feature(matches[0], expected)
            else:
                payload = self.client.feature_create(task.project_path, expected)
                job_id = self.validate_feature(payload.get("feature"), expected)
            self.store.mapped(plan_id, task.task_id, job_id)
            mapping[task.task_id] = job_id
        return {"plan_id": plan_id, "job_map": mapping, "workers_started": False}

    def job_map(self, plan_id):
        plan = self.store.plan(plan_id)
        return {
            "plan_id": plan_id,
            "goal": plan.goal.model_dump(),
            "tasks": [
                {
                    **t.model_dump(exclude={"title", "description", "project_path"}),
                    "job_ref": next(
                        (
                            link
                            for link in self.store.links(plan_id)
                            if link["task_id"] == t.task_id
                        ),
                        None,
                    ),
                }
                for t in plan.tasks
            ],
            "dependencies": [d.model_dump() for d in plan.dependencies],
        }

    def plan_status(self, plan_id):
        plan = self.store.plan(plan_id)
        approval = self.store.approval(plan_id)
        links = {r["task_id"]: r for r in self.store.links(plan_id)}
        rows, blockers = [], []
        for task in plan.tasks:
            link = links.get(task.task_id)
            raw = None
            state = FactoryExecutionState.NOT_MATERIALIZED
            if link and not link["job_id"]:
                state = FactoryExecutionState.BLOCKED
                blockers.append(
                    {"code": "MATERIALIZATION_UNCERTAIN", "task_id": task.task_id}
                )
            elif link:
                feature = self.client.feature_get(
                    task.project_path, link["job_id"]
                ).get("feature")
                if not isinstance(feature, dict) or feature.get("id") != link["job_id"]:
                    raise FactoryError("AUTOMAKER_BAD_RESPONSE")
                raw = feature.get("status")
                if not isinstance(raw, str) or len(raw) > 40:
                    raw = None
                state = normalize_execution(raw)
                if state == FactoryExecutionState.BLOCKED:
                    blockers.append(
                        {"code": "UNKNOWN_AUTOMAKER_STATE", "task_id": task.task_id}
                    )
            rows.append(
                {
                    "task_id": task.task_id,
                    "automaker_job_id": link["job_id"] if link else None,
                    "execution_state": state.value,
                    "raw_status": raw,
                }
            )
        by_id = {r["task_id"]: r for r in rows}
        for row in rows:
            row["dependency_ready"] = all(
                by_id[d.requires_task_id]["execution_state"] == "COMPLETED"
                for d in plan.dependencies
                if d.task_id == row["task_id"]
            )
        if not approval or approval.status != FactoryApprovalStatus.APPROVED:
            blockers.append({"code": "APPROVAL_REQUIRED"})
        return {
            "plan_id": plan_id,
            "goal": plan.goal.model_dump(),
            "approval_status": approval.status.value if approval else "PROPOSED",
            "approval_ref": approval.approval_id if approval else None,
            "approval_verification": "OPERATOR_ASSERTED_NOT_EXTERNALLY_VERIFIED",
            "jobs": rows,
            "blockers": blockers,
            "evidence": [],
            "evidence_status": "NOT_CONNECTED",
            "execution_authority": "Automaker",
        }

    def status(self):
        return {
            "plans": [self.plan_status(p) for p in self.store.plan_ids()],
            "planning_authority": "Argus",
            "workers_started": False,
        }
