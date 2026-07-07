"""Tests for booking cleanup rules (deploy-based + age-based)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hotfix_booking.history import cleanup_bookings


def _booking(version: str, days_ago: int) -> dict:
    now = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)
    return {
        "id": f"HB-{version}",
        "version": version,
        "components": ["c"],
        "clientEnvironments": ["e"],
        "bookedBy": "U",
        "bookedAt": (now - timedelta(days=days_ago)).isoformat(),
        "status": "booked",
    }


NOW = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)


class TestCleanupBookings:
    def test_removes_deployed_versions(self) -> None:
        bookings = [_booking("9.97.10", 1), _booking("9.97.11", 2)]
        kept, removed = cleanup_bookings(
            bookings, deployed={"9.97.10"}, now=NOW, retention_days=180
        )
        assert [b["version"] for b in kept] == ["9.97.11"]
        assert [b["version"] for b in removed] == ["9.97.10"]

    def test_removes_bookings_older_than_retention(self) -> None:
        bookings = [
            _booking("9.97.10", 200),  # older than 180 days → drop
            _booking("9.97.11", 100),  # within window → keep
        ]
        kept, removed = cleanup_bookings(
            bookings, deployed=set(), now=NOW, retention_days=180
        )
        assert [b["version"] for b in kept] == ["9.97.11"]
        assert [b["version"] for b in removed] == ["9.97.10"]

    def test_boundary_at_exactly_retention_days_is_kept(self) -> None:
        bookings = [_booking("9.97.10", 180)]
        kept, removed = cleanup_bookings(
            bookings, deployed=set(), now=NOW, retention_days=180
        )
        assert len(kept) == 1
        assert len(removed) == 0

    def test_deploy_and_age_combined(self) -> None:
        bookings = [
            _booking("9.97.10", 1),     # keep
            _booking("9.97.11", 1),     # deployed → drop
            _booking("9.97.12", 300),   # too old → drop
            _booking("9.97.13", 300),   # too old AND deployed → drop
        ]
        kept, removed = cleanup_bookings(
            bookings, deployed={"9.97.11", "9.97.13"}, now=NOW, retention_days=180
        )
        assert [b["version"] for b in kept] == ["9.97.10"]
        assert {b["version"] for b in removed} == {"9.97.11", "9.97.12", "9.97.13"}

    def test_missing_bookedAt_is_kept(self) -> None:
        # If a booking somehow lacks bookedAt, don't nuke it — treat as fresh.
        bookings = [{"id": "HB-x", "version": "9.97.10"}]
        kept, removed = cleanup_bookings(
            bookings, deployed=set(), now=NOW, retention_days=180
        )
        assert len(kept) == 1
        assert len(removed) == 0

    def test_malformed_bookedAt_is_kept(self) -> None:
        bookings = [{"id": "HB-x", "version": "9.97.10", "bookedAt": "not-a-date"}]
        kept, removed = cleanup_bookings(
            bookings, deployed=set(), now=NOW, retention_days=180
        )
        assert len(kept) == 1

    def test_configurable_retention(self) -> None:
        bookings = [_booking("9.97.10", 30)]
        # 7 day retention → drop
        _, removed = cleanup_bookings(
            bookings, deployed=set(), now=NOW, retention_days=7
        )
        assert len(removed) == 1
        # 60 day retention → keep
        kept, _ = cleanup_bookings(
            bookings, deployed=set(), now=NOW, retention_days=60
        )
        assert len(kept) == 1
