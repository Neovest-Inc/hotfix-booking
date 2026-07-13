"""Tests for the `?fresh=1` cache-bypass query param on read endpoints.

`fresh=1` clears the in-memory Jira response cache before serving the
request. Used by the front-end Refresh buttons and the Book auto-refresh
loop so users always see up-to-date data on those paths.
"""
from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.conftest import load_fixture


class TestFreshBypass:
    def test_two_calls_without_fresh_hit_jira_once(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        """Baseline: repeat call within TTL is served from cache (1 Jira hit)."""
        route = mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json=load_fixture("search_all.json"))
        )
        r1 = client.get("/api/hotfix-booking/deployed-cms")
        r2 = client.get("/api/hotfix-booking/deployed-cms")
        assert r1.status_code == 200 and r2.status_code == 200
        assert route.call_count == 1

    def test_fresh_query_param_bypasses_cache(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        """`?fresh=1` on the second call clears the cache first, forcing a Jira hit."""
        route = mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json=load_fixture("search_all.json"))
        )
        client.get("/api/hotfix-booking/deployed-cms")
        client.get("/api/hotfix-booking/deployed-cms?fresh=1")
        assert route.call_count == 2

    def test_fresh_on_history_bypasses_cache(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        route = mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json={"issues": [], "isLast": True})
        )
        client.get("/api/hotfix-booking/history?major=9&minor=97")
        client.get("/api/hotfix-booking/history?major=9&minor=97&fresh=1")
        # First call: 2 parallel Jira calls (fetch_deployed_cms + fetch_cms_by_version)
        # Second call: fresh=1 clears the cache → both calls happen again = 2 more
        assert route.call_count == 4

    def test_fresh_on_next_version_bypasses_cache(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        route = mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json=load_fixture("search_deployed.json"))
        )
        client.get("/api/hotfix-booking/next-version")
        client.get("/api/hotfix-booking/next-version?fresh=1")
        assert route.call_count == 2

    def test_fresh_on_client_versions_bypasses_cache(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        route = mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json=load_fixture("search_all.json"))
        )
        client.get("/api/hotfix-booking/client-versions")
        client.get("/api/hotfix-booking/client-versions?fresh=1")
        assert route.call_count == 2

    def test_fresh_on_field_options_bypasses_cache(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        components = mock_jira.get("/rest/api/3/project/CM/components").mock(
            return_value=httpx.Response(200, json=load_fixture("cm_components.json"))
        )
        clients_route = mock_jira.get(
            "/rest/api/3/field/customfield_13235/context/14042/option"
        ).mock(return_value=httpx.Response(200, json=load_fixture("client_options.json")))
        client.get("/api/hotfix-booking/field-options")
        client.get("/api/hotfix-booking/field-options?fresh=1")
        assert components.call_count == 2
        assert clients_route.call_count == 2
