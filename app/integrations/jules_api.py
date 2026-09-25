from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


DEFAULT_BASE_URL = "https://jules.googleapis.com/v1alpha"


class JulesAPIError(RuntimeError):
    pass


class JulesClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        self.api_key = api_key or os.environ.get(
            "JULES_API_KEY"
        )

        if not self.api_key:
            raise JulesAPIError(
                "JULES_API_KEY is not configured"
            )

        self.base_url = (
            base_url
            or os.environ.get(
                "JULES_BASE_URL",
                DEFAULT_BASE_URL,
            )
        ).rstrip("/")

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: int = 60,
    ) -> dict[str, Any]:

        url = self.base_url + path

        if query:
            cleaned = {
                key: value
                for key, value in query.items()
                if value is not None
            }

            if cleaned:
                url += "?" + urllib.parse.urlencode(
                    cleaned
                )

        body = None

        headers = {
            "X-Goog-Api-Key": self.api_key,
            "Accept": "application/json",
        }

        if payload is not None:
            body = json.dumps(payload).encode(
                "utf-8"
            )
            headers["Content-Type"] = (
                "application/json"
            )

        request = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method=method,
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=timeout,
            ) as response:
                raw = response.read()

        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(
                errors="replace"
            )

            raise JulesAPIError(
                f"Jules HTTP {exc.code}: {detail}"
            ) from exc

        except urllib.error.URLError as exc:
            raise JulesAPIError(
                f"Jules connection error: {exc}"
            ) from exc

        if not raw:
            return {}

        return json.loads(raw)

    # ------------------------------------------------------
    # Sources
    # ------------------------------------------------------

    def list_sources(
        self,
    ) -> list[dict[str, Any]]:

        items: list[dict[str, Any]] = []
        token = None

        while True:
            result = self._request(
                "GET",
                "/sources",
                query={
                    "pageSize": 100,
                    "pageToken": token,
                },
            )

            items.extend(
                result.get("sources", [])
            )

            token = result.get(
                "nextPageToken"
            )

            if not token:
                return items

    def get_source(
        self,
        source_name: str,
    ) -> dict[str, Any]:

        if not source_name.startswith(
            "sources/"
        ):
            raise JulesAPIError(
                f"Invalid source name: {source_name}"
            )

        return self._request(
            "GET",
            "/" + source_name,
        )

    def find_source(
        self,
        owner: str,
        repo: str,
    ) -> dict[str, Any]:

        owner_lower = owner.lower()
        repo_lower = repo.lower()

        for source in self.list_sources():
            gh = source.get(
                "githubRepo",
                {},
            )

            if (
                str(
                    gh.get("owner", "")
                ).lower()
                == owner_lower
                and str(
                    gh.get("repo", "")
                ).lower()
                == repo_lower
            ):
                return source

        raise JulesAPIError(
            "No connected Jules source for "
            f"{owner}/{repo}"
        )

    # ------------------------------------------------------
    # Sessions
    # ------------------------------------------------------

    def create_session(
        self,
        *,
        source: str,
        branch: str,
        prompt: str,
        title: str | None = None,
        require_plan_approval: bool = False,
        auto_create_pr: bool = False,
    ) -> dict[str, Any]:

        payload: dict[str, Any] = {
            "prompt": prompt,
            "sourceContext": {
                "source": source,
                "githubRepoContext": {
                    "startingBranch": branch,
                },
            },
            "requirePlanApproval": (
                require_plan_approval
            ),
        }

        if title:
            payload["title"] = title

        if auto_create_pr:
            payload["automationMode"] = (
                "AUTO_CREATE_PR"
            )

        return self._request(
            "POST",
            "/sessions",
            payload=payload,
        )

    def get_session(
        self,
        session_id: str,
    ) -> dict[str, Any]:

        return self._request(
            "GET",
            f"/sessions/{session_id}",
        )

    def approve_plan(
        self,
        session_id: str,
    ) -> None:

        self._request(
            "POST",
            f"/sessions/{session_id}:approvePlan",
            payload={},
        )

    def send_message(
        self,
        session_id: str,
        prompt: str,
    ) -> None:

        self._request(
            "POST",
            f"/sessions/{session_id}:sendMessage",
            payload={
                "prompt": prompt,
            },
        )

    # ------------------------------------------------------
    # Activities
    # ------------------------------------------------------

    def list_activities(
        self,
        session_id: str,
        *,
        since: str | None = None,
    ) -> list[dict[str, Any]]:

        items: list[dict[str, Any]] = []
        token = None

        while True:
            result = self._request(
                "GET",
                (
                    f"/sessions/{session_id}"
                    "/activities"
                ),
                query={
                    "pageSize": 100,
                    "pageToken": token,
                    "createTime": since,
                },
            )

            items.extend(
                result.get(
                    "activities",
                    [],
                )
            )

            token = result.get(
                "nextPageToken"
            )

            if not token:
                return items
