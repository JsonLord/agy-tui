from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .policy import HybridError


class RunStore:
    """Durable evidence and conservative leases, not an autonomous campaign engine."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.executescript("""PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, workspace_id TEXT, root TEXT, role TEXT, status TEXT, session_id TEXT, model TEXT, error TEXT, created REAL, updated REAL, deadline REAL);
CREATE TABLE IF NOT EXISTS sessions(session_id TEXT PRIMARY KEY, workspace_id TEXT, root TEXT, role TEXT, model TEXT);
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, kind TEXT, data TEXT, timestamp REAL);
CREATE TABLE IF NOT EXISTS assistance(parent_id TEXT, run_id TEXT UNIQUE);
""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _event(self, db, run_id, kind, data):
        db.execute(
            "INSERT INTO events(run_id,kind,data,timestamp) VALUES(?,?,?,?)",
            (run_id, kind, json.dumps(data), time.time()),
        )

    def audit(self, run_id, kind, data):
        with self.connect() as db:
            self._event(db, run_id, kind, data)

    def begin(self, workspace, role, timeout, parent_id=""):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT id FROM runs WHERE workspace_id=? AND status IN ('STARTING','RUNNING','REMOTE_STOP_UNCONFIRMED')",
                (workspace.id,),
            ).fetchone():
                raise HybridError("WORKSPACE_BUSY_OR_RECOVERY_REQUIRED")
            if parent_id:
                parent = db.execute(
                    "SELECT * FROM runs WHERE id=?", (parent_id,)
                ).fetchone()
                if (
                    not parent
                    or parent["workspace_id"] != workspace.id
                    or parent["role"] == "supervisor"
                ):
                    raise HybridError("INVALID_ASSISTANCE_PARENT")
                if (
                    db.execute(
                        "SELECT COUNT(*) FROM assistance WHERE parent_id=?",
                        (parent_id,),
                    ).fetchone()[0]
                    >= 2
                ):
                    raise HybridError("ASSISTANCE_LIMIT")
            key, now = uuid.uuid4().hex, time.time()
            db.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    workspace.id,
                    str(workspace.root),
                    role,
                    "STARTING",
                    None,
                    None,
                    None,
                    now,
                    now,
                    now + timeout,
                ),
            )
            if parent_id:
                db.execute("INSERT INTO assistance VALUES(?,?)", (parent_id, key))
            self._event(db, key, "created", {"role": role})
            return key

    def verify_session(self, session_id, workspace, role):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        if not row or (row["workspace_id"], row["root"], row["role"]) != (
            workspace.id,
            str(workspace.root),
            role,
        ):
            raise HybridError("SESSION_WORKSPACE_OR_ROLE_MISMATCH")
        return dict(row)

    def bind(self, key, workspace, session):
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO sessions VALUES(?,?,?,?,?)",
                (
                    session["session_id"],
                    workspace.id,
                    session["root"],
                    session["role"],
                    session["model"],
                ),
            )
            old = db.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session["session_id"],)
            ).fetchone()
            if (old["workspace_id"], old["root"], old["role"], old["model"]) != (
                workspace.id,
                session["root"],
                session["role"],
                session["model"],
            ):
                raise HybridError("SESSION_BINDING_CONFLICT")
            db.execute(
                "UPDATE runs SET status='RUNNING',session_id=?,model=?,updated=? WHERE id=?",
                (session["session_id"], session["model"], time.time(), key),
            )
            self._event(db, key, "session_bound", session)

    def finish(self, key, status, error=None):
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET status=?,error=?,updated=? WHERE id=?",
                (status, error, time.time(), key),
            )
            self._event(db, key, "finished", {"status": status, "error": error})

    def status(self, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (key,)).fetchone()
            events = db.execute(
                "SELECT seq,kind,data,timestamp FROM events WHERE run_id=? ORDER BY seq",
                (key,),
            ).fetchall()
        if not row:
            raise HybridError("UNKNOWN_RUN")
        result = dict(row)
        result["run_id"] = result.pop("id")
        result["events"] = [{**dict(e), "data": json.loads(e["data"])} for e in events]
        result["accepted"] = False
        result["recovery_required"] = row["status"] == "REMOTE_STOP_UNCONFIRMED" or (
            row["status"] in {"STARTING", "RUNNING"} and row["deadline"] < time.time()
        )
        return result

    def latest(self):
        with self.connect() as db:
            row = db.execute(
                "SELECT id FROM runs ORDER BY created DESC LIMIT 1"
            ).fetchone()
        return (
            self.status(row["id"])
            if row
            else {"status": "No workspace-native run state exists yet."}
        )
