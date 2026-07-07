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

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            jira_base_url=os.getenv("JIRA_BASE_URL", ""),
            jira_email=os.getenv("JIRA_EMAIL", ""),
            jira_api_token=os.getenv("JIRA_API_TOKEN", ""),
            bookings_file=Path(os.getenv("BOOKINGS_FILE", "./data/hotfix-bookings.json")),
            client_context_id=int(os.getenv("CLIENT_CONTEXT_ID", "14042")),
            port=int(os.getenv("PORT", "3001")),
            booking_retention_days=int(os.getenv("BOOKING_RETENTION_DAYS", "180")),
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
