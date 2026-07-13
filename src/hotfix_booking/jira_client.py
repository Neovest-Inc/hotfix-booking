"""Thin httpx client for the Jira REST API v3.

Mirrors the four calls made by server/hotfix-booking.js:
 - POST /rest/api/3/search/jql   (CM search — deployed / all / by version)
 - GET  /rest/api/3/project/CM/components
 - GET  /rest/api/3/field/customfield_13235/context/{context}/option
"""
from __future__ import annotations

import base64
import logging
import time
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger(__name__)

# Jira's `/rest/api/3/search/jql` endpoint caps each page at ~100 issues
# regardless of `maxResults` and paginates via `nextPageToken`. Safety cap
# on total pages guards against runaway loops from bad tokens: 15 pages ×
# 100 issues = 1500 CMs, well above the ~800 CMs expected at 4 months of
# coverage (~200 CMs/month) — but low enough to fail fast if something is
# wrong. Bump if the CM volume genuinely grows past this.
_MAX_PAGES = 15
_PAGE_SIZE = 100

# ---------------------------------------------------------------------------
# In-memory response cache
# ---------------------------------------------------------------------------
# All four `fetch_*` methods below are wrapped in a tiny process-global TTL
# cache. Rationale:
#  - Every user page-load / auto-refresh cycle hits several endpoints, most
#    of which independently call the same underlying Jira query (e.g.
#    fetch_deployed_cms is invoked by /next-version, /client-versions, and
#    /history). Without caching those are 3+ Jira round-trips (~200ms each)
#    for what is semantically ONE fetch.
#  - Jira data is only mutated externally (by the CM workflow), always with
#    minutes of lag anyway, so a 30-second TTL is safe: the freshness window
#    that matters for the user is "second-to-second consistency across the
#    tabs I have open right now".
#
# Cached values are RETURNED BY REFERENCE. Callers must treat them as
# read-only — mutating a cached list will leak to future readers. Every
# caller in routes.py today reads only; keep it that way.
#
# Tests reset the cache between cases via the autouse `_reset_jira_cache`
# fixture in tests/conftest.py — otherwise a mocked response from test A
# would satisfy the cache for test B and hide real behavior.
_CACHE_TTL_SECONDS = 10.0
_cache: dict[tuple, tuple[float, Any]] = {}


def _cache_get(key: tuple) -> Any | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    stored_at, value = entry
    if time.monotonic() - stored_at > _CACHE_TTL_SECONDS:
        # Expired — drop and force a refetch.
        _cache.pop(key, None)
        return None
    return value


def _cache_set(key: tuple, value: Any) -> None:
    _cache[key] = (time.monotonic(), value)


def clear_cache() -> None:
    """Empty the Jira response cache. Called from tests between cases; safe
    to call at any time in production (next call refetches from Jira)."""
    _cache.clear()

_SEARCH_FIELDS = [
    "summary",
    "status",
    "components",
    "fixVersions",
    "customfield_13235",  # Client Environments
    "customfield_10751",  # TargetDeploymentDate
    "reporter",
    "created",
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


def make_httpx_client(settings: Settings) -> httpx.AsyncClient:
    """Build an httpx.AsyncClient pre-configured for Jira calls.

    Callers must either manage the client's lifetime themselves (call
    `aclose()` on shutdown) OR pass the resulting client to `JiraClient(...)`
    which will honor a caller-owned client and NOT close it.

    Used from two places:
    - `app.py` lifespan: creates ONE client for the whole app lifetime,
      reused across every request (avoids per-request TLS handshake, saves
      ~50-100ms per Jira call).
    - `JiraClient.__aenter__`: builds a per-instance client when none was
      passed in (mainly for standalone / tool use — routes.py always uses
      the persistent app-level client).

    Resilience: the transport retries twice on low-level connection errors
    (dropped TCP, DNS blip, connection reset). Without this, a stale pooled
    connection between the app and Jira could surface as a mysterious 500
    to the user even though a simple retry would succeed. Retries only
    apply to connection errors — 4xx/5xx responses from Jira are still
    returned as-is so the app's own error handling stays in charge.
    """
    return httpx.AsyncClient(
        base_url=settings.jira_base_url,
        headers=_auth_header(settings),
        timeout=30.0,
        transport=httpx.AsyncHTTPTransport(retries=2),
    )


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
        # Jira's issue creation timestamp — used as `bookedAt` when the CM
        # is fed into the dependency graph as a pseudo-booking, so it sorts
        # correctly relative to real bookings on the same release line.
        "createdAt": f.get("created") or None,
    }


class JiraClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        # Callers may pass an explicit AsyncClient (respx wires against the base URL).
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "JiraClient":
        if self._client is None:
            self._client = make_httpx_client(self.settings)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _search_jql(self, jql: str) -> list[dict]:
        """Search Jira for issues matching `jql`, paginating until exhausted.

        Jira's enhanced search endpoint caps each page at ~100 issues and
        signals continuation via `nextPageToken` + `isLast: false`. We
        accumulate all pages up to `_MAX_PAGES` (safety cap). A missing
        `isLast` field is treated as terminal — this keeps hermetic
        synthetic fixtures (which don't include the field) working as
        single-page responses.
        """
        assert self._client is not None
        all_issues: list[dict] = []
        next_page_token: str | None = None
        for page_num in range(1, _MAX_PAGES + 1):
            payload: dict[str, Any] = {
                "jql": jql,
                "fields": _SEARCH_FIELDS,
                "maxResults": _PAGE_SIZE,
            }
            if next_page_token is not None:
                payload["nextPageToken"] = next_page_token
            resp = await self._client.post("/rest/api/3/search/jql", json=payload)
            resp.raise_for_status()
            body = resp.json() or {}
            all_issues.extend(body.get("issues") or [])
            # Terminate on explicit isLast=True, or on missing isLast
            # (single-page fixtures / older API responses).
            if body.get("isLast", True):
                break
            next_page_token = body.get("nextPageToken")
            if not next_page_token:
                # Defensive: isLast=false but no token means we can't page
                # further — treat as terminal to avoid an infinite loop.
                break
        else:
            # for/else: ran the full range without breaking → hit the cap.
            log.warning(
                "Jira search-jql pagination hit max pages (%d). "
                "Some issues may be truncated. JQL: %s",
                _MAX_PAGES,
                jql,
            )
        return [_issue_to_cm(i) for i in all_issues]

    async def fetch_deployed_cms(self, deployed_only: bool = True) -> list[dict]:
        cache_key = ("fetch_deployed_cms", deployed_only)
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached
        status_filter = (
            'AND status in ("Deployment Completed", "Done")' if deployed_only else ""
        )
        # 120-day window covers 4 months (business requirement: some clients
        # on a 3-month release cadence, may be delayed). At ~200 CMs/month
        # that's ~800 CMs, comfortably within the pagination safety cap.
        jql = f"project = CM {status_filter} AND created >= -120d ORDER BY created DESC"
        result = await self._search_jql(jql)
        _cache_set(cache_key, result)
        return result

    async def fetch_cms_by_version(self, major: int, minor: int) -> list[dict]:
        cache_key = ("fetch_cms_by_version", major, minor)
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached
        jql = f'project = CM AND fixVersion ~ "{major}.{minor}.*" ORDER BY created DESC'
        result = await self._search_jql(jql)
        _cache_set(cache_key, result)
        return result

    async def fetch_components(self) -> list[dict]:
        cache_key = ("fetch_components",)
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached
        assert self._client is not None
        resp = await self._client.get("/rest/api/3/project/CM/components")
        resp.raise_for_status()
        result = [{"id": c.get("id"), "name": c.get("name")} for c in (resp.json() or [])]
        _cache_set(cache_key, result)
        return result

    async def fetch_client_options(self) -> list[dict]:
        cache_key = ("fetch_client_options", self.settings.client_context_id)
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached
        assert self._client is not None
        url = (
            f"/rest/api/3/field/customfield_13235/context/"
            f"{self.settings.client_context_id}/option"
        )
        resp = await self._client.get(url)
        resp.raise_for_status()
        values = (resp.json() or {}).get("values", []) or []
        result = [{"id": v.get("id"), "value": v.get("value")} for v in values]
        _cache_set(cache_key, result)
        return result
