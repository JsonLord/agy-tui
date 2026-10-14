from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from urllib.parse import quote

from .policy import AutomakerError

HEALTH_PATH = "/api/health"
PROJECTS_OVERVIEW_PATH = "/api/projects/overview"
FEATURES_LIST_PATH = "/api/features/list"
FEATURES_GET_PATH = "/api/features/get"
FEATURES_CREATE_PATH = "/api/features/create"
FEATURES_UPDATE_PATH = "/api/features/update"
AGENT_START_PATH = "/api/agent/start"
AGENT_STOP_PATH = "/api/agent/stop"
RUNNING_AGENTS_PATH = "/api/running-agents"
AUTO_MODE_STATUS_PATH = "/api/auto-mode/status"
BACKLOG_PLAN_STATUS_PATH = "/api/backlog-plan/status"

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RESPONSE_BYTES = 2_000_000
READ_CONNECT_RETRIES = 1
RETRY_DELAY_SECONDS = 0.25


class AutomakerClient:
    """Typed, bounded client over fixed Automaker endpoints.

    No public/generic request method, no caller-supplied paths or URLs.
    Mutating calls never retry; read calls retry once on connect-level failure.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ):
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes
        # Loopback only: never route integration traffic through system proxies.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _request(
        self, method: str, path: str, *, body: dict | None = None, read: bool = False
    ) -> dict:
        attempts = READ_CONNECT_RETRIES + 1 if read else 1
        last: AutomakerError | None = None
        for attempt in range(attempts):
            try:
                return self._execute(method, path, body)
            except AutomakerError as error:
                transient = read and error.code in {
                    "AUTOMAKER_UNAVAILABLE",
                    "AUTOMAKER_TIMEOUT",
                }
                if not transient or attempt + 1 >= attempts:
                    raise
                last = error
                time.sleep(RETRY_DELAY_SECONDS)
        raise last or AutomakerError("AUTOMAKER_UNAVAILABLE")

    def _execute(self, method: str, path: str, body: dict | None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Accept": "application/json",
            # Verified API-key header at Automaker SHA 5888d2e (lib/auth.ts).
            "X-API-Key": self._api_key,
            # Contract header for Bearer-capable future versions; ignored today.
            "Authorization": "Bearer " + self._api_key,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self._base + path, data=data, headers=headers, method=method
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                return self._parse(response)
        except urllib.error.HTTPError as error:
            self._drain(error)
            raise AutomakerError(self._status_code(error.code)) from None
        except TimeoutError:
            raise AutomakerError("AUTOMAKER_TIMEOUT") from None
        except urllib.error.URLError as error:
            reason = getattr(error, "reason", None)
            code = (
                "AUTOMAKER_TIMEOUT"
                if isinstance(reason, (TimeoutError, socket.timeout))
                else "AUTOMAKER_UNAVAILABLE"
            )
            raise AutomakerError(code) from None
        except OSError:
            raise AutomakerError("AUTOMAKER_UNAVAILABLE") from None

    def _parse(self, response) -> dict:
        if response.status not in (200, 201, 202, 204):
            raise AutomakerError(self._status_code(response.status))
        content_type = (response.headers.get("Content-Type") or "").casefold()
        if "application/json" not in content_type:
            raise AutomakerError("AUTOMAKER_BAD_RESPONSE")
        payload = response.read(self._max_response_bytes + 1)
        if len(payload) > self._max_response_bytes:
            raise AutomakerError("AUTOMAKER_BAD_RESPONSE")
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise AutomakerError("AUTOMAKER_BAD_RESPONSE") from None
        if not isinstance(parsed, dict):
            raise AutomakerError("AUTOMAKER_BAD_RESPONSE")
        return parsed

    @staticmethod
    def _status_code(status: int) -> str:
        if status in (401, 403):
            return "AUTOMAKER_AUTH_FAILED"
        if status == 404:
            return "AUTOMAKER_NOT_FOUND"
        if status == 409:
            return "AUTOMAKER_CONFLICT"
        if status in (400, 415, 422):
            return "AUTOMAKER_REQUEST_REJECTED"
        if 500 <= status <= 599:
            return "AUTOMAKER_UNAVAILABLE"
        return "AUTOMAKER_BAD_RESPONSE"

    @staticmethod
    def _drain(error: urllib.error.HTTPError) -> None:
        # Discard the server error body; never surface raw server details.
        try:
            error.read(65_536)
        except OSError:
            pass

    def health(self) -> dict:
        return self._request("GET", HEALTH_PATH, read=True)

    def projects_overview(self) -> dict:
        return self._request("GET", PROJECTS_OVERVIEW_PATH, read=True)

    def running_agents(self) -> dict:
        return self._request("GET", RUNNING_AGENTS_PATH, read=True)

    def backlog_status(self, project_path: str = "") -> dict:
        path = BACKLOG_PLAN_STATUS_PATH
        if project_path:
            path = f"{path}?projectPath={quote(project_path, safe='')}"
        return self._request("GET", path, read=True)

    def features_list(self, project_path: str) -> dict:
        # POST verb, semantically read-only: orphan detection is backgrounded.
        return self._request(
            "POST", FEATURES_LIST_PATH, body={"projectPath": project_path}, read=True
        )

    def feature_get(self, project_path: str, feature_id: str) -> dict:
        return self._request(
            "POST",
            FEATURES_GET_PATH,
            body={"projectPath": project_path, "featureId": feature_id},
            read=True,
        )

    def auto_mode_status(self, project_path: str = "") -> dict:
        body: dict[str, str] = {}
        if project_path:
            body["projectPath"] = project_path
        # POST verb, semantically read-only.
        return self._request("POST", AUTO_MODE_STATUS_PATH, body=body, read=True)

    def feature_create(self, project_path: str, feature: dict) -> dict:
        return self._request(
            "POST",
            FEATURES_CREATE_PATH,
            body={"projectPath": project_path, "feature": feature},
        )

    def feature_update(self, project_path: str, feature_id: str, updates: dict) -> dict:
        return self._request(
            "POST",
            FEATURES_UPDATE_PATH,
            body={
                "projectPath": project_path,
                "featureId": feature_id,
                "updates": updates,
            },
        )

    def agent_start(self, session_id: str, working_directory: str = "") -> dict:
        body: dict[str, str] = {"sessionId": session_id}
        if working_directory:
            body["workingDirectory"] = working_directory
        return self._request("POST", AGENT_START_PATH, body=body)

    def agent_stop(self, session_id: str) -> dict:
        return self._request("POST", AGENT_STOP_PATH, body={"sessionId": session_id})
