"""Shared pytest fixtures for API tests."""
from __future__ import annotations

import json
from base64 import b64encode
from pathlib import Path
from typing import Iterator

import httpx
import itsdangerous
import pytest
import respx
from fastapi.testclient import TestClient

from hotfix_booking.app import create_app
from hotfix_booking.config import Settings, reset_settings_for_tests

FIXTURES = Path(__file__).parent / "fixtures" / "jira"
TEST_JIRA_BASE = "https://jira.test"

# Canonical test user that the default `client` fixture is logged in as.
# Tests that need a different caller identity call `login_as(client, ...)`.
TEST_USER_EMAIL = "test-user@example.com"
TEST_USER_NAME = "Test User"
TEST_USER_ACCOUNT_ID = "test-account-id"


def load_fixture(name: str) -> dict | list:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def bookings_file(tmp_path: Path) -> Path:
    """Fresh per-test bookings file."""
    f = tmp_path / "hotfix-bookings.json"
    f.write_text(json.dumps({"bookings": []}))
    return f


@pytest.fixture
def settings(bookings_file: Path) -> Settings:
    s = Settings(
        jira_base_url=TEST_JIRA_BASE,
        jira_email="test@example.com",
        jira_api_token="secret",
        bookings_file=bookings_file,
        client_context_id=14042,
        port=3001,
        booking_retention_days=180,
        admin_emails=frozenset(),
        teams_webhook_url="",
        app_base_url="http://localhost:3001",
        atlassian_client_id="test-client-id",
        atlassian_client_secret="test-client-secret",
        session_secret_key="test-session-secret-do-not-use-in-prod-" + "x" * 32,
        session_max_age_days=365,
    )
    reset_settings_for_tests(s)
    return s


@pytest.fixture
def anon_client(settings: Settings) -> Iterator[TestClient]:
    """TestClient with no session cookie — used by /auth/* flow tests."""
    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """Default TestClient — logged in as the canonical Test User.

    Every test that hits a protected endpoint (i.e. anything not under
    /api/hotfix-booking/auth/*) gets this by default. Tests that need to
    exercise a different caller identity call
    `login_as(client, email, display_name)` before their API call.
    """
    app = create_app()
    with TestClient(app) as c:
        login_as(c, TEST_USER_EMAIL, TEST_USER_NAME, TEST_USER_ACCOUNT_ID)
        yield c


def login_as(
    client: TestClient,
    email: str,
    display_name: str,
    account_id: str = "test-account-id",
) -> None:
    """Pre-seed a signed session cookie on `client` so subsequent requests are
    authenticated as the given user.

    Uses the same signing scheme as `starlette.middleware.sessions.SessionMiddleware`,
    which is what our /auth/callback would normally set.
    """
    from hotfix_booking.config import get_settings

    secret = get_settings().session_secret_key
    signer = itsdangerous.TimestampSigner(secret)
    session = {
        "user": {
            "email": email,
            "displayName": display_name,
            "accountId": account_id,
        }
    }
    data = b64encode(json.dumps(session).encode("utf-8"))
    signed = signer.sign(data).decode("utf-8")
    client.cookies.set("hb_session", signed)


@pytest.fixture
def mock_jira(settings: Settings) -> Iterator[respx.MockRouter]:
    """`respx` router bound to the test Jira base URL. Assert routes per test."""
    with respx.mock(base_url=TEST_JIRA_BASE, assert_all_called=False) as router:
        yield router


def write_bookings(path: Path, bookings: list[dict]) -> None:
    path.write_text(json.dumps({"bookings": bookings}, indent=2), encoding="utf-8")


def read_bookings(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["bookings"]
