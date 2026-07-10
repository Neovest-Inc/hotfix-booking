"""Tests for `Settings.from_env` — specifically the Teams target resolver.

The Teams notifier ultimately reads a single `settings.teams_webhook_url`
string. How that string is chosen from the environment is what this file
covers: `TEAMS_TARGET` selects which of the named URLs
(`TEAMS_WEBHOOK_URL_PROD`, `TEAMS_WEBHOOK_URL_TEST`, ...) becomes the
active one.

Key policy under test:
- `TEAMS_TARGET=<name>` resolves to `TEAMS_WEBHOOK_URL_<NAME>`.
- If `TEAMS_TARGET` is set but the matching URL var is empty, notifications
  are DISABLED — we never silently fall back to a different target's URL
  (would risk posting test messages to the prod chat).
- If `TEAMS_TARGET` is unset, notifications are disabled.
- Case-insensitive on the target name.
"""
from __future__ import annotations

import pytest

from hotfix_booking.config import Settings


@pytest.fixture(autouse=True)
def _clean_teams_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every test from the developer's real .env / process env."""
    for var in (
        "TEAMS_TARGET",
        "TEAMS_WEBHOOK_URL_PROD",
        "TEAMS_WEBHOOK_URL_TEST",
        "APP_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)


class TestTeamsTargetResolution:
    def test_target_prod_selects_prod_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEAMS_TARGET", "prod")
        monkeypatch.setenv("TEAMS_WEBHOOK_URL_PROD", "https://prod.example/hook")
        monkeypatch.setenv("TEAMS_WEBHOOK_URL_TEST", "https://test.example/hook")
        s = Settings.from_env()
        assert s.teams_target == "prod"
        assert s.teams_webhook_url == "https://prod.example/hook"

    def test_target_test_selects_test_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEAMS_TARGET", "test")
        monkeypatch.setenv("TEAMS_WEBHOOK_URL_PROD", "https://prod.example/hook")
        monkeypatch.setenv("TEAMS_WEBHOOK_URL_TEST", "https://test.example/hook")
        s = Settings.from_env()
        assert s.teams_target == "test"
        assert s.teams_webhook_url == "https://test.example/hook"

    def test_target_is_case_insensitive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEAMS_TARGET", "PROD")
        monkeypatch.setenv("TEAMS_WEBHOOK_URL_PROD", "https://prod.example/hook")
        assert Settings.from_env().teams_webhook_url == "https://prod.example/hook"

    def test_unset_target_disables_notifications(self) -> None:
        # autouse fixture clears everything.
        s = Settings.from_env()
        assert s.teams_target == ""
        assert s.teams_webhook_url == ""

    def test_target_set_but_matching_url_missing_disables_notifications(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Safety: `TEAMS_TARGET=test` with no `TEAMS_WEBHOOK_URL_TEST` set
        must NOT silently fall through to the prod URL or the legacy field.
        Refusing is safer than sending test messages to the wrong chat."""
        monkeypatch.setenv("TEAMS_TARGET", "test")
        monkeypatch.setenv("TEAMS_WEBHOOK_URL_PROD", "https://prod.example/hook")
        # Intentionally: no TEAMS_WEBHOOK_URL_TEST, no TEAMS_WEBHOOK_URL
        s = Settings.from_env()
        assert s.teams_target == "test"
        assert s.teams_webhook_url == ""

    def test_unknown_target_name_disables_notifications(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEAMS_TARGET", "staging")  # no TEAMS_WEBHOOK_URL_STAGING
        monkeypatch.setenv("TEAMS_WEBHOOK_URL_PROD", "https://prod.example/hook")
        assert Settings.from_env().teams_webhook_url == ""

    def test_supports_arbitrary_target_names_via_convention(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not just prod/test — any `TEAMS_TARGET=<name>` looks up
        `TEAMS_WEBHOOK_URL_<NAME>` (uppercased). Lets us add e.g. `staging`
        without touching code."""
        monkeypatch.setenv("TEAMS_TARGET", "staging")
        monkeypatch.setenv("TEAMS_WEBHOOK_URL_STAGING", "https://staging.example/hook")
        assert (
            Settings.from_env().teams_webhook_url == "https://staging.example/hook"
        )
