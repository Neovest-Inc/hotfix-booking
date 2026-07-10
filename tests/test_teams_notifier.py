"""Unit tests for the Teams webhook notifier.

The notifier is best-effort: it must never raise, must silently no-op when
`TEAMS_WEBHOOK_URL` is unset, and must swallow any HTTP failure from Teams
so the caller (a booking / cancel handler) can complete successfully.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from hotfix_booking.config import Settings
from hotfix_booking.teams_notifier import (
    notify_booking_cancelled,
    notify_booking_created,
)

TEAMS_URL = "https://webhook.test/hotfix"


def _settings(url: str = TEAMS_URL, app_url: str = "") -> Settings:
    return Settings(
        jira_base_url="https://jira.test",
        jira_email="t@example.com",
        jira_api_token="x",
        bookings_file=Path("/tmp/hotfix-booking-tests.json"),
        client_context_id=1,
        port=3001,
        booking_retention_days=180,
        admin_emails=frozenset(),
        teams_webhook_url=url,
        app_base_url=app_url,
    )


def _booking() -> dict:
    return {
        "id": "HB-1",
        "version": "9.99.212",
        "components": ["Trading", "Reporting"],
        "clientEnvironments": ["CL001 - PROD-A", "CL002 - PROD-B"],
        "bookedBy": "Alice Smith",
        "bookedByEmail": "alice@example.com",
        "bookedAt": "2026-07-10T12:30:00+00:00",
        "description": "Fix null-pointer in order routing",
        "status": "booked",
        "parents": [],
        "originalParents": [],
        "rebaseHistory": [],
    }


def _cancelled() -> dict:
    b = _booking()
    b["status"] = "cancelled"
    b["cancelledBy"] = "Alice Smith"
    b["cancelledByEmail"] = "alice@example.com"
    b["cancelledAt"] = "2026-07-11T09:00:00+00:00"
    return b


def _card(request_bytes: bytes) -> dict:
    """Extract the Adaptive Card payload from a captured request."""
    body = json.loads(request_bytes)
    assert body["type"] == "message", body
    att = body["attachments"][0]
    assert att["contentType"] == "application/vnd.microsoft.card.adaptive", att
    return att["content"]


def _all_text(card: dict) -> str:
    """Flatten the card to a big string for substring assertions."""
    return json.dumps(card)


def _fact_titles(card: dict) -> list[str]:
    """All fact titles across every FactSet in the card body."""
    titles: list[str] = []
    for block in card.get("body", []):
        if block.get("type") == "FactSet":
            for f in block.get("facts", []):
                titles.append(f.get("title", ""))
    return titles


def _title_text(card: dict) -> str:
    """The first TextBlock's text — the card's headline."""
    for block in card.get("body", []):
        if block.get("type") == "TextBlock":
            return block.get("text", "")
    return ""


class TestNotifyBookingCreated:
    def test_posts_adaptive_card_wrapped_in_message_envelope(self) -> None:
        with respx.mock() as router:
            route = router.post(TEAMS_URL).mock(return_value=httpx.Response(200))
            notify_booking_created(_booking(), settings=_settings())
            assert route.called
            card = _card(route.calls.last.request.read())
            assert card["type"] == "AdaptiveCard"
            assert card["version"] == "1.5"

    def test_card_contains_version_booker_clients_components(self) -> None:
        with respx.mock() as router:
            route = router.post(TEAMS_URL).mock(return_value=httpx.Response(200))
            notify_booking_created(_booking(), settings=_settings())
            text = _all_text(_card(route.calls.last.request.read()))
            assert "9.99.212" in text
            assert "Alice Smith" in text
            assert "Trading" in text
            assert "Reporting" in text
            assert "PROD-A" in text
            assert "PROD-B" in text

    def test_no_op_when_webhook_url_unset(self) -> None:
        with respx.mock(assert_all_called=False) as router:
            router.post().mock(return_value=httpx.Response(200))
            notify_booking_created(_booking(), settings=_settings(url=""))
            assert not router.calls

    def test_swallows_500_response_from_teams(self) -> None:
        with respx.mock() as router:
            router.post(TEAMS_URL).mock(
                return_value=httpx.Response(500, text="oops")
            )
            notify_booking_created(_booking(), settings=_settings())  # no raise

    def test_swallows_transport_error(self) -> None:
        with respx.mock() as router:
            router.post(TEAMS_URL).mock(side_effect=httpx.ConnectError("boom"))
            notify_booking_created(_booking(), settings=_settings())  # no raise

    def test_swallows_timeout(self) -> None:
        with respx.mock() as router:
            router.post(TEAMS_URL).mock(side_effect=httpx.ReadTimeout("slow"))
            notify_booking_created(_booking(), settings=_settings())  # no raise

    def test_open_url_button_present_when_app_base_url_set(self) -> None:
        with respx.mock() as router:
            route = router.post(TEAMS_URL).mock(return_value=httpx.Response(200))
            notify_booking_created(
                _booking(),
                settings=_settings(app_url="https://hotfix.example.com"),
            )
            card = _card(route.calls.last.request.read())
            actions = card.get("actions", [])
            assert any(
                a.get("type") == "Action.OpenUrl"
                and "Open in Hotfix Booking tool" in a.get("title", "")
                and a.get("url", "").startswith("https://hotfix.example.com")
                for a in actions
            ), actions

    def test_open_url_button_omitted_when_app_base_url_unset(self) -> None:
        with respx.mock() as router:
            route = router.post(TEAMS_URL).mock(return_value=httpx.Response(200))
            notify_booking_created(_booking(), settings=_settings(app_url=""))
            card = _card(route.calls.last.request.read())
            assert not card.get("actions")

    def test_missing_optional_fields_still_produces_card(self) -> None:
        """A booking without an email/description shouldn't blow the notifier up."""
        b = _booking()
        b.pop("description", None)
        b["bookedByEmail"] = ""
        with respx.mock() as router:
            route = router.post(TEAMS_URL).mock(return_value=httpx.Response(200))
            notify_booking_created(b, settings=_settings())
            assert route.called

    def test_version_appears_in_title_not_in_facts(self) -> None:
        """Version is redundant when the headline already carries it."""
        with respx.mock() as router:
            route = router.post(TEAMS_URL).mock(return_value=httpx.Response(200))
            notify_booking_created(_booking(), settings=_settings())
            card = _card(route.calls.last.request.read())
            assert "9.99.212" in _title_text(card)
            assert "Version" not in _fact_titles(card)

    def test_booked_at_rendered_in_eastern_time_with_et_suffix(self) -> None:
        """UTC ISO timestamps in the record must be converted to America/New_York
        for display, matching the rest of the app's UI convention.

        2026-07-10T12:30:00Z is EDT (UTC-4) → 08:30 AM ET.
        """
        with respx.mock() as router:
            route = router.post(TEAMS_URL).mock(return_value=httpx.Response(200))
            b = _booking()
            b["bookedAt"] = "2026-07-10T12:30:00+00:00"
            notify_booking_created(b, settings=_settings())
            card = _card(route.calls.last.request.read())
            text = _all_text(card)
            # Suffix must be present
            assert " ET" in text
            # ET-converted time (08:30, EDT), not UTC (12:30)
            assert "08:30" in text
            assert "12:30" not in text

    def test_long_client_list_is_truncated_with_more_indicator(self) -> None:
        """A booking with more clients than the display cap should show the
        first N and an explicit '(+M more)' indicator — keeps the card compact
        without hiding the fact that more exist."""
        b = _booking()
        b["clientEnvironments"] = [f"CL{i:03d}" for i in range(1, 26)]  # 25 clients
        with respx.mock() as router:
            route = router.post(TEAMS_URL).mock(return_value=httpx.Response(200))
            notify_booking_created(b, settings=_settings())
            text = _all_text(_card(route.calls.last.request.read()))
            assert "CL001" in text
            assert "CL010" in text  # 10th shown
            assert "CL011" not in text  # 11th onward hidden
            assert "more" in text  # indicator present
            assert "15" in text  # 25 - 10 = 15 more

    def test_long_component_list_is_truncated(self) -> None:
        b = _booking()
        b["components"] = [f"Comp{i}" for i in range(1, 20)]  # 19 components
        with respx.mock() as router:
            route = router.post(TEAMS_URL).mock(return_value=httpx.Response(200))
            notify_booking_created(b, settings=_settings())
            text = _all_text(_card(route.calls.last.request.read()))
            assert "Comp1," in text or "Comp1 " in text
            assert "Comp11" not in text  # cap at 10
            assert "more" in text


class TestNotifyBookingCancelled:
    def test_posts_cancellation_card_with_affected_children(self) -> None:
        affected = [
            {
                "id": "HB-2",
                "version": "9.99.213",
                "bookedBy": "Bob Jones",
                "bookedByEmail": "bob@example.com",
                "previousParentVersions": ["9.99.212"],
                "newParentVersions": ["9.99.211"],
            },
            {
                "id": "HB-3",
                "version": "9.99.215",
                "bookedBy": "Carol Lee",
                "bookedByEmail": "carol@example.com",
                "previousParentVersions": ["9.99.212"],
                "newParentVersions": [],
            },
        ]
        with respx.mock() as router:
            route = router.post(TEAMS_URL).mock(return_value=httpx.Response(200))
            notify_booking_cancelled(_cancelled(), affected, settings=_settings())
            text = _all_text(_card(route.calls.last.request.read()))
            assert "9.99.212" in text
            assert "Alice Smith" in text
            # Both affected children mentioned
            assert "9.99.213" in text and "Bob Jones" in text
            assert "9.99.215" in text and "Carol Lee" in text
            # New parent versions shown
            assert "9.99.211" in text

    def test_no_affected_still_posts_card(self) -> None:
        with respx.mock() as router:
            route = router.post(TEAMS_URL).mock(return_value=httpx.Response(200))
            notify_booking_cancelled(_cancelled(), [], settings=_settings())
            assert route.called
            text = _all_text(_card(route.calls.last.request.read()))
            assert "9.99.212" in text

    def test_includes_active_cm_warning_when_present(self) -> None:
        with respx.mock() as router:
            route = router.post(TEAMS_URL).mock(return_value=httpx.Response(200))
            notify_booking_cancelled(
                _cancelled(),
                [],
                active_cm_warning={"cmKey": "CM-123", "status": "In Progress"},
                settings=_settings(),
            )
            text = _all_text(_card(route.calls.last.request.read()))
            assert "CM-123" in text
            assert "In Progress" in text

    def test_no_op_when_webhook_url_unset(self) -> None:
        with respx.mock(assert_all_called=False) as router:
            router.post().mock(return_value=httpx.Response(200))
            notify_booking_cancelled(_cancelled(), [], settings=_settings(url=""))
            assert not router.calls

    def test_swallows_500_response(self) -> None:
        with respx.mock() as router:
            router.post(TEAMS_URL).mock(return_value=httpx.Response(500))
            notify_booking_cancelled(_cancelled(), [], settings=_settings())  # no raise

    def test_swallows_transport_error(self) -> None:
        with respx.mock() as router:
            router.post(TEAMS_URL).mock(side_effect=httpx.ConnectError("boom"))
            notify_booking_cancelled(_cancelled(), [], settings=_settings())  # no raise

    def test_open_url_button_present_when_app_base_url_set(self) -> None:
        with respx.mock() as router:
            route = router.post(TEAMS_URL).mock(return_value=httpx.Response(200))
            notify_booking_cancelled(
                _cancelled(),
                [],
                settings=_settings(app_url="https://hotfix.example.com"),
            )
            card = _card(route.calls.last.request.read())
            actions = card.get("actions", [])
            assert any(
                a.get("type") == "Action.OpenUrl"
                and "Open in Hotfix Booking tool" in a.get("title", "")
                for a in actions
            )

    def test_version_appears_in_title_not_in_facts(self) -> None:
        with respx.mock() as router:
            route = router.post(TEAMS_URL).mock(return_value=httpx.Response(200))
            notify_booking_cancelled(_cancelled(), [], settings=_settings())
            card = _card(route.calls.last.request.read())
            assert "9.99.212" in _title_text(card)
            assert "Version" not in _fact_titles(card)

    def test_no_cancelled_by_fact_row(self) -> None:
        """`cancelledBy` is dropped from the card in the common case where it
        matches `bookedBy` (self-cancel) — see also the smart-display test
        below for the differ case."""
        with respx.mock() as router:
            route = router.post(TEAMS_URL).mock(return_value=httpx.Response(200))
            notify_booking_cancelled(_cancelled(), [], settings=_settings())
            card = _card(route.calls.last.request.read())
            titles = _fact_titles(card)
            assert "Cancelled by" not in titles
            # But the original booker is still surfaced
            assert "Originally booked by" in titles or "Booked by" in titles

    def test_cancelled_by_shown_when_different_from_booker(self) -> None:
        """Admin-cancel / CM-reporter-cancel edge case: someone other than
        the original booker closed the booking out. Surface that in the card
        so the chat has the audit trail. Case- and whitespace-insensitive on
        the comparison so trailing spaces / letter case don't hide it."""
        c = _cancelled()
        c["bookedBy"] = "Alice Smith"
        c["cancelledBy"] = "Admin User"
        with respx.mock() as router:
            route = router.post(TEAMS_URL).mock(return_value=httpx.Response(200))
            notify_booking_cancelled(c, [], settings=_settings())
            card = _card(route.calls.last.request.read())
            titles = _fact_titles(card)
            assert "Cancelled by" in titles
            text = _all_text(card)
            assert "Admin User" in text
            assert "Alice Smith" in text  # original booker still shown

    def test_cancelled_by_hidden_when_matches_booker_case_insensitive(self) -> None:
        c = _cancelled()
        c["bookedBy"] = "Alice Smith"
        c["cancelledBy"] = "  alice smith  "  # trailing spaces + lower case
        with respx.mock() as router:
            route = router.post(TEAMS_URL).mock(return_value=httpx.Response(200))
            notify_booking_cancelled(c, [], settings=_settings())
            card = _card(route.calls.last.request.read())
            assert "Cancelled by" not in _fact_titles(card)

    def test_cancelled_at_rendered_in_eastern_time_with_et_suffix(self) -> None:
        with respx.mock() as router:
            route = router.post(TEAMS_URL).mock(return_value=httpx.Response(200))
            c = _cancelled()
            c["cancelledAt"] = "2026-07-11T13:15:00+00:00"  # → 09:15 AM ET (EDT)
            notify_booking_cancelled(c, [], settings=_settings())
            text = _all_text(_card(route.calls.last.request.read()))
            assert " ET" in text
            assert "09:15" in text
            assert "13:15" not in text

    def test_downstream_wording_says_need_rebasing_and_rebase_on(self) -> None:
        """Section header and per-item wording changed to match reality:
        the downstream bookings HAVEN'T been rebased yet — their owners need
        to go rebase them."""
        affected = [
            {
                "id": "HB-2",
                "version": "9.99.213",
                "bookedBy": "Bob Jones",
                "bookedByEmail": "bob@example.com",
                "previousParentVersions": ["9.99.212"],
                "newParentVersions": ["9.99.211"],
            },
            {
                "id": "HB-3",
                "version": "9.99.215",
                "bookedBy": "Carol Lee",
                "bookedByEmail": "carol@example.com",
                "previousParentVersions": ["9.99.212"],
                "newParentVersions": [],  # baseline
            },
        ]
        with respx.mock() as router:
            route = router.post(TEAMS_URL).mock(return_value=httpx.Response(200))
            notify_booking_cancelled(_cancelled(), affected, settings=_settings())
            text = _all_text(_card(route.calls.last.request.read()))
            # New wording
            assert "need rebasing" in text.lower() or "need to be rebased" in text.lower()
            assert "rebase on" in text.lower()
            # Old wording removed
            assert "now based on" not in text.lower()
            # Baseline case
            assert "rebase on baseline" in text.lower()
