"""Tests for /api/hotfix-booking/environments-compare — the endpoint that
powers the Compare tab."""
from __future__ import annotations

import httpx
import respx
from fastapi.testclient import TestClient

from tests.conftest import load_fixture, write_bookings


# ---------------------------------------------------------------------------
# Query-param validation
# ---------------------------------------------------------------------------
class TestValidation:
    def test_missing_env_a_returns_400(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        r = client.get("/api/hotfix-booking/environments-compare?envB=X")
        assert r.status_code == 400
        assert "envA" in r.json()["detail"]

    def test_missing_env_b_returns_400(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        r = client.get("/api/hotfix-booking/environments-compare?envA=X")
        assert r.status_code == 400

    def test_empty_strings_return_400(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        r = client.get("/api/hotfix-booking/environments-compare?envA=&envB=")
        assert r.status_code == 400

    def test_whitespace_only_returns_400(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        r = client.get(
            "/api/hotfix-booking/environments-compare?envA=%20%20&envB=%20"
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Happy path — hits Jira and computes side-by-side view
# ---------------------------------------------------------------------------
class TestSuccess:
    def _mock_jira(self, mock_jira: respx.MockRouter, fixture: str = "search_all.json") -> None:
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json=load_fixture(fixture))
        )

    def test_success_returns_row_per_component_touched(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        """`search_all.json` fixture:
        - CM-1001 (Deployment Completed, 9.94.20): {Fortress, Convex} × {Alerts, DV_Web}
        - CM-1002 (Done, 9.94.22):                 {Convex} × {Alerts}
        - CM-1003 (Done, 9.92.85):                 {TPG} × {Analytics}
        - CM-1004 (In Progress, 9.94.25):          {Fortress} × {DV_Web}

        Compare Fortress vs Convex: touches Alerts + DV_Web only.
        Analytics is TPG-only → excluded.
        """
        self._mock_jira(mock_jira)
        r = client.get(
            "/api/hotfix-booking/environments-compare"
            "?envA=CL001 - Fortress&envB=CL002 - Convex"
        )
        assert r.status_code == 200
        body = r.json()
        assert body["envA"] == "CL001 - Fortress"
        assert body["envB"] == "CL002 - Convex"
        rows = {row["component"]: row for row in body["rows"]}
        assert set(rows) == {"Alerts", "DV_Web"}

        # Alerts: A only got CM-1001 (9.94.20). B got both CM-1001 (9.94.20)
        # and CM-1002 (9.94.22) → highest wins on B.
        assert rows["Alerts"]["a"]["deployed"]["version"] == "9.94.20"
        assert rows["Alerts"]["a"]["deployed"]["cmKey"] == "CM-1001"
        assert rows["Alerts"]["b"]["deployed"]["version"] == "9.94.22"
        assert rows["Alerts"]["b"]["deployed"]["cmKey"] == "CM-1002"

        # DV_Web: A has CM-1001 (deployed 9.94.20) + CM-1004 (in-flight 9.94.25).
        # B has only CM-1001 (deployed 9.94.20).
        assert rows["DV_Web"]["a"]["deployed"]["version"] == "9.94.20"
        assert rows["DV_Web"]["a"]["inflight"] == [
            {"version": "9.94.25", "status": "In Progress", "cmKey": "CM-1004"}
        ]
        assert rows["DV_Web"]["b"]["deployed"]["version"] == "9.94.20"
        assert rows["DV_Web"]["b"]["inflight"] == []

    def test_response_includes_jira_base_url(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        """The Compare-tab UI links CM keys to Jira via jiraBaseUrl (same
        pattern as /client-versions)."""
        self._mock_jira(mock_jira)
        r = client.get(
            "/api/hotfix-booking/environments-compare"
            "?envA=CL001 - Fortress&envB=CL002 - Convex"
        )
        assert r.status_code == 200
        assert r.json()["jiraBaseUrl"] == "https://jira.test"

    def test_envs_not_present_in_any_cm_yield_zero_rows(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        self._mock_jira(mock_jira)
        r = client.get(
            "/api/hotfix-booking/environments-compare"
            "?envA=Ghost-1&envB=Ghost-2"
        )
        assert r.status_code == 200
        assert r.json()["rows"] == []


# ---------------------------------------------------------------------------
# Booking integration — local bookings surface in cells
# ---------------------------------------------------------------------------
class TestBookings:
    def test_local_booking_shows_in_cell(
        self,
        client: TestClient,
        mock_jira: respx.MockRouter,
        bookings_file,
    ) -> None:
        # Empty Jira response so bookings dominate.
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json={"issues": []})
        )
        write_bookings(
            bookings_file,
            [
                {
                    "id": "HB-1",
                    "version": "9.99.5",
                    "components": ["Alerts"],
                    "clientEnvironments": ["CL001 - Fortress"],
                    "bookedBy": "PM Person",
                    "bookedByEmail": "pm@example.com",
                    "bookedAt": "2026-07-01T12:00:00Z",
                    "status": "booked",
                    "parents": [],
                    "originalParents": [],
                    "rebaseHistory": [],
                }
            ],
        )
        r = client.get(
            "/api/hotfix-booking/environments-compare"
            "?envA=CL001 - Fortress&envB=CL002 - Convex"
        )
        assert r.status_code == 200
        rows = r.json()["rows"]
        assert len(rows) == 1
        assert rows[0]["component"] == "Alerts"
        assert rows[0]["a"]["booked"] == [
            {
                "version": "9.99.5",
                "bookingId": "HB-1",
                "bookedBy": "PM Person",
                "bookedAt": "2026-07-01T12:00:00Z",
            }
        ]
        assert rows[0]["b"]["booked"] == []

    def test_cancelled_booking_excluded(
        self,
        client: TestClient,
        mock_jira: respx.MockRouter,
        bookings_file,
    ) -> None:
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json={"issues": []})
        )
        write_bookings(
            bookings_file,
            [
                {
                    "id": "HB-1",
                    "version": "9.99.5",
                    "components": ["Alerts"],
                    "clientEnvironments": ["CL001 - Fortress"],
                    "bookedBy": "PM Person",
                    "bookedByEmail": "pm@example.com",
                    "bookedAt": "2026-07-01T12:00:00Z",
                    "status": "cancelled",
                    "parents": [],
                    "originalParents": [],
                    "rebaseHistory": [],
                }
            ],
        )
        r = client.get(
            "/api/hotfix-booking/environments-compare"
            "?envA=CL001 - Fortress&envB=CL002 - Convex"
        )
        assert r.status_code == 200
        assert r.json()["rows"] == []


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------
class TestErrors:
    def test_jira_500_yields_500(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        mock_jira.post("/rest/api/3/search/jql").mock(return_value=httpx.Response(502))
        r = client.get(
            "/api/hotfix-booking/environments-compare"
            "?envA=CL001 - Fortress&envB=CL002 - Convex"
        )
        assert r.status_code == 500
        assert r.json() == {"error": "Failed to fetch CMs from Jira"}

    def test_malformed_bookings_yields_500(
        self,
        client: TestClient,
        mock_jira: respx.MockRouter,
        bookings_file,
    ) -> None:
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json={"issues": []})
        )
        # Corrupt the bookings file so load_bookings raises.
        bookings_file.write_text("this is not JSON")
        r = client.get(
            "/api/hotfix-booking/environments-compare"
            "?envA=CL001 - Fortress&envB=CL002 - Convex"
        )
        assert r.status_code == 500
        assert "malformed" in r.json()["error"].lower()
