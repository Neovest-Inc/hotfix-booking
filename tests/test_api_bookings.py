"""Tests for /bookings and /book (Jira mocked minimally where needed)."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.conftest import load_fixture, read_bookings, write_bookings


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


class TestGetBookings:
    def test_empty(self, client: TestClient) -> None:
        r = client.get("/api/hotfix-booking/bookings")
        assert r.status_code == 200
        assert r.json() == {"bookings": []}

    def test_returns_file_contents_verbatim(
        self, client: TestClient, bookings_file: Path
    ) -> None:
        write_bookings(bookings_file, [
            {"id": "HB-1", "version": "9.92.1", "components": ["A"],
             "clientEnvironments": ["CL001"], "bookedBy": "U", "bookedAt": "T",
             "status": "booked"}
        ])
        r = client.get("/api/hotfix-booking/bookings")
        assert r.status_code == 200
        assert r.json()["bookings"][0]["version"] == "9.92.1"


class TestPostBook:
    """POST /book re-checks Jira on every request to make sure the version
    the user is trying to book is still the current next available.

    The `search_deployed.json` fixture has max deployed = 9.94.22, so the
    fresh-computed next is 9.94.23 unless a test writes a booking that bumps it.
    """

    _valid = {
        "version": "9.94.23",
        "components": ["Alerts"],
        "clientEnvironments": ["CL001 - Fortress"],
        "bookedBy": "Alice",
    }

    @pytest.fixture(autouse=True)
    def _stub_jira(self, mock_jira: respx.MockRouter) -> respx.MockRouter:
        """Default Jira stub used by every /book test — max deployed = 9.94.22."""
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json=load_fixture("search_deployed.json"))
        )
        return mock_jira

    def test_success_writes_file_and_returns_booking(
        self, client: TestClient, bookings_file: Path
    ) -> None:
        r = client.post("/api/hotfix-booking/book", json=self._valid)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["success"] is True
        booking = body["booking"]
        assert booking["version"] == "9.94.23"
        assert booking["components"] == ["Alerts"]
        assert booking["clientEnvironments"] == ["CL001 - Fortress"]
        assert booking["bookedBy"] == "Alice"
        assert booking["status"] == "booked"
        assert booking["id"].startswith("HB-")
        assert booking["bookedAt"]
        assert read_bookings(bookings_file) == [booking]

    def test_booked_by_defaults_to_unknown(
        self, client: TestClient, bookings_file: Path
    ) -> None:
        payload = {**self._valid}
        payload.pop("bookedBy")
        r = client.post("/api/hotfix-booking/book", json=payload)
        assert r.status_code == 200, r.text
        assert r.json()["booking"]["bookedBy"] == "Unknown"

    @pytest.mark.parametrize(
        "missing_key",
        ["version", "components", "clientEnvironments"],
    )
    def test_missing_field_returns_400(
        self, client: TestClient, missing_key: str
    ) -> None:
        payload = {**self._valid}
        payload.pop(missing_key)
        r = client.post("/api/hotfix-booking/book", json=payload)
        assert r.status_code == 400
        assert "Missing required fields" in r.json()["error"]

    def test_empty_components_returns_400(self, client: TestClient) -> None:
        payload = {**self._valid, "components": []}
        r = client.post("/api/hotfix-booking/book", json=payload)
        assert r.status_code == 400
        assert r.json() == {"error": "At least one component is required"}

    def test_non_list_components_returns_400(self, client: TestClient) -> None:
        payload = {**self._valid, "components": "Alerts"}
        r = client.post("/api/hotfix-booking/book", json=payload)
        assert r.status_code == 400
        assert r.json() == {"error": "At least one component is required"}

    def test_empty_client_environments_returns_400(self, client: TestClient) -> None:
        payload = {**self._valid, "clientEnvironments": []}
        r = client.post("/api/hotfix-booking/book", json=payload)
        assert r.status_code == 400
        assert r.json() == {"error": "At least one client environment is required"}

    @pytest.mark.parametrize(
        "bad_version",
        ["1.2", "1.2.3.4", "1.2.a", "v1.2.3", "latest"],
    )
    def test_invalid_version_format_returns_400(
        self, client: TestClient, bad_version: str
    ) -> None:
        payload = {**self._valid, "version": bad_version}
        r = client.post("/api/hotfix-booking/book", json=payload)
        assert r.status_code == 400
        assert "x.y.z" in r.json()["error"]

    def test_appends_to_existing_bookings(
        self, client: TestClient, bookings_file: Path
    ) -> None:
        # Pre-existing booking bumps next-version from 9.94.23 to 9.94.24.
        write_bookings(bookings_file, [
            {"id": "HB-1", "version": "9.94.23", "components": ["A"],
             "clientEnvironments": ["CL001"], "bookedBy": "U",
             "bookedAt": "2026-07-01T00:00:00+00:00",
             "status": "booked"}
        ])
        payload = {**self._valid, "version": "9.94.24"}
        r = client.post("/api/hotfix-booking/book", json=payload)
        assert r.status_code == 200, r.text
        after = read_bookings(bookings_file)
        assert [b["version"] for b in after] == ["9.94.23", "9.94.24"]

    # ------------------------------------------------------------------
    # Fresh-next-version check (protects against stale UI state)
    # ------------------------------------------------------------------
    def test_rejects_stale_version_lower_than_current_next(
        self, client: TestClient
    ) -> None:
        # Fresh next is 9.94.23; user is trying to book 9.94.20 (already deployed in fixture).
        payload = {**self._valid, "version": "9.94.20"}
        r = client.post("/api/hotfix-booking/book", json=payload)
        assert r.status_code == 409
        body = r.json()
        assert body["currentNext"] == "9.94.23"
        assert "9.94.20" in body["error"]
        assert "9.94.23" in body["error"]

    def test_rejects_version_ahead_of_current_next(
        self, client: TestClient
    ) -> None:
        # Fresh next is 9.94.23; user submits 9.94.99.
        payload = {**self._valid, "version": "9.94.99"}
        r = client.post("/api/hotfix-booking/book", json=payload)
        assert r.status_code == 409
        assert r.json()["currentNext"] == "9.94.23"

    def test_rejects_when_stale_because_someone_else_booked(
        self, client: TestClient, bookings_file: Path
    ) -> None:
        # Between the user seeing 9.94.23 and clicking Book, someone else booked 9.94.23.
        # New fresh-next is 9.94.24. User's submission of 9.94.23 must be rejected.
        write_bookings(bookings_file, [
            {"id": "HB-first", "version": "9.94.23", "components": ["X"],
             "clientEnvironments": ["Y"], "bookedBy": "Z",
             "bookedAt": "2026-07-01T00:00:00+00:00", "status": "booked"},
        ])
        r = client.post("/api/hotfix-booking/book", json=self._valid)  # version 9.94.23
        assert r.status_code == 409
        assert r.json()["currentNext"] == "9.94.24"

    def test_jira_down_during_book_yields_500(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        # Override the autouse stub with a failure response.
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(503)
        )
        r = client.post("/api/hotfix-booking/book", json=self._valid)
        assert r.status_code == 500

    # ------------------------------------------------------------------
    # Booking for previous minor releases (release-picker feature)
    # ------------------------------------------------------------------
    def _mixed_responder(self, request: httpx.Request) -> httpx.Response:
        """Return the 9.92 by-version fixture when the JQL targets 9.92, else the
        general deployed fixture (which also contains 9.94.x entries)."""
        import json as _json
        body = _json.loads(request.content or b"{}")
        jql = body.get("jql", "")
        if "9.92" in jql:
            return httpx.Response(200, json=load_fixture("search_by_version_9_92.json"))
        return httpx.Response(200, json=load_fixture("search_deployed.json"))

    def test_book_valid_next_for_previous_minor(
        self, client: TestClient, mock_jira: respx.MockRouter, bookings_file: Path
    ) -> None:
        """The by-version fixture for 9.92 has max 9.92.86, so 9.92.87 is the
        legit next hotfix for the 9.92.x release line."""
        mock_jira.post("/rest/api/3/search/jql").mock(side_effect=self._mixed_responder)
        payload = {**self._valid, "version": "9.92.87"}
        r = client.post("/api/hotfix-booking/book", json=payload)
        assert r.status_code == 200, r.text
        assert r.json()["booking"]["version"] == "9.92.87"

    def test_book_wrong_version_for_previous_minor_returns_409(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        """Attempting to book a stale/wrong version for 9.92.x must be rejected
        with the correct current-next for 9.92, not for the current release."""
        mock_jira.post("/rest/api/3/search/jql").mock(side_effect=self._mixed_responder)
        payload = {**self._valid, "version": "9.92.50"}   # wrong; next is 9.92.87
        r = client.post("/api/hotfix-booking/book", json=payload)
        assert r.status_code == 409
        assert r.json()["currentNext"] == "9.92.87"

    def test_book_current_release_still_works(
        self, client: TestClient, mock_jira: respx.MockRouter, bookings_file: Path
    ) -> None:
        """Sanity: booking a hotfix for the *current* release doesn't hit
        the by-version code path (no ?minor= implied) and still works."""
        mock_jira.post("/rest/api/3/search/jql").mock(side_effect=self._mixed_responder)
        # Current is 9.94.22 → next is 9.94.23
        r = client.post("/api/hotfix-booking/book", json=self._valid)  # version 9.94.23
        assert r.status_code == 200, r.text


class TestMalformedBookingsFile:
    """When the JSON store is corrupted, endpoints must surface an error
    instead of silently returning an empty state (which would mask data loss)."""

    def _corrupt(self, path: Path) -> None:
        path.write_text("not-valid-json{{{")

    def test_bookings_endpoint_returns_500(
        self, client: TestClient, bookings_file: Path
    ) -> None:
        self._corrupt(bookings_file)
        r = client.get("/api/hotfix-booking/bookings")
        assert r.status_code == 500
        assert "malformed" in r.json()["error"].lower()

    def test_book_endpoint_returns_500_and_does_not_overwrite(
        self, client: TestClient, bookings_file: Path, mock_jira: respx.MockRouter
    ) -> None:
        # /book hits Jira first, so we need a mock even for the malformed-file case.
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json=load_fixture("search_deployed.json"))
        )
        self._corrupt(bookings_file)
        r = client.post("/api/hotfix-booking/book", json={
            "version": "9.94.23",
            "components": ["A"],
            "clientEnvironments": ["CL"],
        })
        assert r.status_code == 500
        assert bookings_file.read_text() == "not-valid-json{{{"
