"""P7c — SPARK INCIDENT INTERPRETER (spark-x2.5-1.7b).

Spark is invoked ONLY after Laya says needs_interpretation=yes or deterministic
policy flags a hard-to-classify incident (STALL_SUSPECTED persisting beyond a
configured grace while the process is still alive).

Spark explains; it does NOT kill processes, mutate repos, commit, change
permissions, or replan. Its recommended_action is ADVICE consumed by sentinel
policy. Malformed/timeout (or missing credential) -> safe failure: status
recorded, NO state change, keep watching per policy.

Architectural write-isolation: this module imports ONLY the stdlib and takes
exactly one input — a bounded incident packet dict — so it has no handle to
job records, the store, the event log, the API, or the daemon.

Endpoint validation (this machine, no token): GET / -> 200 text/html;
GET /v1/models -> 401 {"detail":"Invalid API key"};
POST /v1/chat/completions (no token) -> 401 {"detail":"Invalid API key"}.
The bearer token (SPARK_API_KEY, env only, [REDACTED] everywhere) was NOT
supplied for P7, so the live leg runs the PENDING_CREDENTIAL path below with
the client validated against a clearly-labeled local mock HTTP server.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

SPARK_DEFAULT_BASE_URL = "https://leon4gr45-llama.hf.space"
SPARK_DEFAULT_MODEL = "spark-x2.5-1.7b"
SPARK_TIMEOUT_S = 60.0

RECOMMENDED_ACTIONS = frozenset(
    {"WAIT", "RECHECK", "ESCALATE", "CANCEL_RECOMMENDED", "DIAGNOSTIC_REQUIRED"}
)

# The packet allowlist: build_incident_packet() emits EXACTLY these top-level
# fields — never full history, never raw repo content.
PACKET_KEYS = frozenset(
    {
        "job_id",
        "state",
        "elapsed_s",
        "deadline_remaining_s",
        "quiet_s",
        "cpu_pct",
        "command_hash",
        "cwd",
        "checkpoint",
        "recent_checkpoints",
        "tail_lines",
        "transitions",
        "laya",
        "previous_incident",
        "metrics",
    }
)

VERBATIM_SKEL = {
    "type": "object",
    "required": ["likely_state", "confidence", "recommended_action",
                 "wait_seconds", "reason", "supervisor_needed"],
    "properties": {"likely_state": {"type": "string"},
                   "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                   "recommended_action": {"enum": sorted(RECOMMENDED_ACTIONS)},
                   "wait_seconds": {"type": ["integer", "null"]},
                   "reason": {"type": "string"},
                   "supervisor_needed": {"type": "boolean"}},
}

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$", re.M)


def build_incident_packet(
    *,
    job_id: str,
    state: str,
    metadata: Dict[str, Any],
    evidence: Dict[str, Any],
    laya: Optional[Dict[str, Any]],
    tail_lines: List[str],
    recent_checkpoints: List[str],
    transitions: List[Dict[str, Any]],
    previous_incident: Optional[Dict[str, Any]] = None,
    metrics: Optional[Dict[str, Any]] = None,
    max_tail_lines: int = 100,
    max_line_chars: int = 400,
    max_checkpoints: int = 5,
    max_transitions: int = 10,
    max_bytes: int = 16384,
) -> Dict[str, Any]:
    """Bounded incident packet. Inputs are already-extracted primitives, so the
    interpreter layer never touches records/stores/logs directly."""

    def clip_lines(lines: List[str], cap_n: int) -> List[str]:
        out = [str(x)[:max_line_chars] for x in (lines or [])[-cap_n:]]
        return out

    packet: Dict[str, Any] = {
        "job_id": str(job_id),
        "state": str(state),
        "elapsed_s": evidence.get("elapsed_s"),
        "deadline_remaining_s": evidence.get("deadline_remaining_s"),
        "quiet_s": evidence.get("quiet_s"),
        "cpu_pct": evidence.get("cpu_pct"),
        "command_hash": str(metadata.get("command_hash", ""))[:64],
        "cwd": str(metadata.get("cwd", ""))[:200],
        "checkpoint": (evidence.get("checkpoint") or None),
        "recent_checkpoints": clip_lines(
            [str(c) for c in (recent_checkpoints or [])], max_checkpoints),
        "tail_lines": clip_lines(tail_lines or [], max_tail_lines),
        "transitions": list((transitions or [])[-max_transitions:]),
        "laya": laya,
        "previous_incident": previous_incident,
        "metrics": metrics or {},
    }
    if isinstance(packet["checkpoint"], str):
        packet["checkpoint"] = packet["checkpoint"][:200]
    # enforce the byte bound by dropping oldest tail lines first
    while len(json.dumps(packet, separators=(",", ":")).encode("utf-8")) > max_bytes:
        if packet["tail_lines"]:
            packet["tail_lines"] = packet["tail_lines"][1:]
        elif packet["transitions"]:
            packet["transitions"] = packet["transitions"][1:]
        else:
            packet["tail_lines"] = []
            break
    return packet


@dataclass
class SparkVerdict:
    """Strictly validated interpretation result. status != 'ok' == safe failure."""

    status: str = "invalid"  # ok | invalid | timeout | error | credential_missing
    likely_state: Optional[str] = None
    confidence: Optional[float] = None
    recommended_action: Optional[str] = None
    wait_seconds: Optional[int] = None
    reason: Optional[str] = None
    supervisor_needed: Optional[bool] = None
    latency_s: Optional[float] = None
    failure: Optional[str] = None

    @property
    def is_ok(self) -> bool:
        return self.status == "ok"

    @property
    def escalates(self) -> bool:
        if not self.is_ok:
            return False
        return bool(self.supervisor_needed) or self.recommended_action in {
            "ESCALATE", "CANCEL_RECOMMENDED", "DIAGNOSTIC_REQUIRED"
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "likely_state": self.likely_state,
            "confidence": self.confidence,
            "recommended_action": self.recommended_action,
            "wait_seconds": self.wait_seconds,
            "reason": (self.reason or "")[:300],
            "supervisor_needed": self.supervisor_needed,
            "latency_s": self.latency_s,
            "failure": self.failure,
        }


def validate_response(obj: Any) -> SparkVerdict:
    """Strict schema validation -> SparkVerdict(status='ok') or raises ValueError."""
    if not isinstance(obj, dict):
        raise ValueError("response is not an object")
    for key in ("likely_state", "confidence", "recommended_action",
                "wait_seconds", "reason", "supervisor_needed"):
        if key not in obj:
            raise ValueError(f"missing field: {key}")
    likely_state = obj["likely_state"]
    if not isinstance(likely_state, str) or not likely_state.strip():
        raise ValueError("likely_state must be a non-empty string")
    confidence = obj["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("confidence must be a number 0..1")
    if not (0.0 <= float(confidence) <= 1.0):
        raise ValueError("confidence out of range")
    action = obj["recommended_action"]
    if action not in RECOMMENDED_ACTIONS:
        raise ValueError("recommended_action outside enum")
    wait = obj["wait_seconds"]
    if wait is not None and (isinstance(wait, bool) or not isinstance(wait, int)):
        raise ValueError("wait_seconds must be int or None")
    reason = obj["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason must be a non-empty string")
    need = obj["supervisor_needed"]
    if not isinstance(need, bool):
        raise ValueError("supervisor_needed must be a bool")
    return SparkVerdict(
        status="ok",
        likely_state=likely_state.strip()[:120],
        confidence=float(confidence),
        recommended_action=str(action),
        wait_seconds=wait,
        reason=reason[:500],
        supervisor_needed=need,
    )


def _default_transport(req_payload: Dict[str, Any], headers: Dict[str, str],
                       timeout: float) -> tuple:
    req = urllib.request.Request(
        req_payload["_url"], data=req_payload["body"], method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(262144)
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read(4096)
        except Exception:
            pass
        return exc.code, body


class SparkClient:
    """OpenAI-compatible chat-completions client. Token via env only; NEVER
    logged/stored/committed. interpret() never raises."""

    def __init__(self, api_key: Optional[str] = None,
                 base_url: Optional[str] = None,
                 model: Optional[str] = None,
                 timeout_s: Optional[float] = None,
                 transport: Optional[Callable[[Dict[str, Any], Dict[str, str], float], tuple]] = None):
        self.api_key = api_key  # may be None -> PENDING_CREDENTIAL path
        self.base_url = (base_url or os.environ.get("SPARK_BASE_URL")
                         or SPARK_DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.environ.get("SPARK_MODEL") or SPARK_DEFAULT_MODEL
        self.timeout_s = float(timeout_s if timeout_s is not None
                               else os.environ.get("SPARK_TIMEOUT_S", SPARK_TIMEOUT_S))
        self._transport = transport or _default_transport
        self.call_count = 0  # real HTTP attempts issued

    def _resolve_key(self) -> Optional[str]:
        if self.api_key:
            return self.api_key
        key = (os.environ.get("SPARK_API_KEY") or "").strip()
        if key:
            self.api_key = key
            return key
        return None

    @staticmethod
    def _extract_json(content: Any) -> Dict[str, Any]:
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty content")
        text = content.strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            stripped = _FENCE_RE.sub("", text).strip()
            data = json.loads(stripped)  # may raise -> malformed
        if not isinstance(data, dict):
            raise ValueError("content is not a JSON object")
        return data

    def interpret(self, packet: Dict[str, Any]) -> SparkVerdict:
        t0 = time.time()

        def done(status: str, failure: Optional[str]) -> SparkVerdict:
            return SparkVerdict(status=status, failure=failure,
                                latency_s=round(time.time() - t0, 3))

        key = self._resolve_key()
        if not key:
            # PENDING_CREDENTIAL: NO fake call, no state change, honest status
            return done("credential_missing", "pending_credential")

        body = json.dumps({
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content":
                    "You are a process-incident interpreter for a local event "
                    "sentinel. Respond with ONLY a JSON object of this exact "
                    "shape: {\"likely_state\": str, \"confidence\": 0..1, "
                    "\"recommended_action\": one of WAIT|RECHECK|ESCALATE|"
                    "CANCEL_RECOMMENDED|DIAGNOSTIC_REQUIRED, \"wait_seconds\": "
                    "int or null, \"reason\": str, \"supervisor_needed\": bool}. "
                    "You only explain incidents; you never execute anything."},
                {"role": "user", "content": json.dumps(packet, separators=(",", ":"))[:12000]},
            ],
        }).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + str(key),
        }
        try:
            self.call_count += 1
            status, raw = self._transport(
                {"_url": self.base_url + "/v1/chat/completions", "body": body},
                headers, self.timeout_s)
        except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError,
                ConnectionError, OSError) as exc:
            if isinstance(exc, TimeoutError) or "timed out" in str(exc):
                return done("timeout", "timeout")
            return done("error", "transport_error")
        if int(status) in (401, 403):
            return done("credential_missing", f"http_{int(status)}")
        if int(status) != 200:
            return done("error", f"http_{int(status)}")
        try:
            envelope = json.loads(
                raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw)
            choices = envelope["choices"]
            content = choices[0]["message"]["content"]
            parsed = self._extract_json(content)
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError, IndexError,
                TypeError, ValueError):
            return done("invalid", "malformed_response")
        try:
            verdict = validate_response(parsed)
        except ValueError as exc:
            verdict = SparkVerdict(status="invalid", failure=str(exc))
        verdict.latency_s = round(time.time() - t0, 3)
        return verdict


@dataclass
class SparkInterpreter:
    """Thin façade: policy hook + client. No write handles, no state mutation."""

    client: Any
    enabled: bool = True
    call_count: int = 0  # interpret() requests issued (including safe failures)

    def interpret(self, packet: Dict[str, Any]) -> SparkVerdict:
        self.call_count += 1
        try:
            return self.client.interpret(packet)
        except Exception as exc:  # pragma: no cover — belt and braces
            return SparkVerdict(status="error", failure=f"raised:{type(exc).__name__}")