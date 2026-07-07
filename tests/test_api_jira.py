"""Tests for endpoints that hit Jira: /field-options, /deployed-cms, /next-version,
/client-versions, /history. Uses respx to mock httpx calls."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.conftest import (
    TEST_JIRA_BASE,
    load_fixture,
    read_bookings,
    write_bookings,
)


# ---------------------------------------------------------------------------
# /field-options
# ---------------------------------------------------------------------------
class TestFieldOptions:
    def test_success(self, client: TestClient, mock_jira: respx.MockRouter) -> None:
        mock_jira.get("/rest/api/3/project/CM/components").mock(
            return_value=httpx.Response(200, json=load_fixture("cm_components.json"))
        )
        mock_jira.get(
            "/rest/api/3/field/customfield_13235/context/14042/option"
        ).mock(return_value=httpx.Response(200, json=load_fixture("client_options.json")))

        r = client.get("/api/hotfix-booking/field-options")
        assert r.status_code == 200
        body = r.json()
        assert body["components"] == [
            {"id": "10100", "name": "Alerts"},
            {"id": "10101", "name": "DV_Web"},
            {"id": "10102", "name": "Analytics"},
        ]
        assert body["clients"] == [
            {"id": "20001", "value": "CL001 - Fortress"},
            {"id": "20002", "value": "CL002 - Convex"},
            {"id": "20003", "value": "CL003 - TPG"},
        ]

    def test_jira_500_yields_500(self, client: TestClient, mock_jira: respx.MockRouter) -> None:
        mock_jira.get("/rest/api/3/project/CM/components").mock(
            return_value=httpx.Response(500)
        )
        mock_jira.get(
            "/rest/api/3/field/customfield_13235/context/14042/option"
        ).mock(return_value=httpx.Response(200, json={"values": []}))

        r = client.get("/api/hotfix-booking/field-options")
        assert r.status_code == 500
        assert r.json() == {"error": "Failed to fetch field options"}


# ---------------------------------------------------------------------------
# /deployed-cms
# ---------------------------------------------------------------------------
class TestDeployedCms:
    def test_success_returns_mapped_cms(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json=load_fixture("search_all.json"))
        )
        r = client.get("/api/hotfix-booking/deployed-cms")
        assert r.status_code == 200
        cms = r.json()["cms"]
        assert len(cms) == 4
        first = cms[0]
        assert first["key"] == "CM-1001"
        assert first["status"] == "Deployment Completed"
        assert first["components"] == ["Alerts", "DV_Web"]
        assert first["fixVersions"] == ["9.94.20"]
        assert first["clientEnvironments"] == ["CL001 - Fortress", "CL002 - Convex"]
        assert first["targetDeploymentDate"] == "2026-05-15"
        assert first["reporter"] == "Alice"

    def test_jira_error_yields_500(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        mock_jira.post("/rest/api/3/search/jql").mock(return_value=httpx.Response(503))
        r = client.get("/api/hotfix-booking/deployed-cms")
        assert r.status_code == 500
        assert r.json() == {"error": "Failed to fetch deployed CMs"}


# ---------------------------------------------------------------------------
# /next-version
# ---------------------------------------------------------------------------
class TestNextVersion:
    def test_uses_max_across_deployed_and_bookings(
        self, client: TestClient, mock_jira: respx.MockRouter, bookings_file: Path
    ) -> None:
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json=load_fixture("search_deployed.json"))
        )
        # Booking above the highest deployed (9.94.22) → should become currentHighest
        write_bookings(bookings_file, [
            {"id": "HB-1", "version": "9.94.30", "components": ["A"],
             "clientEnvironments": ["CL"], "bookedBy": "U", "bookedAt": "T",
             "status": "booked"}
        ])

        r = client.get("/api/hotfix-booking/next-version")
        assert r.status_code == 200
        assert r.json() == {
            "currentHighest": "9.94.30",
            "nextVersion": "9.94.31",
            "baseVersion": "9.94.0",
        }

    def test_uses_deployed_when_no_bookings(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json=load_fixture("search_deployed.json"))
        )
        r = client.get("/api/hotfix-booking/next-version")
        assert r.status_code == 200
        body = r.json()
        assert body["currentHighest"] == "9.94.22"
        assert body["nextVersion"] == "9.94.23"

    def test_auto_cleanup_removes_deployed_bookings(
        self, client: TestClient, mock_jira: respx.MockRouter, bookings_file: Path
    ) -> None:
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json=load_fixture("search_deployed.json"))
        )
        # 9.94.20 IS in the deployed fixture — should be cleaned up.
        # 9.94.30 is not — should be kept.
        write_bookings(bookings_file, [
            {"id": "HB-DEPLOYED", "version": "9.94.20", "components": ["A"],
             "clientEnvironments": ["CL"], "bookedBy": "U", "bookedAt": "T", "status": "booked"},
            {"id": "HB-STILL-PENDING", "version": "9.94.30", "components": ["A"],
             "clientEnvironments": ["CL"], "bookedBy": "U", "bookedAt": "T", "status": "booked"},
        ])

        r = client.get("/api/hotfix-booking/next-version")
        assert r.status_code == 200
        remaining = read_bookings(bookings_file)
        assert [b["id"] for b in remaining] == ["HB-STILL-PENDING"]

    def test_no_versions_returns_null_with_error(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json={"issues": []})
        )
        r = client.get("/api/hotfix-booking/next-version")
        assert r.status_code == 200
        assert r.json() == {"nextVersion": None, "error": "No deployed versions found."}

    def test_jira_error_yields_500(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        mock_jira.post("/rest/api/3/search/jql").mock(return_value=httpx.Response(500))
        r = client.get("/api/hotfix-booking/next-version")
        assert r.status_code == 500
        assert r.json() == {"error": "Failed to calculate next version"}


# ---------------------------------------------------------------------------
# /client-versions
# ---------------------------------------------------------------------------
class TestClientVersions:
    def test_builds_matrix_from_deployed(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json=load_fixture("search_deployed.json"))
        )
        r = client.get("/api/hotfix-booking/client-versions")
        assert r.status_code == 200
        body = r.json()

        # CL001 only got 9.94.20 (from CM-1001)
        assert body["matrix"]["CL001 - Fortress"]["Alerts"]["version"] == "9.94.20"
        assert body["matrix"]["CL001 - Fortress"]["Alerts"]["cmKey"] == "CM-1001"

        # CL002 got both 9.94.20 (CM-1001) and 9.94.22 (CM-1002) — should keep .22
        assert body["matrix"]["CL002 - Convex"]["Alerts"]["version"] == "9.94.22"
        assert body["matrix"]["CL002 - Convex"]["Alerts"]["cmKey"] == "CM-1002"

        # CL003 got only Analytics 9.92.85 (from CM-1003)
        assert body["matrix"]["CL003 - TPG"]["Analytics"]["version"] == "9.92.85"

        # Sorted metadata
        assert body["components"] == ["Alerts", "Analytics", "DV_Web"]
        assert body["clients"] == ["CL001 - Fortress", "CL002 - Convex", "CL003 - TPG"]

    def test_jira_error_yields_500(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        mock_jira.post("/rest/api/3/search/jql").mock(return_value=httpx.Response(502))
        r = client.get("/api/hotfix-booking/client-versions")
        assert r.status_code == 500
        assert r.json() == {"error": "Failed to fetch client versions"}


# ---------------------------------------------------------------------------
# /history
# ---------------------------------------------------------------------------
class TestHistory:
    def _wire_history_calls(self, mock_jira: respx.MockRouter) -> None:
        """History makes two search-jql POSTs. Route by JQL body content."""
        def _responder(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content or b"{}")
            jql = body.get("jql", "")
            if "fixVersion" in jql:
                return httpx.Response(
                    200, json=load_fixture("search_by_version_9_92.json")
                )
            return httpx.Response(200, json=load_fixture("search_all.json"))

        mock_jira.post("/rest/api/3/search/jql").mock(side_effect=_responder)

    def test_no_query_uses_current_minor(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        self._wire_history_calls(mock_jira)
        r = client.get("/api/hotfix-booking/history")
        assert r.status_code == 200
        body = r.json()
        # search_all fixture has 9.94.x → currentMinor = 94
        assert body["currentMinor"] == 94
        assert body["targetMinor"] == 94
        assert body["jiraBaseUrl"] == TEST_JIRA_BASE
        # 5 minor versions listed, latest first
        assert [m["minor"] for m in body["minorVersions"]] == [94, 93, 92, 91, 90]
        assert body["minorVersions"][0] == {"major": 9, "minor": 94, "label": "9.94.x"}

    def test_minor_query_overrides(
        self, client: TestClient, mock_jira: respx.MockRouter, bookings_file: Path
    ) -> None:
        self._wire_history_calls(mock_jira)
        write_bookings(bookings_file, [
            {"id": "HB-1", "version": "9.92.99", "components": ["Alerts"],
             "clientEnvironments": ["CL001"], "bookedBy": "Alice",
             "bookedAt": "2026-03-01T00:00:00Z", "status": "booked"},
        ])
        r = client.get("/api/hotfix-booking/history?minor=92")
        assert r.status_code == 200
        body = r.json()
        assert body["targetMinor"] == 92
        versions = [h["version"] for h in body["hotfixes"]]
        # Sorted desc, includes deployed (from search_by_version fixture) + booking
        assert versions == ["9.92.99", "9.92.86", "9.92.85"]
        booked = [h for h in body["hotfixes"] if h["type"] == "booked"]
        assert len(booked) == 1
        assert booked[0]["reporter"] == "Alice"

    def test_booking_hidden_when_version_deployed(
        self, client: TestClient, mock_jira: respx.MockRouter, bookings_file: Path
    ) -> None:
        self._wire_history_calls(mock_jira)
        # 9.92.85 is in the deployed fixture
        write_bookings(bookings_file, [
            {"id": "HB-dup", "version": "9.92.85", "components": ["X"],
             "clientEnvironments": ["Y"], "bookedBy": "Z",
             "bookedAt": "2026-01-01T00:00:00Z", "status": "booked"},
        ])
        r = client.get("/api/hotfix-booking/history?minor=92")
        assert r.status_code == 200
        entries = [h for h in r.json()["hotfixes"] if h["version"] == "9.92.85"]
        assert len(entries) == 1
        assert entries[0]["type"] == "deployed"

    def test_jira_error_yields_500(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        mock_jira.post("/rest/api/3/search/jql").mock(return_value=httpx.Response(500))
        r = client.get("/api/hotfix-booking/history")
        assert r.status_code == 500
        assert r.json() == {"error": "Failed to fetch hotfix history"}
