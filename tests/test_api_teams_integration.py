"""End-to-end: /book and /cancel post to the Teams webhook when configured,
and always succeed regardless of the Teams webhook's response.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from hotfix_booking.config import Settings, reset_settings_for_tests
from tests.conftest import load_fixture, login_as, write_bookings

TEAMS_URL = "https://webhook.test/hotfix"


def _enable_teams(settings: Settings, url: str = TEAMS_URL) -> Settings:
    """Swap the module-level settings singleton for one with Teams turned on."""
    s = replace(settings, teams_webhook_url=url)
    reset_settings_for_tests(s)
    return s


ALICE_EMAIL = "alice@example.com"


def _user(name: str, email: str) -> dict:
    return {
        "accountId": f"acc-{name}",
        "displayName": name,
        "emailAddress": email,
        "active": True,
    }


def _users_by_email(email: str) -> list[dict]:
    known = {ALICE_EMAIL: _user("Alice", ALICE_EMAIL)}
    return [known[email]] if email in known else []


# ---------------------------------------------------------------------------
# POST /book
# ---------------------------------------------------------------------------
class TestBookNotification:
    _valid = {
        "version": "9.94.23",
        "components": ["Alerts"],
        "clientEnvironments": ["CL001 - Fortress"],
        "bookedBy": "Alice",
    }

    @pytest.fixture(autouse=True)
    def _stub_jira(self, mock_jira: respx.MockRouter) -> respx.MockRouter:
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(
                200, json=load_fixture("search_deployed.json")
            )
        )
        return mock_jira

    def test_book_posts_to_teams_when_url_set(
        self, client: TestClient, settings: Settings, mock_jira: respx.MockRouter
    ) -> None:
        _enable_teams(settings)
        teams_route = mock_jira.post(TEAMS_URL).mock(
            return_value=httpx.Response(200)
        )
        r = client.post("/api/hotfix-booking/book", json=self._valid)
        assert r.status_code == 200, r.text
        assert teams_route.called
        body = json.loads(teams_route.calls.last.request.read())
        card = body["attachments"][0]["content"]
        text = json.dumps(card)
        assert "9.94.23" in text
        assert "Alerts" in text
        assert "CL001 - Fortress" in text

    def test_book_still_succeeds_when_teams_returns_500(
        self, client: TestClient, settings: Settings, mock_jira: respx.MockRouter
    ) -> None:
        _enable_teams(settings)
        teams_route = mock_jira.post(TEAMS_URL).mock(
            return_value=httpx.Response(500, text="teams unavailable")
        )
        r = client.post("/api/hotfix-booking/book", json=self._valid)
        assert r.status_code == 200, r.text
        assert r.json()["success"] is True
        assert teams_route.called  # we did try

    def test_book_still_succeeds_when_teams_transport_fails(
        self, client: TestClient, settings: Settings, mock_jira: respx.MockRouter
    ) -> None:
        _enable_teams(settings)
        mock_jira.post(TEAMS_URL).mock(side_effect=httpx.ConnectError("dns"))
        r = client.post("/api/hotfix-booking/book", json=self._valid)
        assert r.status_code == 200, r.text

    def test_book_does_not_call_teams_when_url_unset(
        self, client: TestClient, settings: Settings, mock_jira: respx.MockRouter
    ) -> None:
        # settings fixture leaves teams_webhook_url = "" — DO NOT enable Teams.
        # If the notifier tried to POST, respx would raise (nothing registered
        # for webhook.test), so a green response is proof of no-op.
        r = client.post("/api/hotfix-booking/book", json=self._valid)
        assert r.status_code == 200, r.text

    def test_book_does_not_post_to_teams_on_validation_failure(
        self, client: TestClient, settings: Settings, mock_jira: respx.MockRouter
    ) -> None:
        """A failed booking (400 / 409) must never trigger a Teams post."""
        _enable_teams(settings)
        teams_route = mock_jira.post(TEAMS_URL).mock(
            return_value=httpx.Response(200)
        )
        bad = {**self._valid, "components": []}
        r = client.post("/api/hotfix-booking/book", json=bad)
        assert r.status_code == 400
        assert not teams_route.called


# ---------------------------------------------------------------------------
# POST /cancel
# ---------------------------------------------------------------------------
def _mk_booking(
    *,
    id: str,
    version: str,
    booker: str = "Alice",
    booker_email: str = ALICE_EMAIL,
    parents: list[str] | None = None,
) -> dict:
    return {
        "id": id,
        "version": version,
        "components": ["REST"],
        "clientEnvironments": ["C1"],
        "bookedBy": booker,
        "bookedByEmail": booker_email,
        "bookedAt": "2026-07-01T10:00:00+00:00",
        "status": "booked",
        "parents": parents or [],
        "originalParents": parents or [],
        "rebaseHistory": [],
    }


class TestCancelNotification:
    @pytest.fixture(autouse=True)
    def _stub_jira(self, mock_jira: respx.MockRouter) -> respx.MockRouter:
        def _user_search(request):
            from urllib.parse import parse_qs, urlparse

            qs = parse_qs(urlparse(str(request.url)).query)
            email = (qs.get("query") or [""])[0]
            return httpx.Response(200, json=_users_by_email(email))

        mock_jira.get("/rest/api/3/user/search").mock(side_effect=_user_search)
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json={"issues": []})
        )
        return mock_jira

    def test_cancel_posts_to_teams_when_url_set(
        self,
        client: TestClient,
        settings: Settings,
        bookings_file: Path,
        mock_jira: respx.MockRouter,
    ) -> None:
        _enable_teams(settings)
        write_bookings(bookings_file, [_mk_booking(id="HB-A", version="9.97.1")])
        teams_route = mock_jira.post(TEAMS_URL).mock(
            return_value=httpx.Response(200)
        )
        login_as(client, ALICE_EMAIL, "Alice")
        r = client.post(
            "/api/hotfix-booking/cancel",
            json={"bookingId": "HB-A"},
        )
        assert r.status_code == 200, r.text
        assert teams_route.called
        text = json.dumps(
            json.loads(teams_route.calls.last.request.read())
        )
        assert "9.97.1" in text
        assert "Alice" in text

    def test_cancel_still_succeeds_when_teams_returns_500(
        self,
        client: TestClient,
        settings: Settings,
        bookings_file: Path,
        mock_jira: respx.MockRouter,
    ) -> None:
        _enable_teams(settings)
        write_bookings(bookings_file, [_mk_booking(id="HB-A", version="9.97.1")])
        mock_jira.post(TEAMS_URL).mock(return_value=httpx.Response(500))
        login_as(client, ALICE_EMAIL, "Alice")
        r = client.post(
            "/api/hotfix-booking/cancel",
            json={"bookingId": "HB-A"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["cancelled"]["status"] == "cancelled"

    def test_cancel_does_not_call_teams_when_url_unset(
        self,
        client: TestClient,
        settings: Settings,
        bookings_file: Path,
        mock_jira: respx.MockRouter,
    ) -> None:
        write_bookings(bookings_file, [_mk_booking(id="HB-A", version="9.97.1")])
        # No _enable_teams; if the notifier tried to POST, respx would raise.
        login_as(client, ALICE_EMAIL, "Alice")
        r = client.post(
            "/api/hotfix-booking/cancel",
            json={"bookingId": "HB-A"},
        )
        assert r.status_code == 200, r.text

    def test_cancel_does_not_post_on_auth_failure(
        self,
        client: TestClient,
        settings: Settings,
        bookings_file: Path,
        mock_jira: respx.MockRouter,
    ) -> None:
        """A rejected cancel (403) must not trigger a Teams post."""
        _enable_teams(settings)
        write_bookings(
            bookings_file,
            [_mk_booking(id="HB-A", version="9.97.1", booker="Alice")],
        )
        teams_route = mock_jira.post(TEAMS_URL).mock(
            return_value=httpx.Response(200)
        )
        # Log in as a stranger — not the owner, not an admin, not a CM reporter → 403.
        login_as(client, "stranger@example.com", "Stranger")
        r = client.post(
            "/api/hotfix-booking/cancel",
            json={"bookingId": "HB-A"},
        )
        assert r.status_code == 403
        assert not teams_route.called
