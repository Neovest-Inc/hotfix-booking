"""Environment-driven configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    jira_base_url: str
    jira_email: str
    jira_api_token: str
    bookings_file: Path
    client_context_id: int
    port: int
    booking_retention_days: int
    admin_emails: frozenset[str]
    teams_webhook_url: str = ""
    app_base_url: str = ""
    teams_target: str = ""
    atlassian_client_id: str = ""
    atlassian_client_secret: str = ""
    session_secret_key: str = ""
    session_max_age_days: int = 365

    @classmethod
    def from_env(cls) -> "Settings":
        raw_admins = os.getenv("ADMIN_EMAILS", "")
        admins = frozenset(
            e.strip().lower() for e in raw_admins.split(",") if e.strip()
        )
        # Teams webhook resolution:
        #   TEAMS_TARGET=<name>  → look up TEAMS_WEBHOOK_URL_<NAME> (uppercased)
        #   TEAMS_TARGET unset   → notifications disabled
        # Deliberate policy: if TEAMS_TARGET is set but the matching URL var
        # is empty, notifications are DISABLED. Refusing to fall through is
        # safer than accidentally posting test messages to the prod chat.
        teams_target = os.getenv("TEAMS_TARGET", "").strip().lower()
        teams_webhook_url = ""
        if teams_target:
            teams_webhook_url = os.getenv(
                f"TEAMS_WEBHOOK_URL_{teams_target.upper()}", ""
            ).strip()
        return cls(
            jira_base_url=os.getenv("JIRA_BASE_URL", ""),
            jira_email=os.getenv("JIRA_EMAIL", ""),
            jira_api_token=os.getenv("JIRA_API_TOKEN", ""),
            bookings_file=Path(os.getenv("BOOKINGS_FILE", "./data/hotfix-bookings.json")),
            client_context_id=int(os.getenv("CLIENT_CONTEXT_ID", "14042")),
            port=int(os.getenv("PORT", "3001")),
            booking_retention_days=int(os.getenv("BOOKING_RETENTION_DAYS", "180")),
            admin_emails=admins,
            teams_webhook_url=teams_webhook_url,
            app_base_url=os.getenv("APP_BASE_URL", "").strip().rstrip("/"),
            teams_target=teams_target,
            atlassian_client_id=os.getenv("ATLASSIAN_CLIENT_ID", "").strip(),
            atlassian_client_secret=os.getenv("ATLASSIAN_CLIENT_SECRET", "").strip(),
            session_secret_key=os.getenv("SESSION_SECRET_KEY", "").strip(),
            session_max_age_days=int(os.getenv("SESSION_MAX_AGE_DAYS", "365")),
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def reset_settings_for_tests(settings: Settings) -> None:
    """Test hook — override the module-level settings singleton."""
    global _settings
    _settings = settings
