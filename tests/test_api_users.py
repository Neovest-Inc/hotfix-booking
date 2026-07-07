"""Tests for GET /api/hotfix-booking/resolve-user and its integration with POST /book."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.conftest import load_fixture, read_bookings


IVAN_REAL = {
    "accountId": "712020:ba19a0d3-f62c-4b6a-8d58-3b09c372721a",
    "displayName": "Ivan Queiroz",
    "emailAddress": "iqueiroz@neovest.com",
    "active": True,
}
IVAN_QUEUE_STUB = {
    "accountId": "qm:50fd12cc-c619-4e94-828c-2a0eeca1bcb6:2e7e359e",
    "displayName": "iqueiroz@neovest.com",
    "emailAddress": "",
    "active": True,
}


class TestResolveUserEndpoint:
    def test_resolves_real_user(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        mock_jira.get("/rest/api/3/user/search").mock(
            return_value=httpx.Response(200, json=[IVAN_REAL, IVAN_QUEUE_STUB])
        )
        r = client.get("/api/hotfix-booking/resolve-user?email=iqueiroz@neovest.com")
        assert r.status_code == 200
        body = r.json()
        assert body["email"] == "iqueiroz@neovest.com"
        assert body["displayName"] == "Ivan Queiroz"
        assert body["accountId"] == IVAN_REAL["accountId"]

    def test_no_match_returns_404(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        mock_jira.get("/rest/api/3/user/search").mock(
            return_value=httpx.Response(200, json=[])
        )
        r = client.get("/api/hotfix-booking/resolve-user?email=nobody@neovest.com")
        assert r.status_code == 404
        assert "nobody@neovest.com" in r.json()["error"]

    def test_only_stub_account_returns_404(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        """If Jira returns only a queue stub (no real user), we treat as no match."""
        mock_jira.get("/rest/api/3/user/search").mock(
            return_value=httpx.Response(200, json=[IVAN_QUEUE_STUB])
        )
        r = client.get("/api/hotfix-booking/resolve-user?email=iqueiroz@neovest.com")
        assert r.status_code == 404

    def test_missing_email_returns_400(self, client: TestClient) -> None:
        r = client.get("/api/hotfix-booking/resolve-user")
        assert r.status_code == 422 or r.status_code == 400  # FastAPI missing-query default

    def test_empty_email_returns_400(self, client: TestClient) -> None:
        r = client.get("/api/hotfix-booking/resolve-user?email=")
        assert r.status_code == 400

    def test_jira_error_returns_500(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        mock_jira.get("/rest/api/3/user/search").mock(
            return_value=httpx.Response(503)
        )
        r = client.get("/api/hotfix-booking/resolve-user?email=iqueiroz@neovest.com")
        assert r.status_code == 500


class TestBookWithEmailResolution:
    """POST /book with a bookedByEmail resolves via Jira and stores the real
    displayName as bookedBy — client-sent bookedBy is ignored to prevent spoofing."""

    def _stub_jira_for_book_and_lookup(
        self, mock_jira: respx.MockRouter
    ) -> respx.MockRouter:
        # Search-jql stub (for the fresh-next-version check in /book).
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json=load_fixture("search_deployed.json"))
        )
        # User-search stub.
        mock_jira.get("/rest/api/3/user/search").mock(
            return_value=httpx.Response(200, json=[IVAN_REAL, IVAN_QUEUE_STUB])
        )
        return mock_jira

    _valid = {
        "version": "9.94.23",
        "components": ["Alerts"],
        "clientEnvironments": ["CL001 - Fortress"],
    }

    def test_email_resolves_and_becomes_bookedBy(
        self, client: TestClient, mock_jira: respx.MockRouter, bookings_file: Path
    ) -> None:
        self._stub_jira_for_book_and_lookup(mock_jira)
        payload = {**self._valid, "bookedByEmail": "iqueiroz@neovest.com"}
        r = client.post("/api/hotfix-booking/book", json=payload)
        assert r.status_code == 200, r.text
        booking = r.json()["booking"]
        assert booking["bookedBy"] == "Ivan Queiroz"
        assert booking["bookedByEmail"] == "iqueiroz@neovest.com"
        stored = read_bookings(bookings_file)[0]
        assert stored["bookedBy"] == "Ivan Queiroz"
        assert stored["bookedByEmail"] == "iqueiroz@neovest.com"

    def test_client_bookedBy_is_ignored_when_email_provided(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        """Client can't spoof by setting bookedBy — the email lookup wins."""
        self._stub_jira_for_book_and_lookup(mock_jira)
        payload = {
            **self._valid,
            "bookedByEmail": "iqueiroz@neovest.com",
            "bookedBy": "Someone Else",
        }
        r = client.post("/api/hotfix-booking/book", json=payload)
        assert r.status_code == 200, r.text
        assert r.json()["booking"]["bookedBy"] == "Ivan Queiroz"

    def test_unresolvable_email_returns_400(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json=load_fixture("search_deployed.json"))
        )
        mock_jira.get("/rest/api/3/user/search").mock(
            return_value=httpx.Response(200, json=[])
        )
        payload = {**self._valid, "bookedByEmail": "nobody@neovest.com"}
        r = client.post("/api/hotfix-booking/book", json=payload)
        assert r.status_code == 400
        assert "nobody@neovest.com" in r.json()["error"]

    def test_backwards_compat_no_email_still_works(
        self, client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        """POST without bookedByEmail falls back to plain bookedBy string."""
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json=load_fixture("search_deployed.json"))
        )
        payload = {**self._valid, "bookedBy": "Legacy User"}
        r = client.post("/api/hotfix-booking/book", json=payload)
        assert r.status_code == 200, r.text
        booking = r.json()["booking"]
        assert booking["bookedBy"] == "Legacy User"
        assert booking.get("bookedByEmail") in (None, "")
