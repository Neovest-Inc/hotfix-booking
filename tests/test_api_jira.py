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
        body = r.json()
        assert body["currentHighest"] == "9.94.30"
        assert body["nextVersion"] == "9.94.31"
        assert body["baseVersion"] == "9.94.0"

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
        body = r.json()
        assert body["nextVersion"] is None
        assert body["error"] == "No deployed versions found."

    def test_jira_error_yields_500(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        mock_jira.post("/rest/api/3/search/jql").mock(return_value=httpx.Response(500))
        r = client.get("/api/hotfix-booking/next-version")
        assert r.status_code == 500
        assert r.json() == {"error": "Failed to calculate next version"}

    def test_age_based_cleanup_removes_old_bookings(
        self, client: TestClient, mock_jira: respx.MockRouter, bookings_file: Path
    ) -> None:
        """Bookings older than the retention window are auto-removed on /next-version."""
        from datetime import datetime, timedelta, timezone
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json=load_fixture("search_deployed.json"))
        )
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=200)).isoformat()   # older than 180d default
        fresh = (now - timedelta(days=10)).isoformat()
        write_bookings(bookings_file, [
            {"id": "HB-OLD", "version": "9.94.50", "components": ["A"],
             "clientEnvironments": ["CL"], "bookedBy": "U", "bookedAt": old,
             "status": "booked"},
            {"id": "HB-FRESH", "version": "9.94.51", "components": ["A"],
             "clientEnvironments": ["CL"], "bookedBy": "U", "bookedAt": fresh,
             "status": "booked"},
        ])

        r = client.get("/api/hotfix-booking/next-version")
        assert r.status_code == 200
        remaining = read_bookings(bookings_file)
        assert [b["id"] for b in remaining] == ["HB-FRESH"]

    def test_malformed_bookings_file_yields_500(
        self, client: TestClient, mock_jira: respx.MockRouter, bookings_file: Path
    ) -> None:
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json=load_fixture("search_deployed.json"))
        )
        bookings_file.write_text("not-json{")
        r = client.get("/api/hotfix-booking/next-version")
        assert r.status_code == 500
        assert "malformed" in r.json()["error"].lower()

    def test_response_includes_minor_versions_for_dropdown(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        """The Book Hotfix UI populates its release dropdown from this response,
        so /next-version must include the discovered minor lines and current pair."""
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json=load_fixture("search_deployed.json"))
        )
        r = client.get("/api/hotfix-booking/next-version")
        assert r.status_code == 200
        body = r.json()
        # Deployed fixture has (9,94) and (9,92) as distinct pairs.
        assert body["currentMajor"] == 9
        assert body["currentMinor"] == 94
        assert body["major"] == 9
        assert body["minor"] == 94
        assert [(m["major"], m["minor"]) for m in body["minorVersions"]] == [(9, 94), (9, 92)]

    def test_dropdown_includes_lines_with_only_in_progress_cms(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        """A release line that only has 'Open' / 'In Progress' CMs (no deploys yet)
        must still appear in the release dropdown — otherwise users can't book against
        a brand-new release that's actively being worked on."""
        all_issues = [
            {"key": "CM-1", "fields": {
                "summary": "s", "status": {"name": "Deployment Completed"},
                "components": [{"name": "A"}],
                "fixVersions": [{"name": "9.94.5"}],
                "customfield_13235": [], "customfield_10751": None,
                "reporter": {"displayName": "R"}}},
            # In-Progress 9.98.1 — no deploys for 9.98 yet
            {"key": "CM-2", "fields": {
                "summary": "s", "status": {"name": "Open"},
                "components": [{"name": "A"}],
                "fixVersions": [{"name": "9.98.1"}],
                "customfield_13235": [], "customfield_10751": None,
                "reporter": {"displayName": "R"}}},
        ]

        # Simulate Jira's server-side JQL filter: a query with the
        # status-filter clause returns ONLY the deployed CMs.
        def _responder(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content or b"{}")
            jql = body.get("jql", "")
            if 'status in ("Deployment Completed", "Done")' in jql:
                deployed = [i for i in all_issues
                            if i["fields"]["status"]["name"] in ("Deployment Completed", "Done")]
                return httpx.Response(200, json={"issues": deployed})
            return httpx.Response(200, json={"issues": all_issues})
        mock_jira.post("/rest/api/3/search/jql").mock(side_effect=_responder)

        r = client.get("/api/hotfix-booking/next-version")
        assert r.status_code == 200
        body = r.json()
        # Both lines discovered; 9.98 is highest (current).
        assert body["currentMajor"] == 9
        assert body["currentMinor"] == 98
        pairs = [(m["major"], m["minor"]) for m in body["minorVersions"]]
        assert (9, 98) in pairs
        assert (9, 94) in pairs

    def test_minor_query_filters_to_that_minor(
        self, client: TestClient, mock_jira: respx.MockRouter, bookings_file: Path
    ) -> None:
        """?minor=X uses the by-version Jira query (no 100-day cap) and returns
        the next hotfix for THAT specific minor."""
        def _responder(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content or b"{}")
            jql = body.get("jql", "")
            if "fixVersion" in jql:
                return httpx.Response(
                    200, json=load_fixture("search_by_version_9_92.json")
                )
            return httpx.Response(200, json=load_fixture("search_deployed.json"))
        mock_jira.post("/rest/api/3/search/jql").mock(side_effect=_responder)

        # search_by_version_9_92 fixture max is 9.92.86 → next should be 9.92.87
        r = client.get("/api/hotfix-booking/next-version?major=9&minor=92")
        assert r.status_code == 200
        body = r.json()
        assert body["nextVersion"] == "9.92.87"
        assert body["currentHighest"] == "9.92.86"
        assert body["minor"] == 92
        assert body["major"] == 9
        # currentMinor (94) is different from the requested minor (92)
        assert body["currentMinor"] == 94

    def test_minor_query_counts_bookings_for_that_minor_only(
        self, client: TestClient, mock_jira: respx.MockRouter, bookings_file: Path
    ) -> None:
        def _responder(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content or b"{}")
            if "fixVersion" in body.get("jql", ""):
                return httpx.Response(
                    200, json=load_fixture("search_by_version_9_92.json")
                )
            return httpx.Response(200, json=load_fixture("search_deployed.json"))
        mock_jira.post("/rest/api/3/search/jql").mock(side_effect=_responder)

        # A pending booking for 9.92.87 should bump the next to 9.92.88.
        # Bookings for other minors (9.94.x) must be ignored for this query.
        write_bookings(bookings_file, [
            {"id": "HB-92", "version": "9.92.87", "components": ["A"],
             "clientEnvironments": ["CL"], "bookedBy": "U",
             "bookedAt": "2026-07-01T00:00:00+00:00", "status": "booked"},
            {"id": "HB-94", "version": "9.94.99", "components": ["A"],
             "clientEnvironments": ["CL"], "bookedBy": "U",
             "bookedAt": "2026-07-01T00:00:00+00:00", "status": "booked"},
        ])
        r = client.get("/api/hotfix-booking/next-version?major=9&minor=92")
        assert r.status_code == 200
        body = r.json()
        assert body["nextVersion"] == "9.92.88"

    def test_minor_query_with_no_matching_data_returns_error(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        def _responder(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content or b"{}")
            if "fixVersion" in body.get("jql", ""):
                return httpx.Response(200, json={"issues": []})
            return httpx.Response(200, json=load_fixture("search_deployed.json"))
        mock_jira.post("/rest/api/3/search/jql").mock(side_effect=_responder)

        r = client.get("/api/hotfix-booking/next-version?major=9&minor=80")
        assert r.status_code == 200
        body = r.json()
        assert body["nextVersion"] is None
        assert "9.80" in body["error"]
        # Dropdown data still present so the UI stays usable
        assert body["currentMinor"] == 94
        assert isinstance(body["minorVersions"], list)


# ---------------------------------------------------------------------------
# /client-versions
# ---------------------------------------------------------------------------
# The Version Matrix intentionally shows only in-flight CMs (work currently
# moving through the workflow). Already-shipped CMs are excluded — "what is
# really deployed" is answered by a separate DevOps tool that reads client
# servers directly.
class TestClientVersions:
    def test_shows_only_in_flight_cms(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        # search_all fixture has CM-1001/1002/1003 (deployed) + CM-1004
        # (In Progress). Only CM-1004 should surface in the matrix.
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json=load_fixture("search_all.json"))
        )
        r = client.get("/api/hotfix-booking/client-versions")
        assert r.status_code == 200
        body = r.json()

        # Only the (CL001, DV_Web) cell — where CM-1004 lives — is populated.
        assert body["clients"] == ["CL001 - Fortress"]
        assert body["components"] == ["DV_Web"]
        cell = body["matrix"]["CL001 - Fortress"]["DV_Web"]
        # No deployed headline in in-flight-only mode.
        assert cell["version"] is None
        assert cell["cmKey"] is None
        assert cell["deployedAt"] is None
        # In-flight CM appears in the popover list.
        assert cell["inflight"] == [
            {"version": "9.94.25", "status": "In Progress", "cmKey": "CM-1004"}
        ]

    def test_deployed_only_fixture_yields_empty_matrix(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        """Every CM in `search_deployed.json` is Done / Deployment Completed —
        so with in-flight-only mode there's nothing to show."""
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json=load_fixture("search_deployed.json"))
        )
        r = client.get("/api/hotfix-booking/client-versions")
        assert r.status_code == 200
        body = r.json()
        assert body["matrix"] == {}
        assert body["clients"] == []
        assert body["components"] == []

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
        # search_all fixture has (9,94) and (9,92) as distinct pairs.
        assert body["currentMajor"] == 9
        assert body["currentMinor"] == 94
        assert body["targetMajor"] == 9
        assert body["targetMinor"] == 94
        assert body["jiraBaseUrl"] == TEST_JIRA_BASE
        assert [(m["major"], m["minor"]) for m in body["minorVersions"]] == [(9, 94), (9, 92)]
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
        r = client.get("/api/hotfix-booking/history?major=9&minor=92")
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
        # 9.92.85 is in the deployed fixture. When a booking exists for that
        # same version, we now emit a single UNIFIED row carrying both the CM
        # fields (cmKey) and the booking fields (id, bookingStatus) instead
        # of duplicating the version across two rows.
        write_bookings(bookings_file, [
            {"id": "HB-dup", "version": "9.92.85", "components": ["X"],
             "clientEnvironments": ["Y"], "bookedBy": "Z",
             "bookedAt": "2026-01-01T00:00:00Z", "status": "booked"},
        ])
        r = client.get("/api/hotfix-booking/history?major=9&minor=92")
        assert r.status_code == 200
        entries = [h for h in r.json()["hotfixes"] if h["version"] == "9.92.85"]
        assert len(entries) == 1
        row = entries[0]
        assert row["cmKey"]  # CM data present
        assert row["id"] == "HB-dup"  # booking data present
        assert row["type"] == "booked"

    def test_jira_error_yields_500(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        mock_jira.post("/rest/api/3/search/jql").mock(return_value=httpx.Response(500))
        r = client.get("/api/hotfix-booking/history")
        assert r.status_code == 500
        assert r.json() == {"error": "Failed to fetch hotfix history"}


# ---------------------------------------------------------------------------
# _search_jql pagination
# ---------------------------------------------------------------------------
# Jira's /rest/api/3/search/jql endpoint caps each page at ~100 issues and
# uses `nextPageToken` for pagination. The client must loop until `isLast`
# is true (or the token is missing), concatenating issues across pages.
# A hard cap of 15 pages protects against runaway loops from bad tokens.
class TestSearchJqlPagination:
    def test_paginates_across_multiple_pages(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        """Two-page response: both pages' issues should appear concatenated."""
        page1 = {
            "issues": [{
                "key": "CM-9001",
                "fields": {
                    "summary": "page 1", "status": {"name": "Done"},
                    "components": [{"name": "Alerts"}],
                    "fixVersions": [{"name": "9.94.10"}],
                    "customfield_13235": [{"value": "CL001 - Fortress"}],
                    "customfield_10751": "2026-05-01",
                    "reporter": {"displayName": "Alice"},
                },
            }],
            "nextPageToken": "TOKEN_PAGE_2",
            "isLast": False,
        }
        page2 = {
            "issues": [{
                "key": "CM-9002",
                "fields": {
                    "summary": "page 2", "status": {"name": "Done"},
                    "components": [{"name": "Alerts"}],
                    "fixVersions": [{"name": "9.94.11"}],
                    "customfield_13235": [{"value": "CL002 - Convex"}],
                    "customfield_10751": "2026-05-02",
                    "reporter": {"displayName": "Bob"},
                },
            }],
            "isLast": True,
        }

        def _responder(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content or b"{}")
            if body.get("nextPageToken") == "TOKEN_PAGE_2":
                return httpx.Response(200, json=page2)
            return httpx.Response(200, json=page1)

        mock_jira.post("/rest/api/3/search/jql").mock(side_effect=_responder)

        r = client.get("/api/hotfix-booking/deployed-cms")
        assert r.status_code == 200
        keys = [cm["key"] for cm in r.json()["cms"]]
        assert keys == ["CM-9001", "CM-9002"], (
            "Both pages should be concatenated; got only page 1 means pagination is broken."
        )

    def test_stops_at_max_pages_safety_cap(
        self, client: TestClient, mock_jira: respx.MockRouter, caplog
    ) -> None:
        """Runaway `isLast: false` responses must terminate at MAX_PAGES=15."""
        call_count = {"n": 0}

        def _endless_responder(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(200, json={
                "issues": [{
                    "key": f"CM-{call_count['n']:04d}",
                    "fields": {
                        "summary": f"page {call_count['n']}",
                        "status": {"name": "Done"},
                        "components": [{"name": "Alerts"}],
                        "fixVersions": [{"name": "9.94.10"}],
                        "customfield_13235": [{"value": "CL001 - Fortress"}],
                        "customfield_10751": None,
                        "reporter": {"displayName": "X"},
                    },
                }],
                "nextPageToken": f"TOKEN_{call_count['n']}",
                "isLast": False,
            })

        mock_jira.post("/rest/api/3/search/jql").mock(side_effect=_endless_responder)

        import logging
        with caplog.at_level(logging.WARNING):
            r = client.get("/api/hotfix-booking/deployed-cms")

        assert r.status_code == 200
        assert call_count["n"] == 15, (
            f"Expected exactly 15 pages before hitting the safety cap; got {call_count['n']}."
        )
        assert any("max" in rec.message.lower() and "page" in rec.message.lower()
                   for rec in caplog.records), (
            "Expected a WARNING log when the pagination cap is hit."
        )

    def test_single_page_when_isLast_missing(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        """Legacy fixture without `isLast`/`nextPageToken` must terminate cleanly
        after one page (backwards-compat for hermetic synthetic fixtures)."""
        call_count = {"n": 0}

        def _responder(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(200, json=load_fixture("search_all.json"))

        mock_jira.post("/rest/api/3/search/jql").mock(side_effect=_responder)

        r = client.get("/api/hotfix-booking/deployed-cms")
        assert r.status_code == 200
        assert call_count["n"] == 1, (
            f"Response without `isLast` should be treated as terminal; got {call_count['n']} calls."
        )


# ---------------------------------------------------------------------------
# JQL time window
# ---------------------------------------------------------------------------
class TestJqlTimeWindow:
    """The `created >= -Nd` window sets how far back the matrix and next-version
    look. Must cover at least 4 months (~120 days) to catch clients on a
    3-month release cadence who may be delayed."""

    def test_fetch_deployed_uses_at_least_120_day_window(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        captured_jql: list[str] = []

        def _responder(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content or b"{}")
            captured_jql.append(body.get("jql", ""))
            return httpx.Response(200, json=load_fixture("search_all.json"))

        mock_jira.post("/rest/api/3/search/jql").mock(side_effect=_responder)

        r = client.get("/api/hotfix-booking/client-versions")
        assert r.status_code == 200
        assert captured_jql, "Expected at least one search-jql POST"
        # Extract the -Nd bound from the JQL
        import re
        match = re.search(r"created\s*>=\s*-(\d+)d", captured_jql[0])
        assert match, f"Expected `created >= -Nd` in JQL; got: {captured_jql[0]}"
        days = int(match.group(1))
        assert days >= 120, (
            f"Time window is {days}d; must be >= 120d to cover 4 months per business requirement."
        )
