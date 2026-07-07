"""Thin httpx client for the Jira REST API v3.

Mirrors the four calls made by server/hotfix-booking.js:
 - POST /rest/api/3/search/jql   (CM search — deployed / all / by version)
 - GET  /rest/api/3/project/CM/components
 - GET  /rest/api/3/field/customfield_13235/context/{context}/option
"""
from __future__ import annotations

import base64
from typing import Any

import httpx

from .config import Settings

_SEARCH_FIELDS = [
    "summary",
    "status",
    "components",
    "fixVersions",
    "customfield_13235",  # Client Environments
    "customfield_10751",  # TargetDeploymentDate
    "reporter",
]


def _auth_header(settings: Settings) -> dict[str, str]:
    token = base64.b64encode(
        f"{settings.jira_email}:{settings.jira_api_token}".encode()
    ).decode()
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _issue_to_cm(issue: dict) -> dict:
    """Same shape mapping Node uses in fetchDeployedCMs/fetchCMsByVersion."""
    f = issue.get("fields", {}) or {}
    status = f.get("status") or {}
    reporter = f.get("reporter") or {}
    return {
        "key": issue.get("key"),
        "summary": f.get("summary"),
        "status": status.get("name") or "Unknown",
        "components": [c.get("name") for c in (f.get("components") or [])],
        "fixVersions": [v.get("name") for v in (f.get("fixVersions") or [])],
        "clientEnvironments": [c.get("value") for c in (f.get("customfield_13235") or [])],
        "targetDeploymentDate": f.get("customfield_10751") or None,
        "reporter": reporter.get("displayName") or None,
    }


class JiraClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        # Callers may pass an explicit AsyncClient (respx wires against the base URL).
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "JiraClient":
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.jira_base_url,
                headers=_auth_header(self.settings),
                timeout=30.0,
            )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _search_jql(self, jql: str) -> list[dict]:
        assert self._client is not None
        resp = await self._client.post(
            "/rest/api/3/search/jql",
            json={"jql": jql, "fields": _SEARCH_FIELDS, "maxResults": 500},
        )
        resp.raise_for_status()
        issues = (resp.json() or {}).get("issues", []) or []
        return [_issue_to_cm(i) for i in issues]

    async def fetch_deployed_cms(self, deployed_only: bool = True) -> list[dict]:
        status_filter = (
            'AND status in ("Deployment Completed", "Done")' if deployed_only else ""
        )
        jql = f"project = CM {status_filter} AND created >= -100d ORDER BY created DESC"
        return await self._search_jql(jql)

    async def fetch_cms_by_version(self, major: int, minor: int) -> list[dict]:
        jql = f'project = CM AND fixVersion ~ "{major}.{minor}.*" ORDER BY created DESC'
        return await self._search_jql(jql)

    async def fetch_components(self) -> list[dict]:
        assert self._client is not None
        resp = await self._client.get("/rest/api/3/project/CM/components")
        resp.raise_for_status()
        return [{"id": c.get("id"), "name": c.get("name")} for c in (resp.json() or [])]

    async def fetch_client_options(self) -> list[dict]:
        assert self._client is not None
        url = (
            f"/rest/api/3/field/customfield_13235/context/"
            f"{self.settings.client_context_id}/option"
        )
        resp = await self._client.get(url)
        resp.raise_for_status()
        values = (resp.json() or {}).get("values", []) or []
        return [{"id": v.get("id"), "value": v.get("value")} for v in values]

    async def search_users_by_email(self, email: str) -> list[dict]:
        """Query Jira for user records matching an email.

        Jira may return multiple hits: the real user plus auxiliary service
        accounts (accountId prefixed `qm:`, no emailAddress). Callers should
        filter to records whose `emailAddress` matches the query email.
        """
        assert self._client is not None
        resp = await self._client.get(
            "/rest/api/3/user/search", params={"query": email}
        )
        resp.raise_for_status()
        users = resp.json() or []
        return [
            {
                "accountId": u.get("accountId"),
                "displayName": u.get("displayName"),
                "emailAddress": u.get("emailAddress"),
                "active": u.get("active"),
            }
            for u in users
        ]
