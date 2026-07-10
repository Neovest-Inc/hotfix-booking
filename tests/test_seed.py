"""Tests for the ``tools/seed.py`` dev script.

Covers the two behaviors that matter beyond "it writes JSON":
  1. ``build_seed`` accepts a ``(display_name, email)`` tuple and applies both
     verbatim to the "your own" rows (J and L scenarios).
  2. ``resolve_user_or_error`` calls Jira's user-search endpoint and either
     returns ``(displayName, email)`` or raises a ``RuntimeError`` describing
     why. This is what protects the seed from silently attributing rows to an
     email that doesn't correspond to a real Jira account.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
import respx

# tools/ isn't a package; add it to sys.path so we can import seed directly.
TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import seed  # noqa: E402

from hotfix_booking.config import Settings, reset_settings_for_tests  # noqa: E402

TEST_JIRA_BASE = "https://jira.test"


@pytest.fixture
def seed_settings(tmp_path: Path) -> Settings:
    s = Settings(
        jira_base_url=TEST_JIRA_BASE,
        jira_email="test@example.com",
        jira_api_token="secret",
        bookings_file=tmp_path / "hotfix-bookings.json",
        client_context_id=14042,
        port=3001,
        booking_retention_days=180,
        admin_emails=frozenset(),
    )
    reset_settings_for_tests(s)
    return s


def test_build_seed_no_user_omits_user_owned_rows() -> None:
    records = seed.build_seed(None)
    # No record should carry the literal "You" name — that footgun is gone.
    assert all(r["bookedBy"] != "You" for r in records)
    # J and L scenarios only exist when a user is provided.
    assert not any(r["id"] == "HB-SEED-540" for r in records)
    assert not any(r["id"] == "HB-SEED-541" for r in records)
    assert not any(r["id"] == "HB-SEED-CM-COLLIDE" for r in records)


def test_build_seed_with_user_tuple_uses_display_name_and_email() -> None:
    user = ("Georgios Zacharis", "gzacharis@neovest.com")
    records = seed.build_seed(user)
    mine = [r for r in records if r["bookedByEmail"] == user[1]]
    # J (540, 541) + L (9.98.1) = 3 rows attributed to the current user.
    assert len(mine) == 3
    for r in mine:
        assert r["bookedBy"] == "Georgios Zacharis"
        assert r["bookedByEmail"] == "gzacharis@neovest.com"


def test_resolve_user_or_error_returns_display_name_for_real_user(
    seed_settings: Settings,
) -> None:
    email = "gzacharis@neovest.com"
    with respx.mock(base_url=TEST_JIRA_BASE, assert_all_called=True) as router:
        router.get("/rest/api/3/user/search").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "accountId": "abc123",
                        "displayName": "Georgios Zacharis",
                        "emailAddress": email,
                        "active": True,
                    },
                    # Jira also returns service-account stubs — must be filtered.
                    {
                        "accountId": "qm:zzz",
                        "displayName": "qm-service",
                        "emailAddress": "",
                        "active": True,
                    },
                ],
            )
        )
        result = seed.resolve_user_or_error(email)
    assert result == ("Georgios Zacharis", email)


def test_resolve_user_or_error_raises_when_jira_returns_no_match(
    seed_settings: Settings,
) -> None:
    email = "nobody@example.com"
    with respx.mock(base_url=TEST_JIRA_BASE, assert_all_called=True) as router:
        router.get("/rest/api/3/user/search").mock(
            return_value=httpx.Response(200, json=[])
        )
        with pytest.raises(RuntimeError, match="No active Jira user"):
            seed.resolve_user_or_error(email)


def test_resolve_user_or_error_raises_when_only_service_stubs_returned(
    seed_settings: Settings,
) -> None:
    email = "someone@example.com"
    with respx.mock(base_url=TEST_JIRA_BASE, assert_all_called=True) as router:
        router.get("/rest/api/3/user/search").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "accountId": "qm:zzz",
                        "displayName": "qm-service",
                        "emailAddress": "",
                        "active": True,
                    },
                ],
            )
        )
        with pytest.raises(RuntimeError, match="No active Jira user"):
            seed.resolve_user_or_error(email)


def test_resolve_user_or_error_raises_when_credentials_missing(
    tmp_path: Path,
) -> None:
    s = Settings(
        jira_base_url="",
        jira_email="",
        jira_api_token="",
        bookings_file=tmp_path / "hotfix-bookings.json",
        client_context_id=14042,
        port=3001,
        booking_retention_days=180,
        admin_emails=frozenset(),
    )
    reset_settings_for_tests(s)
    with pytest.raises(RuntimeError, match="Jira credentials"):
        seed.resolve_user_or_error("anyone@example.com")
