"""P7a — append-only JSONL event log with bounded disk usage.

Meaningful transitions ONLY (the engine never asks for an event when nothing
changed), so there is no duplicate spam. Each event:

    {"ts", "job_id", "event_type", "state", "evidence" <compact dict>}

Rotation: events.jsonl -> events.jsonl.1 -> ... events.jsonl.N (N small),
so the daemon keeps only bounded disk. Reading is tail-only (never loads the
whole log; a bounded final chunk is scanned backwards).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional


class EventLog:
    def __init__(self, root, *, max_bytes: int = 1_000_000, rotations: int = 3,
                 fsync: bool = True):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "events.jsonl"
        self.max_bytes = int(max_bytes)
        self.rotations = int(rotations)
        self.fsync = fsync

    def emit(self, ts: float, job_id: str, event_type: str, state: str,
             evidence: Optional[Dict[str, object]] = None) -> Dict[str, object]:
        event = {
            "ts": round(float(ts), 3),
            "job_id": job_id,
            "event_type": event_type,
            "state": state,
            "evidence": evidence or {},
        }
        self._maybe_rotate()
        line = json.dumps(event, separators=(",", ":"), sort_keys=False) + "\n"
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            if self.fsync:
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
        return event

    def _maybe_rotate(self) -> None:
        try:
            if not self.path.exists() or self.path.stat().st_size < self.max_bytes:
                return
        except OSError:
            return
        oldest = self.root / f"events.jsonl.{self.rotations}"
        if oldest.exists():
            try:
                oldest.unlink()
            except OSError:
                pass
        for i in range(self.rotations - 1, 0, -1):
            src = self.root / f"events.jsonl.{i}"
            if src.exists():
                try:
                    src.replace(self.root / f"events.jsonl.{i + 1}")
                except OSError:
                    pass
        try:
            self.path.replace(self.root / "events.jsonl.1")
        except OSError:
            pass

    def _read_last_chunk(self, max_bytes: int = 512_000) -> List[Dict[str, object]]:
        if not self.path.exists():
            return []
        try:
            size = self.path.stat().st_size
            with open(self.path, "rb") as fh:
                fh.seek(max(0, size - max_bytes))
                chunk = fh.read()
        except OSError:
            return []
        text = chunk.decode("utf-8", "replace")
        if size > max_bytes and "\n" in text:
            text = text.split("\n", 1)[1]  # drop the (possibly partial) first line
        out: List[Dict[str, object]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def tail_for(self, job_id: Optional[str] = None, n: int = 50) -> List[Dict[str, object]]:
        """Last n events (optionally for one job), tail-read only."""
        events = self._read_last_chunk()
        if job_id is not None:
            events = [e for e in events if e.get("job_id") == job_id]
        return events[-n:]

    def event_types_for(self, job_id: str) -> List[str]:
        return [str(e.get("event_type")) for e in self.tail_for(job_id, n=10_000)]

    def count_type(self, job_id: str, event_type: str) -> int:
        return sum(1 for e in self.tail_for(job_id, n=10_000)
                   if e.get("event_type") == event_type)