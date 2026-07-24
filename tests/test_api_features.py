"""GET /api/hotfix-booking/features — feature-flag endpoint used by the
front-end to decide which tabs to show.

The flag defaults to False (Compare tab hidden). Ops flips it on in `.env`
via `HB_COMPARE_ENABLED=true` when the Compare feature is ready to go live.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from hotfix_booking.config import Settings, reset_settings_for_tests


def _apply_settings(base: Settings, **overrides) -> None:
    reset_settings_for_tests(
        Settings(
            jira_base_url=base.jira_base_url,
            jira_email=base.jira_email,
            jira_api_token=base.jira_api_token,
            bookings_file=base.bookings_file,
            client_context_id=base.client_context_id,
            port=base.port,
            booking_retention_days=base.booking_retention_days,
            admin_emails=base.admin_emails,
            teams_webhook_url=base.teams_webhook_url,
            app_base_url=base.app_base_url,
            teams_target=base.teams_target,
            atlassian_client_id=base.atlassian_client_id,
            atlassian_client_secret=base.atlassian_client_secret,
            session_secret_key=base.session_secret_key,
            session_max_age_days=base.session_max_age_days,
            compare_enabled=overrides.get("compare_enabled", base.compare_enabled),
        )
    )


def test_features_requires_auth(anon_client: TestClient) -> None:
    """Unauthenticated calls get 401 — same gating as every other
    /api/hotfix-booking/* endpoint."""
    r = anon_client.get("/api/hotfix-booking/features")
    assert r.status_code == 401


def test_features_defaults_compare_disabled(client: TestClient, settings: Settings) -> None:
    """Fresh Settings has compare_enabled=False → endpoint reports the tab
    should be hidden. This is the safe default so the Compare tab never
    accidentally ships to prod without an explicit env flip."""
    r = client.get("/api/hotfix-booking/features")
    assert r.status_code == 200
    assert r.json() == {"compareEnabled": False}


def test_features_reports_enabled_when_flag_is_on(
    client: TestClient, settings: Settings
) -> None:
    """Flipping HB_COMPARE_ENABLED on (represented here by overriding the
    Settings singleton) makes the endpoint report the tab should show."""
    _apply_settings(settings, compare_enabled=True)
    r = client.get("/api/hotfix-booking/features")
    assert r.status_code == 200
    assert r.json() == {"compareEnabled": True}


def test_settings_parses_hb_compare_enabled_env_var(monkeypatch, tmp_path) -> None:
    """The env var accepts common truthy values (case-insensitive) and
    defaults to False for anything else — including unset, empty string,
    typos, `0`, `false`, etc. Safe by default."""
    from hotfix_booking.config import Settings

    # Minimum viable env for Settings.from_env().
    for k in [
        "JIRA_BASE_URL",
        "JIRA_EMAIL",
        "JIRA_API_TOKEN",
        "CLIENT_CONTEXT_ID",
        "PORT",
        "BOOKING_RETENTION_DAYS",
        "ADMIN_EMAILS",
        "TEAMS_TARGET",
        "APP_BASE_URL",
        "ATLASSIAN_CLIENT_ID",
        "ATLASSIAN_CLIENT_SECRET",
        "SESSION_SECRET_KEY",
        "SESSION_MAX_AGE_DAYS",
    ]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BOOKINGS_FILE", str(tmp_path / "bookings.json"))

    # Default (unset) → False.
    monkeypatch.delenv("HB_COMPARE_ENABLED", raising=False)
    assert Settings.from_env().compare_enabled is False

    # Truthy values → True.
    for val in ("true", "True", "TRUE", "1", "yes", "YES", "on"):
        monkeypatch.setenv("HB_COMPARE_ENABLED", val)
        assert Settings.from_env().compare_enabled is True, val

    # Everything else → False (including empty, `0`, `false`, garbage).
    for val in ("", "0", "false", "no", "off", "maybe", " "):
        monkeypatch.setenv("HB_COMPARE_ENABLED", val)
        assert Settings.from_env().compare_enabled is False, repr(val)
