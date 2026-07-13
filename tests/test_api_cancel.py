"""End-to-end tests for POST /api/hotfix-booking/cancel.

Covers scenarios C1–C25 from the plan: authorization, rebase math against a
mutable DAG, chain / fan-out / multi-parent shapes, chronological rebaseHistory
accumulation, Jira active-CM warnings, cleanup interaction, backfill migration,
and response-contract snapshotting.
"""
from __future__ import annotations

import copy
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from hotfix_booking.config import Settings, reset_settings_for_tests
from tests.conftest import login_as, read_bookings, write_bookings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
ALICE_EMAIL = "alice@neovest.com"
BOB_EMAIL = "bob@neovest.com"
CAROL_EMAIL = "carol@neovest.com"
ADMIN_EMAIL = "admin@neovest.com"

_NAME_BY_EMAIL = {
    ALICE_EMAIL: "Alice",
    BOB_EMAIL: "Bob",
    CAROL_EMAIL: "Carol",
    ADMIN_EMAIL: "Admin User",
}


def _display_name(email: str) -> str:
    """Fall back to the local-part if we don't have a canonical name."""
    return _NAME_BY_EMAIL.get(email, email.split("@")[0])


def _cancel_as(
    client: TestClient, booking_id: str, email: str, **extra_payload
) -> httpx.Response:
    """Log in as `email` (name auto-derived) and POST /cancel with `booking_id`.

    Caller identity now comes from the session cookie — the old
    `cancelledByEmail` payload field is gone. `extra_payload` merges extra
    keys into the body (used by a few tests that pass stray fields).
    """
    login_as(client, email, _display_name(email))
    return client.post(
        "/api/hotfix-booking/cancel",
        json={"bookingId": booking_id, **extra_payload},
    )


def _user(name: str, email: str) -> dict:
    return {
        "accountId": f"acc-{name}",
        "displayName": name,
        "emailAddress": email,
        "active": True,
    }


def _users_by_email(email: str) -> list[dict]:
    """Return the mock Jira user record for a known test email."""
    known = {
        ALICE_EMAIL: _user("Alice", ALICE_EMAIL),
        BOB_EMAIL: _user("Bob", BOB_EMAIL),
        CAROL_EMAIL: _user("Carol", CAROL_EMAIL),
        ADMIN_EMAIL: _user("Admin User", ADMIN_EMAIL),
    }
    if email in known:
        return [known[email]]
    return []


def _mk_booking(
    *,
    id: str,
    version: str,
    components: list[str],
    clients: list[str],
    booker: str = "Alice",
    booker_email: str = ALICE_EMAIL,
    at: str = "2026-07-01T10:00:00+00:00",
    parents: list[str] | None = None,
    original_parents: list[str] | None = None,
    rebase_history: list[dict] | None = None,
    status: str = "booked",
) -> dict:
    return {
        "id": id,
        "version": version,
        "components": components,
        "clientEnvironments": clients,
        "bookedBy": booker,
        "bookedByEmail": booker_email,
        "bookedAt": at,
        "status": status,
        "parents": parents if parents is not None else [],
        "originalParents": original_parents if original_parents is not None else (parents or []),
        "rebaseHistory": rebase_history if rebase_history is not None else [],
    }


@pytest.fixture
def stub_jira_default(mock_jira: respx.MockRouter) -> respx.MockRouter:
    """Autouse-style helper — most cancel tests want: user lookup returns
    Alice/Bob/etc., and Jira CM search returns an empty list (no warning)."""
    def _user_search_side_effect(request):
        # Extract `query` param
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(str(request.url)).query)
        email = (qs.get("query") or [""])[0]
        return httpx.Response(200, json=_users_by_email(email))

    mock_jira.get("/rest/api/3/user/search").mock(side_effect=_user_search_side_effect)
    mock_jira.post("/rest/api/3/search/jql").mock(
        return_value=httpx.Response(200, json={"issues": []})
    )
    return mock_jira


# ---------------------------------------------------------------------------
# C1 — Cancel own booking with no children
# ---------------------------------------------------------------------------
class TestCancelBasic:
    def test_C1_owner_cancels_no_children(
        self, client: TestClient, bookings_file: Path, stub_jira_default
    ) -> None:
        write_bookings(bookings_file, [
            _mk_booking(id="HB-A", version="9.97.1",
                        components=["REST"], clients=["C1"]),
        ])
        r = _cancel_as(client, "HB-A", ALICE_EMAIL)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["affected"] == []
        assert body["activeCmWarning"] is None
        cancelled = body["cancelled"]
        assert cancelled["id"] == "HB-A"
        assert cancelled["status"] == "cancelled"
        assert cancelled["cancelledBy"] == "Alice"
        assert cancelled["cancelledByEmail"] == ALICE_EMAIL
        assert cancelled["cancelledAt"]
        # Persisted
        stored = read_bookings(bookings_file)
        assert stored[0]["status"] == "cancelled"


# ---------------------------------------------------------------------------
# C2, C4 — one direct child rebased to baseline
# ---------------------------------------------------------------------------
class TestCancelWithChild:
    def test_C2_C4_direct_child_rebased_to_baseline(
        self, client: TestClient, bookings_file: Path, stub_jira_default
    ) -> None:
        write_bookings(bookings_file, [
            _mk_booking(id="HB-A", version="9.97.1",
                        components=["REST"], clients=["C1"],
                        at="2026-07-01T10:00:00+00:00"),
            _mk_booking(id="HB-B", version="9.97.2",
                        components=["REST"], clients=["C1"],
                        at="2026-07-02T10:00:00+00:00",
                        parents=["HB-A"], original_parents=["HB-A"],
                        booker="Bob", booker_email=BOB_EMAIL),
        ])
        r = _cancel_as(client, "HB-A", ALICE_EMAIL)
        assert r.status_code == 200, r.text
        body = r.json()
        affected = body["affected"]
        assert len(affected) == 1
        assert affected[0]["id"] == "HB-B"
        assert affected[0]["bookedBy"] == "Bob"
        assert affected[0]["bookedByEmail"] == BOB_EMAIL
        assert affected[0]["previousParentVersions"] == ["9.97.1"]
        assert affected[0]["newParentVersions"] == []  # baseline

        # Persisted state
        stored = read_bookings(bookings_file)
        by_id = {b["id"]: b for b in stored}
        assert by_id["HB-A"]["status"] == "cancelled"
        assert by_id["HB-B"]["parents"] == []
        assert by_id["HB-B"]["originalParents"] == ["HB-A"]  # never mutated
        history = by_id["HB-B"]["rebaseHistory"]
        assert len(history) == 1
        event = history[0]
        assert event["cancelledBookingId"] == "HB-A"
        assert event["cancelledVersion"] == "9.97.1"
        assert event["cancelledBy"] == "Alice"
        assert event["previousParents"] == ["HB-A"]
        assert event["newParents"] == []
        assert event["previousParentVersions"] == ["9.97.1"]
        assert event["newParentVersions"] == []


# ---------------------------------------------------------------------------
# C3, C18, C20 — chain shapes: middle, grandchildren untouched, cancel-after-rebase
# ---------------------------------------------------------------------------
class TestCancelChain:
    def _chain(self) -> list[dict]:
        return [
            _mk_booking(id="HB-A", version="9.97.1",
                        components=["REST"], clients=["C1"],
                        at="2026-07-01T10:00:00+00:00"),
            _mk_booking(id="HB-B", version="9.97.2",
                        components=["REST"], clients=["C1"],
                        at="2026-07-02T10:00:00+00:00",
                        parents=["HB-A"], original_parents=["HB-A"],
                        booker="Bob", booker_email=BOB_EMAIL),
            _mk_booking(id="HB-C", version="9.97.3",
                        components=["REST"], clients=["C1"],
                        at="2026-07-03T10:00:00+00:00",
                        parents=["HB-B"], original_parents=["HB-B"],
                        booker="Carol", booker_email=CAROL_EMAIL),
        ]

    def test_C3_cancel_middle_rebases_grandchild_to_grandparent(
        self, client: TestClient, bookings_file: Path, stub_jira_default
    ) -> None:
        write_bookings(bookings_file, self._chain())
        # Cancel HB-B (owned by Bob). HB-C (currently based on HB-B) should
        # rebase to HB-A.
        r = _cancel_as(client, "HB-B", BOB_EMAIL)
        assert r.status_code == 200, r.text
        body = r.json()
        assert [a["id"] for a in body["affected"]] == ["HB-C"]
        assert body["affected"][0]["previousParentVersions"] == ["9.97.2"]
        assert body["affected"][0]["newParentVersions"] == ["9.97.1"]
        stored = {b["id"]: b for b in read_bookings(bookings_file)}
        assert stored["HB-C"]["parents"] == ["HB-A"]
        # Rebase history captured
        assert stored["HB-C"]["rebaseHistory"][0]["cancelledVersion"] == "9.97.2"

    def test_C18_grandchildren_untouched_when_cancelling_root(
        self, client: TestClient, bookings_file: Path, stub_jira_default
    ) -> None:
        write_bookings(bookings_file, self._chain())
        r = _cancel_as(client, "HB-A", ALICE_EMAIL)
        assert r.status_code == 200, r.text
        # Only HB-B is a DIRECT child (HB-C's current parent is HB-B, not HB-A).
        assert [a["id"] for a in r.json()["affected"]] == ["HB-B"]
        stored = {b["id"]: b for b in read_bookings(bookings_file)}
        # HB-B rebased to baseline
        assert stored["HB-B"]["parents"] == []
        # HB-C untouched: still based on HB-B, no new rebaseHistory entry
        assert stored["HB-C"]["parents"] == ["HB-B"]
        assert stored["HB-C"]["rebaseHistory"] == []

    def test_C20_cancel_of_a_booking_that_is_itself_rebased(
        self, client: TestClient, bookings_file: Path, stub_jira_default
    ) -> None:
        # Step 1: cancel HB-B. HB-C rebases to HB-A. HB-C.rebaseHistory has 1 entry.
        write_bookings(bookings_file, self._chain())
        _cancel_as(client, "HB-B", BOB_EMAIL)
        # Step 2: now cancel HB-A. HB-C (currently based on HB-A) rebases to baseline.
        r = _cancel_as(client, "HB-A", ALICE_EMAIL)
        assert r.status_code == 200, r.text
        assert [a["id"] for a in r.json()["affected"]] == ["HB-C"]
        stored = {b["id"]: b for b in read_bookings(bookings_file)}
        assert stored["HB-C"]["parents"] == []
        # Two rebase events, chronological
        history = stored["HB-C"]["rebaseHistory"]
        assert len(history) == 2
        assert history[0]["cancelledVersion"] == "9.97.2"
        assert history[1]["cancelledVersion"] == "9.97.1"
        # originalParents unchanged after both rebases
        assert stored["HB-C"]["originalParents"] == ["HB-B"]


# ---------------------------------------------------------------------------
# C5 — Fan-out (two children on disjoint cells)
# ---------------------------------------------------------------------------
class TestCancelFanOut:
    def test_C5_two_direct_children_rebased(
        self, client: TestClient, bookings_file: Path, stub_jira_default
    ) -> None:
        write_bookings(bookings_file, [
            # Alice's booking covers both cells.
            _mk_booking(id="HB-A", version="9.97.1",
                        components=["REST", "TM"], clients=["C1", "C2"],
                        at="2026-07-01T10:00:00+00:00"),
            # Bob's covers (C1, REST) only — child of A.
            _mk_booking(id="HB-B", version="9.97.2",
                        components=["REST"], clients=["C1"],
                        at="2026-07-02T10:00:00+00:00",
                        parents=["HB-A"], original_parents=["HB-A"],
                        booker="Bob", booker_email=BOB_EMAIL),
            # Carol's covers (C2, TM) only — child of A on disjoint cell.
            _mk_booking(id="HB-C", version="9.97.3",
                        components=["TM"], clients=["C2"],
                        at="2026-07-03T10:00:00+00:00",
                        parents=["HB-A"], original_parents=["HB-A"],
                        booker="Carol", booker_email=CAROL_EMAIL),
        ])
        r = _cancel_as(client, "HB-A", ALICE_EMAIL)
        assert r.status_code == 200
        affected_ids = {a["id"] for a in r.json()["affected"]}
        assert affected_ids == {"HB-B", "HB-C"}
        stored = {b["id"]: b for b in read_bookings(bookings_file)}
        assert stored["HB-B"]["parents"] == []
        assert stored["HB-C"]["parents"] == []
        assert len(stored["HB-B"]["rebaseHistory"]) == 1
        assert len(stored["HB-C"]["rebaseHistory"]) == 1


# ---------------------------------------------------------------------------
# C6 — Multi-parent (DAG): sequential cancels of both parents
# ---------------------------------------------------------------------------
class TestCancelMultiParent:
    def test_C6_multiparent_cancel_reduces_parents_step_by_step(
        self, client: TestClient, bookings_file: Path, stub_jira_default
    ) -> None:
        write_bookings(bookings_file, [
            # HB-A covers (C1, REST) — Alice
            _mk_booking(id="HB-A", version="9.97.1",
                        components=["REST"], clients=["C1"],
                        at="2026-07-01T10:00:00+00:00"),
            # HB-B covers (C1, TM) — Bob
            _mk_booking(id="HB-B", version="9.97.2",
                        components=["TM"], clients=["C1"],
                        at="2026-07-02T10:00:00+00:00",
                        booker="Bob", booker_email=BOB_EMAIL),
            # HB-C covers (C1, REST) + (C1, TM) → parents = [HB-B, HB-A]
            _mk_booking(id="HB-C", version="9.97.3",
                        components=["REST", "TM"], clients=["C1"],
                        at="2026-07-03T10:00:00+00:00",
                        parents=["HB-B", "HB-A"],
                        original_parents=["HB-B", "HB-A"],
                        booker="Carol", booker_email=CAROL_EMAIL),
        ])
        # Cancel HB-A: HB-C loses HB-A as parent, keeps HB-B.
        r = _cancel_as(client, "HB-A", ALICE_EMAIL)
        assert r.status_code == 200
        assert [a["id"] for a in r.json()["affected"]] == ["HB-C"]
        stored = {b["id"]: b for b in read_bookings(bookings_file)}
        assert stored["HB-C"]["parents"] == ["HB-B"]
        # Now cancel HB-B: HB-C rebases to baseline.
        r = _cancel_as(client, "HB-B", BOB_EMAIL)
        assert r.status_code == 200
        stored = {b["id"]: b for b in read_bookings(bookings_file)}
        assert stored["HB-C"]["parents"] == []
        # Two chronological rebase events, originalParents preserved
        history = stored["HB-C"]["rebaseHistory"]
        assert [e["cancelledVersion"] for e in history] == ["9.97.1", "9.97.2"]
        assert stored["HB-C"]["originalParents"] == ["HB-B", "HB-A"]


# ---------------------------------------------------------------------------
# C7 — Non-overlapping bookings untouched
# ---------------------------------------------------------------------------
class TestCancelUnrelatedUntouched:
    def test_C7_unrelated_booking_not_affected(
        self, client: TestClient, bookings_file: Path, stub_jira_default
    ) -> None:
        write_bookings(bookings_file, [
            _mk_booking(id="HB-A", version="9.97.1",
                        components=["REST"], clients=["C1"],
                        at="2026-07-01T10:00:00+00:00"),
            _mk_booking(id="HB-U", version="9.97.2",
                        components=["TM"], clients=["C2"],
                        at="2026-07-02T10:00:00+00:00",
                        booker="Bob", booker_email=BOB_EMAIL),
        ])
        r = _cancel_as(client, "HB-A", ALICE_EMAIL)
        assert r.status_code == 200
        assert r.json()["affected"] == []
        stored = {b["id"]: b for b in read_bookings(bookings_file)}
        assert stored["HB-U"]["rebaseHistory"] == []
        assert stored["HB-U"]["parents"] == []


# ---------------------------------------------------------------------------
# C21 — Cross-release-line isolation
# ---------------------------------------------------------------------------
class TestCrossReleaseLineIsolation:
    def test_C21_cancel_in_one_line_does_not_affect_another(
        self, client: TestClient, bookings_file: Path, stub_jira_default
    ) -> None:
        write_bookings(bookings_file, [
            _mk_booking(id="HB-97a", version="9.97.1",
                        components=["REST"], clients=["C1"],
                        at="2026-07-01T10:00:00+00:00"),
            # Same client + component but on 9.98.x — must be a separate DAG.
            _mk_booking(id="HB-98a", version="9.98.1",
                        components=["REST"], clients=["C1"],
                        at="2026-07-02T10:00:00+00:00",
                        booker="Bob", booker_email=BOB_EMAIL),
            _mk_booking(id="HB-98b", version="9.98.2",
                        components=["REST"], clients=["C1"],
                        at="2026-07-03T10:00:00+00:00",
                        parents=["HB-98a"], original_parents=["HB-98a"],
                        booker="Carol", booker_email=CAROL_EMAIL),
        ])
        r = _cancel_as(client, "HB-97a", ALICE_EMAIL)
        assert r.status_code == 200
        assert r.json()["affected"] == []
        stored = {b["id"]: b for b in read_bookings(bookings_file)}
        assert stored["HB-98b"]["parents"] == ["HB-98a"]
        assert stored["HB-98b"]["rebaseHistory"] == []


# ---------------------------------------------------------------------------
# C8 — Burn: next-version counts past cancelled records
# ---------------------------------------------------------------------------
class TestBurnedVersion:
    def test_C8_next_version_still_bumps_past_cancelled(
        self, client: TestClient, bookings_file: Path, stub_jira_default,
        mock_jira: respx.MockRouter,
    ) -> None:
        # Two bookings, cancel one, /next-version must still bump past both.
        write_bookings(bookings_file, [
            _mk_booking(id="HB-A", version="9.94.23",
                        components=["Alerts"], clients=["CL001"],
                        at="2026-07-01T10:00:00+00:00"),
        ])
        # /next-version uses the deployed_cms fixture (max 9.94.22).
        # After Alice books 9.94.23, next should be 9.94.24. Cancel 9.94.23 →
        # next remains 9.94.24 (burned).
        # Set up Jira search-jql to return the fixture so /next-version works.
        from tests.conftest import load_fixture
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json=load_fixture("search_deployed.json"))
        )
        r = _cancel_as(client, "HB-A", ALICE_EMAIL)
        assert r.status_code == 200
        # Now /next-version should return 9.94.24, NOT 9.94.23.
        r = client.get("/api/hotfix-booking/next-version")
        assert r.status_code == 200, r.text
        assert r.json()["nextVersion"] == "9.94.24"


# ---------------------------------------------------------------------------
# C9 — Authorization (owner, non-owner, admin)
# ---------------------------------------------------------------------------
class TestAuthorization:
    def test_C9_non_owner_non_admin_forbidden(
        self, client: TestClient, bookings_file: Path, stub_jira_default
    ) -> None:
        write_bookings(bookings_file, [
            _mk_booking(id="HB-A", version="9.97.1",
                        components=["REST"], clients=["C1"],
                        booker="Alice", booker_email=ALICE_EMAIL),
        ])
        r = _cancel_as(client, "HB-A", BOB_EMAIL)
        assert r.status_code == 403
        assert "only the booker" in r.json()["error"].lower()
        # Not cancelled
        stored = read_bookings(bookings_file)
        assert stored[0]["status"] == "booked"

    def test_C9_owner_email_case_insensitive(
        self, client: TestClient, bookings_file: Path, stub_jira_default
    ) -> None:
        """Booker's stored email may have different casing than the caller's
        session email — the ownership check must still match."""
        write_bookings(bookings_file, [
            _mk_booking(id="HB-A", version="9.97.1",
                        components=["REST"], clients=["C1"],
                        booker="Alice", booker_email="ALICE@neovest.com"),
        ])
        # Session carries the lowercase form (as Atlassian would return);
        # ownership match must be case-insensitive.
        r = _cancel_as(client, "HB-A", ALICE_EMAIL)
        assert r.status_code == 200, r.text

    def test_C9_admin_can_cancel_other_users_booking(
        self, client: TestClient, bookings_file: Path, settings: Settings,
        mock_jira: respx.MockRouter,
    ) -> None:
        # Add ADMIN_EMAIL to the admin allow-list; keep every other field
        # (including session_secret_key so login_as still works).
        import dataclasses
        reset_settings_for_tests(dataclasses.replace(
            settings, admin_emails=frozenset({ADMIN_EMAIL}),
        ))
        write_bookings(bookings_file, [
            _mk_booking(id="HB-A", version="9.97.1",
                        components=["REST"], clients=["C1"],
                        booker="Alice", booker_email=ALICE_EMAIL),
        ])
        # Mock: user search returns Admin user for admin email.
        def _user_side(request):
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(str(request.url)).query)
            email = (qs.get("query") or [""])[0]
            return httpx.Response(200, json=_users_by_email(email))
        mock_jira.get("/rest/api/3/user/search").mock(side_effect=_user_side)
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json={"issues": []})
        )
        r = _cancel_as(client, "HB-A", ADMIN_EMAIL)
        assert r.status_code == 200, r.text
        assert read_bookings(bookings_file)[0]["status"] == "cancelled"

    def _wire_user_search(self, mock_jira: respx.MockRouter) -> None:
        def _user_side(request):
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(str(request.url)).query)
            email = (qs.get("query") or [""])[0]
            return httpx.Response(200, json=_users_by_email(email))
        mock_jira.get("/rest/api/3/user/search").mock(side_effect=_user_side)

    def _wire_cm(
        self, mock_jira: respx.MockRouter, *, reporter: str, version: str = "9.97.1"
    ) -> None:
        mock_jira.post("/rest/api/3/search/jql").mock(return_value=httpx.Response(200, json={
            "issues": [{
                "key": "CM-1234",
                "fields": {
                    "summary": "hotfix",
                    "status": {"name": "In Progress"},
                    "components": [{"name": "REST"}],
                    "fixVersions": [{"name": version}],
                    "customfield_13235": [{"value": "C1"}],
                    "customfield_10751": None,
                    "reporter": {"displayName": reporter},
                }
            }]
        }))

    def test_C9_cm_reporter_can_cancel_other_users_booking(
        self, client: TestClient, bookings_file: Path, mock_jira: respx.MockRouter
    ) -> None:
        """Bob is the reporter of the Jira CM for 9.97.1. Alice booked it in
        the app. Bob should be able to cancel through the app because his
        Jira displayName ("Bob") matches the CM's reporter."""
        write_bookings(bookings_file, [
            _mk_booking(id="HB-A", version="9.97.1",
                        components=["REST"], clients=["C1"],
                        booker="Alice", booker_email=ALICE_EMAIL),
        ])
        self._wire_user_search(mock_jira)
        self._wire_cm(mock_jira, reporter="Bob")  # matches Bob's displayName
        r = _cancel_as(client, "HB-A", BOB_EMAIL)
        assert r.status_code == 200, r.text
        stored = read_bookings(bookings_file)
        assert stored[0]["status"] == "cancelled"
        # The cancel is attributed to Bob (the one who clicked cancel).
        assert stored[0]["cancelledBy"] == "Bob"
        # The active-CM warning still fires because the CM is In Progress.
        assert r.json()["activeCmWarning"] == {"cmKey": "CM-1234", "status": "In Progress"}

    def test_C9_cm_reporter_match_is_case_insensitive(
        self, client: TestClient, bookings_file: Path, mock_jira: respx.MockRouter
    ) -> None:
        write_bookings(bookings_file, [
            _mk_booking(id="HB-A", version="9.97.1",
                        components=["REST"], clients=["C1"],
                        booker="Alice", booker_email=ALICE_EMAIL),
        ])
        self._wire_user_search(mock_jira)
        self._wire_cm(mock_jira, reporter="  BOB  ")  # whitespace + case differ
        r = _cancel_as(client, "HB-A", BOB_EMAIL)
        assert r.status_code == 200, r.text

    def test_C9_non_reporter_non_owner_non_admin_still_forbidden(
        self, client: TestClient, bookings_file: Path, mock_jira: respx.MockRouter
    ) -> None:
        write_bookings(bookings_file, [
            _mk_booking(id="HB-A", version="9.97.1",
                        components=["REST"], clients=["C1"],
                        booker="Alice", booker_email=ALICE_EMAIL),
        ])
        self._wire_user_search(mock_jira)
        # CM exists but the reporter is somebody else entirely.
        self._wire_cm(mock_jira, reporter="Somebody Else")
        r = _cancel_as(client, "HB-A", BOB_EMAIL)
        assert r.status_code == 403
        assert "reporter" in r.json()["error"].lower()

    def test_C9_no_cm_at_all_falls_back_to_owner_only_check(
        self, client: TestClient, bookings_file: Path, mock_jira: respx.MockRouter
    ) -> None:
        """If Jira has no CM for this version, the reporter shortcut can't
        apply — Bob (non-owner, non-admin) must be rejected."""
        write_bookings(bookings_file, [
            _mk_booking(id="HB-A", version="9.97.1",
                        components=["REST"], clients=["C1"],
                        booker="Alice", booker_email=ALICE_EMAIL),
        ])
        self._wire_user_search(mock_jira)
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json={"issues": []})
        )
        r = _cancel_as(client, "HB-A", BOB_EMAIL)
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# C10 — Idempotency guard
# ---------------------------------------------------------------------------
class TestAlreadyCancelled:
    def test_C10_cancelling_twice_returns_409(
        self, client: TestClient, bookings_file: Path, stub_jira_default
    ) -> None:
        write_bookings(bookings_file, [
            _mk_booking(id="HB-A", version="9.97.1",
                        components=["REST"], clients=["C1"]),
        ])
        r1 = _cancel_as(client, "HB-A", ALICE_EMAIL)
        assert r1.status_code == 200
        r2 = _cancel_as(client, "HB-A", ALICE_EMAIL)
        assert r2.status_code == 409
        assert "already cancelled" in r2.json()["error"].lower()


# ---------------------------------------------------------------------------
# C11 — Active Jira CM warning
# ---------------------------------------------------------------------------
class TestActiveCmWarning:
    def _write_alice(self, bookings_file: Path) -> None:
        write_bookings(bookings_file, [
            _mk_booking(id="HB-A", version="9.97.1",
                        components=["REST"], clients=["C1"]),
        ])

    def _wire_jira(
        self, mock_jira: respx.MockRouter, *, cm_status: str | None
    ) -> None:
        """Mock the two Jira endpoints. `cm_status=None` → no CM at all."""
        def _user_side(request):
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(str(request.url)).query)
            email = (qs.get("query") or [""])[0]
            return httpx.Response(200, json=_users_by_email(email))

        mock_jira.get("/rest/api/3/user/search").mock(side_effect=_user_side)

        if cm_status is None:
            issues = []
        else:
            issues = [{
                "key": "CM-9999",
                "fields": {
                    "summary": "hotfix",
                    "status": {"name": cm_status},
                    "components": [{"name": "REST"}],
                    "fixVersions": [{"name": "9.97.1"}],
                    "customfield_13235": [{"value": "C1"}],
                    "customfield_10751": None,
                    "reporter": {"displayName": "Alice"},
                }
            }]
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json={"issues": issues})
        )

    @pytest.mark.parametrize("in_flight_status", [
        # In-flight statuses seen in the real Jira workflow. Any of these
        # firing the amber "Active CM in Jira" warning is the intended
        # behaviour — the CM is not yet settled, cancelling the local
        # booking behind Jira's back is a coordination risk.
        "Open",                   # someone claimed the version, working to deploy
        "In Progress",
        "DL Approved",            # Deployment Lead approved, still to deploy
        "Today's Deployments",
        "Business Approved",
        "QA Approved",
        # Case-insensitivity guard.
        "open", "IN PROGRESS", "dl approved",
    ])
    def test_C11_in_flight_cm_yields_warning(
        self, client: TestClient, bookings_file: Path,
        mock_jira: respx.MockRouter, in_flight_status: str,
    ) -> None:
        self._write_alice(bookings_file)
        self._wire_jira(mock_jira, cm_status=in_flight_status)
        r = _cancel_as(client, "HB-A", ALICE_EMAIL)
        assert r.status_code == 200, r.text
        warning = r.json()["activeCmWarning"]
        assert warning == {"cmKey": "CM-9999", "status": in_flight_status}

    @pytest.mark.parametrize("terminal_status", [
        # Positive terminals (CM already shipped / signed off):
        "Done",
        "Deployment Completed",
        "Global Review",  # post-deploy audit state — CM already deployed
        # Negative terminals (CM won't ship):
        "Rollback", "Rejected", "Cancelled",
        # Case-insensitive match.
        "DONE", "deployment completed", "GLOBAL REVIEW",
        "ROLLBACK", "rejected", "CANCELLED",
    ])
    def test_C11_terminal_status_yields_no_warning(
        self, client: TestClient, bookings_file: Path,
        mock_jira: respx.MockRouter, terminal_status: str,
    ) -> None:
        self._write_alice(bookings_file)
        self._wire_jira(mock_jira, cm_status=terminal_status)
        r = _cancel_as(client, "HB-A", ALICE_EMAIL)
        assert r.status_code == 200, r.text
        assert r.json()["activeCmWarning"] is None

    def test_C11_no_cm_at_all_no_warning(
        self, client: TestClient, bookings_file: Path, mock_jira: respx.MockRouter
    ) -> None:
        self._write_alice(bookings_file)
        self._wire_jira(mock_jira, cm_status=None)
        r = _cancel_as(client, "HB-A", ALICE_EMAIL)
        assert r.status_code == 200
        assert r.json()["activeCmWarning"] is None


# ---------------------------------------------------------------------------
# C12, C24 — Cleanup interaction
# ---------------------------------------------------------------------------
class TestCleanupInteraction:
    def test_C12_C24_cancelled_record_survives_deploy_based_cleanup(
        self, bookings_file: Path
    ) -> None:
        """Regression: if the cancelled version later appears as deployed in
        Jira, cleanup MUST NOT purge it — we want the audit trail plus the
        "cancelled locally, live CM in Jira" warning to keep showing.
        """
        from datetime import datetime, timezone
        from hotfix_booking.history import cleanup_bookings

        cancelled = _mk_booking(id="HB-A", version="9.97.1",
                                components=["REST"], clients=["C1"],
                                status="cancelled")
        active = _mk_booking(id="HB-B", version="9.97.2",
                             components=["REST"], clients=["C1"])
        kept, removed = cleanup_bookings(
            [cancelled, active],
            deployed={"9.97.1", "9.97.2"},  # both deployed now
            now=datetime.now(timezone.utc),
            retention_days=180,
        )
        # Only the non-cancelled record is purged by deploy cleanup.
        assert [b["id"] for b in kept] == ["HB-A"]
        assert [b["id"] for b in removed] == ["HB-B"]

    def test_C12_cancelled_record_STILL_purged_by_age(
        self, bookings_file: Path
    ) -> None:
        from datetime import datetime, timezone, timedelta
        from hotfix_booking.history import cleanup_bookings

        old_at = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        cancelled_old = _mk_booking(id="HB-A", version="9.97.1",
                                    components=["REST"], clients=["C1"],
                                    status="cancelled", at=old_at)
        kept, removed = cleanup_bookings(
            [cancelled_old],
            deployed=set(),
            now=datetime.now(timezone.utc),
            retention_days=180,
        )
        assert kept == []
        assert [b["id"] for b in removed] == ["HB-A"]


# ---------------------------------------------------------------------------
# C13, C19 — /history and /bookings expose the new fields
# ---------------------------------------------------------------------------
class TestHistoryAndBookingsShape:
    def test_C13_history_includes_cancelled_records_with_status(
        self, client: TestClient, bookings_file: Path, mock_jira: respx.MockRouter
    ) -> None:
        write_bookings(bookings_file, [
            _mk_booking(id="HB-A", version="9.97.1",
                        components=["REST"], clients=["C1"],
                        status="cancelled"),
            _mk_booking(id="HB-B", version="9.97.2",
                        components=["REST"], clients=["C1"]),
        ])
        # /history hits Jira; return no CMs so only the local bookings show.
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json={"issues": []})
        )
        r = client.get("/api/hotfix-booking/history?major=9&minor=97")
        assert r.status_code == 200, r.text
        hotfixes = r.json()["hotfixes"]
        by_ver = {h["version"]: h for h in hotfixes}
        assert by_ver["9.97.1"]["status"] == "Cancelled"
        assert by_ver["9.97.1"]["bookingStatus"] == "cancelled"
        assert by_ver["9.97.2"]["status"] == "Booked"

    def test_C19_bookings_response_carries_parents_and_history_fields(
        self, client: TestClient, bookings_file: Path
    ) -> None:
        write_bookings(bookings_file, [
            _mk_booking(id="HB-A", version="9.97.1",
                        components=["REST"], clients=["C1"],
                        parents=["HB-OLD"], original_parents=["HB-OLD"],
                        rebase_history=[{"cancelledVersion": "9.96.5"}]),
        ])
        r = client.get("/api/hotfix-booking/bookings")
        assert r.status_code == 200
        b = r.json()["bookings"][0]
        assert b["parents"] == ["HB-OLD"]
        assert b["originalParents"] == ["HB-OLD"]
        assert b["rebaseHistory"][0]["cancelledVersion"] == "9.96.5"
        assert b["status"] == "booked"

    def test_C19_history_response_carries_rebase_fields(
        self, client: TestClient, bookings_file: Path, mock_jira: respx.MockRouter
    ) -> None:
        write_bookings(bookings_file, [
            _mk_booking(id="HB-A", version="9.97.1",
                        components=["REST"], clients=["C1"],
                        rebase_history=[{"cancelledVersion": "old"}]),
        ])
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json={"issues": []})
        )
        r = client.get("/api/hotfix-booking/history?major=9&minor=97")
        assert r.status_code == 200
        h = r.json()["hotfixes"][0]
        assert h["parents"] == []
        assert h["originalParents"] == []
        assert h["rebaseHistory"] == [{"cancelledVersion": "old"}]


# ---------------------------------------------------------------------------
# C14 — Race smoke test (store lock serializes cancels)
# ---------------------------------------------------------------------------
class TestRaceSmoke:
    def test_C14_serial_cancels_produce_deterministic_state(
        self, client: TestClient, bookings_file: Path, stub_jira_default
    ) -> None:
        # Two rapid cancels should each see the effects of the previous.
        write_bookings(bookings_file, [
            _mk_booking(id="HB-A", version="9.97.1",
                        components=["REST"], clients=["C1"]),
            _mk_booking(id="HB-B", version="9.97.2",
                        components=["REST"], clients=["C1"],
                        at="2026-07-02T10:00:00+00:00",
                        parents=["HB-A"], original_parents=["HB-A"],
                        booker="Bob", booker_email=BOB_EMAIL),
        ])
        r1 = _cancel_as(client, "HB-A", ALICE_EMAIL)
        r2 = _cancel_as(client, "HB-B", BOB_EMAIL)
        assert r1.status_code == 200 and r2.status_code == 200
        stored = {b["id"]: b for b in read_bookings(bookings_file)}
        assert stored["HB-A"]["status"] == "cancelled"
        assert stored["HB-B"]["status"] == "cancelled"


# ---------------------------------------------------------------------------
# C15 — Backfill migration + cancel
# ---------------------------------------------------------------------------
class TestBackfillThenCancel:
    def test_C15_cancel_works_on_legacy_file_after_backfill(
        self, client: TestClient, bookings_file: Path, stub_jira_default
    ) -> None:
        # Write LEGACY records (no parents / originalParents / rebaseHistory).
        import json as _json
        legacy = [
            {"id": "HB-A", "version": "9.97.1",
             "components": ["REST"], "clientEnvironments": ["C1"],
             "bookedBy": "Alice", "bookedByEmail": ALICE_EMAIL,
             "bookedAt": "2026-07-01T10:00:00+00:00", "status": "booked"},
            {"id": "HB-B", "version": "9.97.2",
             "components": ["REST"], "clientEnvironments": ["C1"],
             "bookedBy": "Bob", "bookedByEmail": BOB_EMAIL,
             "bookedAt": "2026-07-02T10:00:00+00:00", "status": "booked"},
        ]
        bookings_file.write_text(_json.dumps({"bookings": legacy}, indent=2),
                                 encoding="utf-8")
        # Cancel HB-A. HB-B should get rebased to baseline (backfill established
        # HB-B.parents = ["HB-A"] first).
        r = _cancel_as(client, "HB-A", ALICE_EMAIL)
        assert r.status_code == 200, r.text
        assert [a["id"] for a in r.json()["affected"]] == ["HB-B"]
        stored = {b["id"]: b for b in read_bookings(bookings_file)}
        assert stored["HB-B"]["parents"] == []
        # Backfill also populated originalParents from the migrated parents.
        assert stored["HB-B"]["originalParents"] == ["HB-A"]
        assert len(stored["HB-B"]["rebaseHistory"]) == 1


# ---------------------------------------------------------------------------
# C16 — originalParents preserved
# ---------------------------------------------------------------------------
class TestOriginalParentsImmutable:
    def test_C16_originalParents_unchanged_after_multiple_rebases(
        self, client: TestClient, bookings_file: Path, stub_jira_default
    ) -> None:
        write_bookings(bookings_file, [
            _mk_booking(id="HB-A", version="9.97.1",
                        components=["REST"], clients=["C1"],
                        at="2026-07-01T10:00:00+00:00"),
            _mk_booking(id="HB-B", version="9.97.2",
                        components=["TM"], clients=["C1"],
                        at="2026-07-02T10:00:00+00:00",
                        booker="Bob", booker_email=BOB_EMAIL),
            _mk_booking(id="HB-C", version="9.97.3",
                        components=["REST", "TM"], clients=["C1"],
                        at="2026-07-03T10:00:00+00:00",
                        parents=["HB-B", "HB-A"],
                        original_parents=["HB-B", "HB-A"],
                        booker="Carol", booker_email=CAROL_EMAIL),
        ])
        # Cancel A then B
        _cancel_as(client, "HB-A", ALICE_EMAIL)
        _cancel_as(client, "HB-B", BOB_EMAIL)
        stored = {b["id"]: b for b in read_bookings(bookings_file)}
        assert stored["HB-C"]["parents"] == []
        assert stored["HB-C"]["originalParents"] == ["HB-B", "HB-A"]


# ---------------------------------------------------------------------------
# C22 — Legacy record without bookedByEmail
# ---------------------------------------------------------------------------
class TestMissingBookedByEmail:
    def test_C22_no_email_non_admin_forbidden(
        self, client: TestClient, bookings_file: Path, stub_jira_default
    ) -> None:
        write_bookings(bookings_file, [
            _mk_booking(id="HB-A", version="9.97.1",
                        components=["REST"], clients=["C1"],
                        booker="Legacy Person", booker_email=""),
        ])
        r = _cancel_as(client, "HB-A", ALICE_EMAIL)
        assert r.status_code == 403

    def test_C22_no_email_admin_can_cancel(
        self, client: TestClient, bookings_file: Path, settings: Settings,
        mock_jira: respx.MockRouter,
    ) -> None:
        import dataclasses
        reset_settings_for_tests(dataclasses.replace(
            settings, admin_emails=frozenset({ADMIN_EMAIL}),
        ))
        write_bookings(bookings_file, [
            _mk_booking(id="HB-A", version="9.97.1",
                        components=["REST"], clients=["C1"],
                        booker="Legacy Person", booker_email=""),
        ])
        def _user_side(request):
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(str(request.url)).query)
            email = (qs.get("query") or [""])[0]
            return httpx.Response(200, json=_users_by_email(email))
        mock_jira.get("/rest/api/3/user/search").mock(side_effect=_user_side)
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json={"issues": []})
        )
        r = _cancel_as(client, "HB-A", ADMIN_EMAIL)
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# C23 — Body validation
# ---------------------------------------------------------------------------
class TestBodyValidation:
    def test_C23_missing_bookingId(self, client: TestClient, stub_jira_default) -> None:
        login_as(client, ALICE_EMAIL, "Alice")
        r = client.post(
            "/api/hotfix-booking/cancel",
            json={},
        )
        assert r.status_code == 400
        assert "bookingId" in r.json()["error"]

    def test_C23_missing_session_returns_401(
        self, anon_client: TestClient
    ) -> None:
        """Under OAuth, caller identity must come from the session cookie.
        No session → 401 (not 400)."""
        r = anon_client.post(
            "/api/hotfix-booking/cancel",
            json={"bookingId": "HB-x"},
        )
        assert r.status_code == 401

    def test_C23_unknown_booking_id(
        self, client: TestClient, bookings_file: Path, stub_jira_default
    ) -> None:
        write_bookings(bookings_file, [])
        r = _cancel_as(client, "HB-nope", ALICE_EMAIL)
        assert r.status_code == 404
        assert "not found" in r.json()["error"].lower()


# ---------------------------------------------------------------------------
# Regression: the CM-only cancel path was intentionally NOT shipped.
# `/cancel` operates on local bookings — Jira CMs must be cancelled in Jira.
# These tests document the boundary so a well-meaning refactor doesn't
# quietly resurrect the tombstone flow.
# ---------------------------------------------------------------------------
class TestCancelDoesNotAcceptCmKey:
    def test_cm_key_alone_is_rejected_as_missing_bookingId(
        self, client: TestClient, bookings_file: Path, stub_jira_default
    ) -> None:
        login_as(client, ALICE_EMAIL, "Alice")
        r = client.post(
            "/api/hotfix-booking/cancel",
            json={"cmKey": "CM-42"},
        )
        assert r.status_code == 400
        # Error should still name bookingId — the endpoint has one shape.
        assert "bookingId" in r.json()["error"]

    def test_bookingId_with_stray_cmKey_ignored_and_still_works(
        self, client: TestClient, bookings_file: Path, stub_jira_default
    ) -> None:
        """A well-formed cancel is not broken by an unexpected `cmKey` field.
        We silently ignore unknown fields so stale clients degrade gracefully."""
        write_bookings(bookings_file, [
            _mk_booking(id="HB-A", version="9.97.1",
                        components=["REST"], clients=["C1"]),
        ])
        r = _cancel_as(client, "HB-A", ALICE_EMAIL, cmKey="CM-999")
        assert r.status_code == 200, r.text
        assert r.json()["cancelled"]["id"] == "HB-A"
# ---------------------------------------------------------------------------
# C25 — Response contract snapshot
# ---------------------------------------------------------------------------
class TestResponseContract:
    def test_C25_response_shape_stable(
        self, client: TestClient, bookings_file: Path, stub_jira_default
    ) -> None:
        write_bookings(bookings_file, [
            _mk_booking(id="HB-A", version="9.97.1",
                        components=["REST"], clients=["C1"]),
            _mk_booking(id="HB-B", version="9.97.2",
                        components=["REST"], clients=["C1"],
                        parents=["HB-A"], original_parents=["HB-A"],
                        booker="Bob", booker_email=BOB_EMAIL),
        ])
        r = _cancel_as(client, "HB-A", ALICE_EMAIL)
        assert r.status_code == 200
        body = r.json()
        # Top-level keys
        assert set(body.keys()) == {"cancelled", "affected", "activeCmWarning"}
        # Cancelled record has expected fields
        cancelled_keys = set(body["cancelled"].keys())
        for required in ("id", "version", "status", "cancelledAt",
                         "cancelledBy", "cancelledByEmail",
                         "parents", "originalParents", "rebaseHistory"):
            assert required in cancelled_keys, f"missing {required} in cancelled"
        # Affected entries have their expected fields
        affected_keys = set(body["affected"][0].keys())
        for required in ("id", "version", "bookedBy", "bookedByEmail",
                         "previousParentVersions", "newParentVersions"):
            assert required in affected_keys, f"missing {required} in affected[0]"


# ---------------------------------------------------------------------------
# Cancel rebases into a Jira CM parent (created outside the app)
# ---------------------------------------------------------------------------
class TestCancelRebasesOntoJiraCmPriors:
    """When a booking is cancelled and its child needs to be rebased, the
    recomputation must consider Jira CMs on the same release line — the same
    priors that would have been considered at initial booking time. Otherwise
    a rebase could silently drop a valid Jira-CM parent and rebase the child
    all the way down to baseline.
    """

    def _wire_cms(
        self, mock_jira: respx.MockRouter, cms: list[dict]
    ) -> None:
        def _user_side(request):
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(str(request.url)).query)
            email = (qs.get("query") or [""])[0]
            return httpx.Response(200, json=_users_by_email(email))
        mock_jira.get("/rest/api/3/user/search").mock(side_effect=_user_side)
        mock_jira.post("/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json={"issues": cms})
        )

    def _cm_issue(self, key: str, version: str, components: list[str],
                  clients: list[str], created: str = "2026-06-01T09:00:00.000+0000",
                  status: str = "Deployment Completed") -> dict:
        return {
            "key": key,
            "fields": {
                "summary": f"Hotfix {version}",
                "status": {"name": status},
                "components": [{"name": c} for c in components],
                "fixVersions": [{"name": version}],
                "customfield_13235": [{"value": c} for c in clients],
                "customfield_10751": "2026-06-01",
                "reporter": {"displayName": "Someone"},
                "created": created,
            },
        }

    def test_cancel_rebases_child_onto_jira_cm_instead_of_baseline(
        self, client: TestClient, bookings_file: Path, mock_jira: respx.MockRouter
    ) -> None:
        # Setup: Jira CM CM-9812 (9.98.12, outside app) + two local bookings
        # HB-A (9.98.601) on top of CM-9812, HB-B (9.98.602) on top of HB-A.
        # Cancel HB-A → HB-B should rebase to CM-9812 (not baseline 9.98.0).
        write_bookings(bookings_file, [
            _mk_booking(id="HB-A", version="9.98.601",
                        components=["REST"], clients=["C1"],
                        at="2026-07-01T10:00:00+00:00",
                        parents=["jira:CM-9812"],
                        original_parents=["jira:CM-9812"]),
            _mk_booking(id="HB-B", version="9.98.602",
                        components=["REST"], clients=["C1"],
                        at="2026-07-02T10:00:00+00:00",
                        parents=["HB-A"], original_parents=["HB-A"],
                        booker="Bob", booker_email=BOB_EMAIL),
        ])
        self._wire_cms(mock_jira, [
            self._cm_issue("CM-9812", "9.98.12",
                           components=["REST"], clients=["C1"]),
        ])
        r = _cancel_as(client, "HB-A", ALICE_EMAIL)
        assert r.status_code == 200, r.text
        body = r.json()
        # HB-B affected; new parent must be the CM, not baseline.
        assert [a["id"] for a in body["affected"]] == ["HB-B"]
        assert body["affected"][0]["previousParentVersions"] == ["9.98.601"]
        assert body["affected"][0]["newParentVersions"] == ["9.98.12"]
        stored = {b["id"]: b for b in read_bookings(bookings_file)}
        assert stored["HB-B"]["parents"] == ["jira:CM-9812"]
