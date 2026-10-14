"""SQLite reference registry. Durable intent precedes every external create."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .schemas import FactoryApproval, FactoryPlan


class FactoryError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class FactoryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, revision TEXT NOT NULL, envelope TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, envelope TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS links(plan_id TEXT NOT NULL, task_id TEXT NOT NULL, key TEXT NOT NULL UNIQUE, approval_id TEXT NOT NULL, job_id TEXT, phase TEXT NOT NULL, PRIMARY KEY(plan_id,task_id), UNIQUE(plan_id,job_id));
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def register(self, plan: FactoryPlan):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT revision FROM plans WHERE id=?", (plan.plan_id,)
            ).fetchone()
            if old and old[0] != plan.revision():
                raise FactoryError("PLAN_REVISION_CONFLICT")
            db.execute(
                "INSERT OR IGNORE INTO plans VALUES(?,?,?)",
                (plan.plan_id, plan.revision(), plan.model_dump_json()),
            )
        return {"plan_id": plan.plan_id, "plan_revision": plan.revision()}

    def plan(self, plan_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT envelope FROM plans WHERE id=?", (plan_id,)
            ).fetchone()
        if not row:
            raise FactoryError("PLAN_NOT_FOUND")
        return FactoryPlan.model_validate_json(row[0])

    def approve(self, approval: FactoryApproval):
        plan = self.plan(approval.plan_id)
        if plan.revision() != approval.plan_revision:
            raise FactoryError("APPROVAL_REVISION_MISMATCH")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT envelope FROM approvals WHERE id=?", (approval.approval_id,)
            ).fetchone()
            if old:
                if json.loads(old[0]) != approval.model_dump(mode="json"):
                    raise FactoryError("APPROVAL_ID_CONFLICT")
                return {"approval_id": approval.approval_id}
            if db.execute(
                "SELECT 1 FROM links WHERE plan_id=?", (plan.plan_id,)
            ).fetchone():
                raise FactoryError("MATERIALIZATION_ALREADY_STARTED")
            db.execute(
                "INSERT INTO approvals VALUES(?,?,?)",
                (approval.approval_id, approval.plan_id, approval.model_dump_json()),
            )
        return {"approval_id": approval.approval_id}

    def approval(self, plan_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT envelope FROM approvals WHERE plan_id=? ORDER BY rowid DESC LIMIT 1",
                (plan_id,),
            ).fetchone()
        return FactoryApproval.model_validate_json(row[0]) if row else None

    def claim(self, plan, task, key, approval):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            latest = db.execute(
                "SELECT envelope FROM approvals WHERE plan_id=? ORDER BY rowid DESC LIMIT 1",
                (plan.plan_id,),
            ).fetchone()
            if not latest or json.loads(latest[0]) != approval.model_dump(mode="json"):
                raise FactoryError("APPROVAL_CHANGED")
            existing = db.execute(
                "SELECT * FROM links WHERE plan_id=? AND task_id=?",
                (plan.plan_id, task.task_id),
            ).fetchone()
            if existing:
                return dict(existing), False
            db.execute(
                "INSERT INTO links VALUES(?,?,?,?,NULL,'INTENT')",
                (plan.plan_id, task.task_id, key, approval.approval_id),
            )
        return {"key": key, "job_id": None, "phase": "INTENT"}, True

    def mapped(self, plan_id, task_id, job_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT job_id FROM links WHERE plan_id=? AND task_id=?",
                (plan_id, task_id),
            ).fetchone()
            if not row or (row[0] is not None and row[0] != job_id):
                raise FactoryError("JOB_MAPPING_CONFLICT")
            db.execute(
                "UPDATE links SET job_id=?,phase='MAPPED' WHERE plan_id=? AND task_id=?",
                (job_id, plan_id, task_id),
            )

    def links(self, plan_id):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM links WHERE plan_id=? ORDER BY task_id", (plan_id,)
                )
            ]

    def plan_ids(self):
        with self.connect() as db:
            return [
                row[0]
                for row in db.execute("SELECT id FROM plans ORDER BY id LIMIT 100")
            ]
