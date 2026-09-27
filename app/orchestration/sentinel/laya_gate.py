"""P7b — LAYA AMBIGUITY GATE.

Laya is a side-channel classifier: advisory, non-generative, fail-safe, NOT in
the token-generation path. The gate is consulted ONLY when deterministic
evidence is genuinely ambiguous (default: entering STALL_SUSPECTED) and only
PRE-terminal — a LayaDecision can NEVER change a terminal deterministic fact.

Contract reused as-is (do NOT redesign): POST {base}/v1/systemone with
{"state": <compact text>, "questions": {name: {choice|noul|score ...}}};
200 -> {"model", "answers": {name: {"type","probabilities","confidence",...}}}.
422 carries a precise "detail" (the error messages ARE the documentation).
Auth: Bearer key resolved IN-PROCESS from LAYA_API_KEY env (set by the daemon
spawner); never printed, written, or logged — [REDACTED] everywhere.

ANY failure (timeout, 5xx, 42x, bad shape, network, missing key) ->
LayaDecision(status="unknown") and the deterministic policy continues:
keep watching per current policy; never kill; never wake the supervisor
solely because Laya failed.

Stdlib-only HTTP. This is the AGY in-repo client (no cross-tree imports).
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

DEFAULT_BASE_URL = "https://laya.blablador.fz-juelich.de"
DEFAULT_TIMEOUT_S = 8.0
DEFAULT_RETRIES = 1

CLASSIFICATION_LABELS = {
    "RUNNING": "The process is making working progress and should simply be "
               "observed; nothing needs to happen now.",
    "WAIT": "The process is legitimately quiet (long compute or wait phase); "
            "keep watching with no intervention.",
    "STALLED": "The process looks genuinely stuck; the supervisor should be "
               "informed so a human/model decides what to do.",
    "FAILED": "The process clearly failed based on the observed evidence.",
    "CHECKPOINT": "The process just reached a meaningful checkpoint/phase and "
                  "should be allowed to continue.",
    "COMPLETE_UNCERTAIN": "The evidence honestly cannot classify the process; "
                           "take no action on this answer.",
}

PAYLOAD_KEYS = (
    "state",
    "elapsed_s",
    "quiet_s",
    "cpu_pct",
    "rss_delta_mb",
    "stdout_delta_bytes",
    "stderr_delta_bytes",
    "checkpoint_age_s",
    "deadline_remaining_s",
    "recent_markers",
)

_SECRET_PATTERNS = (
    re.compile(r"glpat-[^\s\"']+", re.I),
    re.compile(r"(?i)bearer\s+[^\s\"',}]+"),
    re.compile(r"(?i)(api[-_]?key|authorization|token|secret)\s*[:=]\s*\S+"),
)

_MAX_STRING = 200  # payload strings never carry raw log blobs


def scrub_secrets(text: str) -> str:
    out = str(text or "")
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


def _clip(value: Any, limit: int = _MAX_STRING) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    return value


def build_laya_payload(record, obs, now: float = 0.0,
                       *, max_bytes: int = 2048, max_markers: int = 5) -> Dict[str, Any]:
    """Compact structured payload ONLY — bounded, never raw repo/log content."""
    checkpoint_age = None
    if record.checkpoint_at is not None:
        checkpoint_age = max(0.0, round(now - record.checkpoint_at, 3))
    rss_delta_mb = None
    if obs.rss_delta_kb is not None:
        rss_delta_mb = round(obs.rss_delta_kb / 1024.0, 3)
    markers = []
    if record.checkpoint:
        markers.append(_clip("checkpoint=" + str(record.checkpoint), 80))
    for path in list(record.last_marker_mtimes.keys())[: max(0, max_markers)]:
        markers.append(_clip(os.path.basename(path), 60))
    markers = markers[:max_markers]

    payload: Dict[str, Any] = {
        "state": record.state,
        "elapsed_s": round(max(0.0, now - record.started_at), 3),
        "quiet_s": round(max(0.0, obs.quiet_s), 3),
        "cpu_pct": None if obs.cpu_pct is None else round(obs.cpu_pct, 1),
        "rss_delta_mb": rss_delta_mb,
        "stdout_delta_bytes": obs.stdout_delta,
        "stderr_delta_bytes": obs.stderr_delta,
        "checkpoint_age_s": checkpoint_age,
        "deadline_remaining_s": None
        if obs.deadline_remaining_s is None
        else round(obs.deadline_remaining_s, 3),
        "recent_markers": markers,
    }
    # bounded by construction; belt-and-braces trim if ever oversized
    while len(json.dumps(payload, separators=(",", ":")).encode()) > max_bytes:
        if payload["recent_markers"]:
            payload["recent_markers"] = payload["recent_markers"][:-1]
        else:
            break
    return payload


def laya_questions() -> Dict[str, Any]:
    """One HTTP call, three questions: classification + 2 noul flags."""
    return {
        "classification": {
            "type": "choice",
            "instructions": "Classify the monitored local process from its "
                            "deterministic observation payload only.",
            "criteria": CLASSIFICATION_LABELS,
        },
        "notify_supervisor": {
            "type": "noul",
            "instructions": "Should the supervisor be told about this process "
                            "state NOW? true: intervention/decision is genuinely "
                            "needed. false: local deterministic watching is "
                            "sufficient (default).",
        },
        "needs_interpretation": {
            "type": "noul",
            "instructions": "Would a lightweight incident interpreter add value "
                            "here (would semantic log interpretation change what "
                            "we do)? true: interpretation would help. false: the "
                            "deterministic evidence is already actionable.",
        },
    }


@dataclass
class LayaDecision:
    """Typed result. status != 'ok' => fail-safe UNKNOWN, policy continues."""

    status: str = "unknown"                       # "ok" | "unknown"
    classification: Optional[str] = None
    notify_supervisor: bool = False
    needs_interpretation: bool = False
    confidence: Optional[float] = None
    probabilities: Dict[str, float] = field(default_factory=dict)
    model: Optional[str] = None
    latency_s: Optional[float] = None
    failure: Optional[str] = None

    @property
    def is_unknown(self) -> bool:
        return self.status != "ok"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "classification": self.classification,
            "notify_supervisor": self.notify_supervisor,
            "needs_interpretation": self.needs_interpretation,
            "confidence": self.confidence,
            "model": self.model,
            "latency_s": self.latency_s,
            "failure": self.failure,
        }


def _default_transport(payload: Dict[str, Any], headers: Dict[str, str],
                       timeout: float) -> tuple:
    req = urllib.request.Request(
        payload["_url"], data=payload["body"], method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(65536)
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read(4096)
        except Exception:
            pass
        return exc.code, body


class LayaClient:
    """Stdlib-only System-1 client. NEVER raises, NEVER logs the key."""

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 timeout_s: Optional[float] = None, retries: Optional[int] = None,
                 transport: Optional[Callable[[Dict[str, Any], Dict[str, str], float], tuple]] = None):
        self.base_url = (base_url or os.environ.get("LAYA_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.timeout_s = float(timeout_s if timeout_s is not None
                               else os.environ.get("LAYA_TIMEOUT_S", DEFAULT_TIMEOUT_S))
        self.retries = int(retries if retries is not None
                           else os.environ.get("LAYA_RETRIES", DEFAULT_RETRIES))
        self._api_key = api_key
        self._transport = transport or _default_transport
        self.call_count = 0  # HTTP attempts issued by this client

    def _resolve_api_key(self) -> Optional[str]:
        if self._api_key:
            return self._api_key
        key = (os.environ.get("LAYA_API_KEY") or "").strip()
        if key:
            self._api_key = key
            return key
        return None

    def decide(self, state: str, questions: Dict[str, Any]) -> Dict[str, Any]:
        """POST /v1/systemone. Returns {'status','answers','failure','latency_s','model'}."""
        t0 = time.time()
        failure: Optional[str] = None
        answers: Optional[Dict[str, Any]] = None
        model = None

        if not self._resolve_api_key():
            return {"status": "unknown", "answers": None,
                    "failure": "missing_credentials", "latency_s": 0.0, "model": None}

        body = json.dumps({
            "state": scrub_secrets(str(state))[:4000],
            "questions": questions,
        }).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Bearer " + str(self._api_key),
        }
        attempts = max(0, self.retries) + 1
        for attempt in range(attempts):
            self.call_count += 1
            try:
                status, raw = self._transport(
                    {"_url": self.base_url + "/v1/systemone", "body": body},
                    headers, self.timeout_s)
            except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError,
                    ConnectionError, OSError):
                failure = "unavailable"
                continue
            if 200 <= int(status) < 300:
                try:
                    data = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw)
                except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
                    failure = "malformed_response"
                    break  # shape problems are not retryable
                if isinstance(data, dict) and isinstance(data.get("answers"), dict):
                    answers = data["answers"]
                    model = data.get("model")
                    failure = None
                    break
                failure = "bad_shape"
                break
            code = int(status)
            if code in (401, 403):
                failure = "auth"
                break
            if code == 429 or code >= 500:
                failure = "rate_limited" if code == 429 else "server_error"
                if code == 429 or code >= 500:
                    continue  # transient classes get the bounded retry
            else:
                failure = f"http_{code}"
                break
        latency = round(time.time() - t0, 3)
        if failure or answers is None:
            return {"status": "unknown", "answers": None,
                    "failure": failure or "unavailable", "latency_s": latency,
                    "model": model}
        return {"status": "ok", "answers": answers, "failure": None,
                "latency_s": latency, "model": model}


def _noul_true(answer: Any, threshold: float) -> Optional[bool]:
    if not isinstance(answer, dict):
        return None
    try:
        if answer.get("noul") is None:
            raise TypeError("no noul field")
        val = float(answer.get("noul"))
    except (TypeError, ValueError):
        probs = answer.get("probabilities")
        if isinstance(probs, dict):
            for key in ("true", "yes", "True"):
                if key in probs:
                    try:
                        val = float(probs[key])
                        break
                    except (TypeError, ValueError):
                        return None
            else:
                return None
        else:
            return None
    return val >= threshold


def parse_laya_response(result: Dict[str, Any], *,
                        notify_threshold: float = 0.5,
                        interpret_threshold: float = 0.5) -> LayaDecision:
    """Typed mapping of the hosted response; ANY problem -> UNKNOWN decision."""
    if not isinstance(result, dict) or result.get("status") != "ok":
        return LayaDecision(status="unknown",
                            failure=str((result or {}).get("failure") or "unknown"),
                            latency_s=(result or {}).get("latency_s"))
    answers = result.get("answers")
    if not isinstance(answers, dict):
        return LayaDecision(status="unknown", failure="bad_shape",
                            latency_s=result.get("latency_s"))
    cls = answers.get("classification") or {}
    choice = cls.get("choice") if isinstance(cls, dict) else None
    if choice not in CLASSIFICATION_LABELS:
        ok_any = any(isinstance(a, dict) and
                     (a.get("choice") in CLASSIFICATION_LABELS or a.get("noul") is not None)
                     for a in answers.values())
        if not ok_any:
            return LayaDecision(status="unknown", failure="bad_shape",
                                latency_s=result.get("latency_s"))
        choice = None
    probs: Dict[str, float] = {}
    if isinstance(cls, dict) and isinstance(cls.get("probabilities"), dict):
        for k, v in cls["probabilities"].items():
            try:
                probs[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
    confidence = None
    if isinstance(cls, dict) and cls.get("confidence") is not None:
        try:
            confidence = float(cls.get("confidence"))
        except (TypeError, ValueError):
            confidence = None
    notify = _noul_true(answers.get("notify_supervisor"), notify_threshold)
    interpret = _noul_true(answers.get("needs_interpretation"), interpret_threshold)
    return LayaDecision(
        status="ok",
        classification=choice,
        notify_supervisor=bool(notify) if notify is not None else False,
        needs_interpretation=bool(interpret) if interpret is not None else False,
        confidence=confidence,
        probabilities=probs,
        model=result.get("model"),
        latency_s=result.get("latency_s"),
        failure=None,
    )


class LayaGate:
    """The P7b gate: compact payload in, typed decision out, fail-safe always."""

    def __init__(self, client: Optional[Any] = None, *, notify_threshold: float = 0.5,
                 interpret_threshold: float = 0.5, max_payload_bytes: int = 2048):
        self.client = client if client is not None else LayaClient()
        self.notify_threshold = notify_threshold
        self.interpret_threshold = interpret_threshold
        self.max_payload_bytes = max_payload_bytes
        self.call_count = 0   # gate-level decision requests (metrics use this)
        self.last_payload_bytes = 0

    def decide(self, payload: Dict[str, Any]) -> LayaDecision:
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        self.last_payload_bytes = len(raw.encode("utf-8"))
        if self.last_payload_bytes > self.max_payload_bytes:
            # compactness is enforced, not hoped for
            trimmed = dict(payload)
            trimmed["recent_markers"] = []
            raw = json.dumps(trimmed, separators=(",", ":"), sort_keys=True)
            self.last_payload_bytes = len(raw.encode("utf-8"))
        self.call_count += 1
        result = self.client.decide(raw, laya_questions())
        return parse_laya_response(
            result,
            notify_threshold=self.notify_threshold,
            interpret_threshold=self.interpret_threshold,
        )