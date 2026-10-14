"""AGY reference-only bridge ledger on the existing factory SQLite store."""

from __future__ import annotations

import hashlib
import json

from app.factory_local.store import FactoryError


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class BridgeStore:
    def __init__(self, factory):
        self.factory = factory
        with factory.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS argus_imports(project_id TEXT NOT NULL, source_ref TEXT NOT NULL, plan_id TEXT NOT NULL UNIQUE, revision TEXT NOT NULL, PRIMARY KEY(project_id,source_ref));
                CREATE TABLE IF NOT EXISTS argus_requests(request_id TEXT PRIMARY KEY, kind TEXT NOT NULL, digest TEXT NOT NULL, project_id TEXT NOT NULL, result TEXT);
                CREATE TABLE IF NOT EXISTS argus_reviews(review_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, task_id TEXT NOT NULL, envelope TEXT NOT NULL);
            """)

    def import_plan(self, project_id, source_ref, plan):
        with self.factory.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT plan_id,revision FROM argus_imports WHERE project_id=? AND source_ref=?",
                (project_id, source_ref),
            ).fetchone()
            if old:
                if tuple(old) != (plan.plan_id, plan.revision()):
                    raise FactoryError("ARGUS_IMPORT_CONFLICT")
            else:
                if db.execute(
                    "SELECT 1 FROM plans WHERE id=?", (plan.plan_id,)
                ).fetchone():
                    raise FactoryError("ARGUS_IMPORT_CONFLICT")
                db.execute(
                    "INSERT INTO plans VALUES(?,?,?)",
                    (plan.plan_id, plan.revision(), plan.model_dump_json()),
                )
                db.execute(
                    "INSERT INTO argus_imports VALUES(?,?,?,?)",
                    (project_id, source_ref, plan.plan_id, plan.revision()),
                )
        return {
            "plan_id": plan.plan_id,
            "plan_revision": plan.revision(),
            "argus_project_id": project_id,
            "argus_source_ref": source_ref,
            "approval_status": self.factory.approval(plan.plan_id).status.value
            if self.factory.approval(plan.plan_id)
            else "PROPOSED",
        }

    def lookup(self, request_id, kind, digest, project_id):
        with self.factory.connect() as db:
            row = db.execute(
                "SELECT * FROM argus_requests WHERE request_id=?", (request_id,)
            ).fetchone()
        if row is None:
            return None
        if (row["kind"], row["digest"], row["project_id"]) != (
            kind,
            digest,
            project_id,
        ):
            raise FactoryError("ARGUS_REQUEST_CONFLICT")
        if row["result"] is None:
            raise FactoryError("ARGUS_OUTCOME_UNCERTAIN")
        return json.loads(row["result"])

    def claim(self, request_id, kind, digest, project_id):
        with self.factory.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM argus_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if row:
                if (row["kind"], row["digest"], row["project_id"]) != (
                    kind,
                    digest,
                    project_id,
                ):
                    raise FactoryError("ARGUS_REQUEST_CONFLICT")
                if row["result"] is None:
                    raise FactoryError("ARGUS_OUTCOME_UNCERTAIN")
                return json.loads(row["result"])
            db.execute(
                "INSERT INTO argus_requests VALUES(?,?,?,?,NULL)",
                (request_id, kind, digest, project_id),
            )
        return None

    def finish(self, request_id, result):
        with self.factory.connect() as db:
            db.execute(
                "UPDATE argus_requests SET result=? WHERE request_id=? AND result IS NULL",
                (json.dumps(result), request_id),
            )

    def save_review(self, result):
        with self.factory.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT envelope FROM argus_reviews WHERE review_id=?",
                (result.review_id,),
            ).fetchone()
            if old and json.loads(old[0]) != result.model_dump(mode="json"):
                raise FactoryError("ARGUS_REVIEW_CONFLICT")
            db.execute(
                "INSERT OR IGNORE INTO argus_reviews VALUES(?,?,?,?)",
                (
                    result.review_id,
                    result.plan_id,
                    result.task_id,
                    result.model_dump_json(),
                ),
            )

    def preview(self, plan_id):
        plan = self.factory.plan(plan_id)
        with self.factory.connect() as db:
            row = db.execute(
                "SELECT project_id,source_ref FROM argus_imports WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
        if not row:
            raise FactoryError("ARGUS_PLAN_NOT_IMPORTED")
        return {
            "plan": plan.model_dump(mode="json"),
            "plan_revision": plan.revision(),
            "argus_project_id": row[0],
            "argus_source_ref": row[1],
        }

    def reviews(self, plan_id):
        self.factory.plan(plan_id)
        with self.factory.connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT envelope FROM argus_reviews WHERE plan_id=? ORDER BY rowid DESC LIMIT 100",
                    (plan_id,),
                )
            ]

    def uncertain_requests(self, project_id):
        with self.factory.connect() as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT request_id FROM argus_requests WHERE project_id=? AND result IS NULL LIMIT 100",
                    (project_id,),
                )
            ]
