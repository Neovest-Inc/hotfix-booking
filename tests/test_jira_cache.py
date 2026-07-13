"""Tests for the process-global Jira response cache in `jira_client`.

The cache is a small TTL-based memo layer that squashes repeated identical
Jira calls (e.g. the same `fetch_deployed_cms` called from three different
endpoints in a single browser session). These tests lock in:

- Two calls with the same args + within TTL = one Jira request.
- Two calls with different args = two Jira requests.
- Cache respects TTL (expires and refetches).
- `clear_cache()` fully invalidates.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from hotfix_booking import jira_client as jc
from hotfix_booking.config import Settings
from hotfix_booking.jira_client import JiraClient, clear_cache

TEST_BASE = "https://jira.test"


def _settings() -> Settings:
    return Settings(
        jira_base_url=TEST_BASE,
        jira_email="t@example.com",
        jira_api_token="s",
        bookings_file="/tmp/b.json",  # not used in these tests
        client_context_id=14042,
        port=1,
        booking_retention_days=180,
        admin_emails=frozenset(),
        session_secret_key="test-secret",
    )


@pytest.fixture
def settings() -> Settings:
    from hotfix_booking.config import reset_settings_for_tests
    s = _settings()
    reset_settings_for_tests(s)
    return s


@pytest.mark.asyncio
async def test_repeated_fetch_deployed_cms_hits_jira_once(settings: Settings) -> None:
    """Two calls with the same args within the TTL should share one Jira request."""
    with respx.mock(base_url=TEST_BASE) as mock:
        route = mock.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json={"issues": [], "isLast": True})
        )
        async with JiraClient(settings) as jira:
            await jira.fetch_deployed_cms(deployed_only=False)
            await jira.fetch_deployed_cms(deployed_only=False)
        assert route.call_count == 1


@pytest.mark.asyncio
async def test_different_args_do_not_share_cache_entry(settings: Settings) -> None:
    """deployed_only=True and deployed_only=False are separate cache keys."""
    with respx.mock(base_url=TEST_BASE) as mock:
        route = mock.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json={"issues": [], "isLast": True})
        )
        async with JiraClient(settings) as jira:
            await jira.fetch_deployed_cms(deployed_only=True)
            await jira.fetch_deployed_cms(deployed_only=False)
        assert route.call_count == 2


@pytest.mark.asyncio
async def test_fetch_cms_by_version_caches_per_release(settings: Settings) -> None:
    with respx.mock(base_url=TEST_BASE) as mock:
        route = mock.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json={"issues": [], "isLast": True})
        )
        async with JiraClient(settings) as jira:
            await jira.fetch_cms_by_version(9, 97)  # miss
            await jira.fetch_cms_by_version(9, 97)  # hit
            await jira.fetch_cms_by_version(9, 98)  # miss (different key)
        assert route.call_count == 2


@pytest.mark.asyncio
async def test_fetch_components_caches(settings: Settings) -> None:
    with respx.mock(base_url=TEST_BASE) as mock:
        route = mock.get("/rest/api/3/project/CM/components").mock(
            return_value=httpx.Response(200, json=[])
        )
        async with JiraClient(settings) as jira:
            await jira.fetch_components()
            await jira.fetch_components()
        assert route.call_count == 1


@pytest.mark.asyncio
async def test_fetch_client_options_caches(settings: Settings) -> None:
    with respx.mock(base_url=TEST_BASE) as mock:
        route = mock.get(
            f"/rest/api/3/field/customfield_13235/context/{settings.client_context_id}/option"
        ).mock(return_value=httpx.Response(200, json={"values": []}))
        async with JiraClient(settings) as jira:
            await jira.fetch_client_options()
            await jira.fetch_client_options()
        assert route.call_count == 1


@pytest.mark.asyncio
async def test_clear_cache_forces_refetch(settings: Settings) -> None:
    """Explicit clear_cache() invalidates every entry, next call goes to Jira."""
    with respx.mock(base_url=TEST_BASE) as mock:
        route = mock.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json={"issues": [], "isLast": True})
        )
        async with JiraClient(settings) as jira:
            await jira.fetch_deployed_cms(deployed_only=True)
            clear_cache()
            await jira.fetch_deployed_cms(deployed_only=True)
        assert route.call_count == 2


@pytest.mark.asyncio
async def test_ttl_expiry_forces_refetch(settings: Settings, monkeypatch) -> None:
    """After the TTL elapses, the cached value is dropped and Jira is hit again."""
    fake_now = [1000.0]
    monkeypatch.setattr(jc.time, "monotonic", lambda: fake_now[0])

    with respx.mock(base_url=TEST_BASE) as mock:
        route = mock.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json={"issues": [], "isLast": True})
        )
        async with JiraClient(settings) as jira:
            await jira.fetch_deployed_cms(deployed_only=True)  # miss at t=1000
            fake_now[0] += 5.0                                   # t=1005 (< 10s TTL)
            await jira.fetch_deployed_cms(deployed_only=True)  # hit
            fake_now[0] += 20.0                                  # t=1025 (> 10s TTL)
            await jira.fetch_deployed_cms(deployed_only=True)  # miss again
        assert route.call_count == 2
