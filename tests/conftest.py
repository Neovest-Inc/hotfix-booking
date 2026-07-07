"""Shared pytest fixtures for API tests."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from hotfix_booking.app import create_app
from hotfix_booking.config import Settings, reset_settings_for_tests

FIXTURES = Path(__file__).parent / "fixtures" / "jira"
TEST_JIRA_BASE = "https://jira.test"


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
    )
    reset_settings_for_tests(s)
    return s


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def mock_jira(settings: Settings) -> Iterator[respx.MockRouter]:
    """`respx` router bound to the test Jira base URL. Assert routes per test."""
    with respx.mock(base_url=TEST_JIRA_BASE, assert_all_called=False) as router:
        yield router


def write_bookings(path: Path, bookings: list[dict]) -> None:
    path.write_text(json.dumps({"bookings": bookings}, indent=2), encoding="utf-8")


def read_bookings(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["bookings"]
