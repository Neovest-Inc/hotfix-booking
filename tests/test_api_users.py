"""Tests for POST /book's identity-from-session behavior (OAuth era).

The legacy GET /resolve-user endpoint was removed in Phase 4 — no code path
inside the app needs it any more (the OAuth callback populates the session
directly from Atlassian's /me profile).
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.conftest import load_fixture, read_bookings


class TestBookIdentityFromSession:
    """Under OAuth, /book takes identity from the signed session cookie.
    Client-sent `bookedBy` / `bookedByEmail` fields are IGNORED — no spoofing."""

    _valid = {
        "version": "9.94.23",
        "components": ["Alerts"],
        "clientEnvironments": ["CL001 - Fortress"],
    }

    def test_bookedBy_comes_from_session_not_payload(
        self, client: TestClient, mock_jira: respx.MockRouter, bookings_file: Path
    ) -> None:
        """Any `bookedBy` in the payload is ignored — the logged-in user wins."""
        from tests.conftest import login_as

        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json=load_fixture("search_deployed.json"))
        )
        login_as(client, "iqueiroz@neovest.com", "Ivan Queiroz")
        payload = {
            **self._valid,
            "bookedBy": "SPOOFED",           # ignored
            "bookedByEmail": "spoofed@x.com" # ignored
        }
        r = client.post("/api/hotfix-booking/book", json=payload)
        assert r.status_code == 200, r.text
        booking = r.json()["booking"]
        assert booking["bookedBy"] == "Ivan Queiroz"
        assert booking["bookedByEmail"] == "iqueiroz@neovest.com"

    def test_book_requires_authentication(
        self, anon_client: TestClient, mock_jira: respx.MockRouter
    ) -> None:
        """No session cookie → 401, even before Jira gets touched."""
        r = anon_client.post("/api/hotfix-booking/book", json=self._valid)
        assert r.status_code == 401

