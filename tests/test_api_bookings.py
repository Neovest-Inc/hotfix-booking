"""Tests for /bookings and /book (no Jira interaction)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import read_bookings, write_bookings


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


class TestGetBookings:
    def test_empty(self, client: TestClient) -> None:
        r = client.get("/api/hotfix-booking/bookings")
        assert r.status_code == 200
        assert r.json() == {"bookings": []}

    def test_returns_file_contents_verbatim(
        self, client: TestClient, bookings_file: Path
    ) -> None:
        write_bookings(bookings_file, [
            {"id": "HB-1", "version": "9.92.1", "components": ["A"],
             "clientEnvironments": ["CL001"], "bookedBy": "U", "bookedAt": "T",
             "status": "booked"}
        ])
        r = client.get("/api/hotfix-booking/bookings")
        assert r.status_code == 200
        assert r.json()["bookings"][0]["version"] == "9.92.1"


class TestPostBook:
    _valid = {
        "version": "9.94.19",
        "components": ["Alerts"],
        "clientEnvironments": ["CL001 - Fortress"],
        "bookedBy": "Alice",
    }

    def test_success_writes_file_and_returns_booking(
        self, client: TestClient, bookings_file: Path
    ) -> None:
        r = client.post("/api/hotfix-booking/book", json=self._valid)
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        booking = body["booking"]
        assert booking["version"] == "9.94.19"
        assert booking["components"] == ["Alerts"]
        assert booking["clientEnvironments"] == ["CL001 - Fortress"]
        assert booking["bookedBy"] == "Alice"
        assert booking["status"] == "booked"
        assert booking["id"].startswith("HB-")
        assert booking["bookedAt"]  # ISO 8601 timestamp
        # File was written
        assert read_bookings(bookings_file) == [booking]

    def test_booked_by_defaults_to_unknown(
        self, client: TestClient, bookings_file: Path
    ) -> None:
        payload = {**self._valid}
        payload.pop("bookedBy")
        r = client.post("/api/hotfix-booking/book", json=payload)
        assert r.status_code == 200
        assert r.json()["booking"]["bookedBy"] == "Unknown"

    @pytest.mark.parametrize(
        "missing_key",
        ["version", "components", "clientEnvironments"],
    )
    def test_missing_field_returns_400(
        self, client: TestClient, missing_key: str
    ) -> None:
        payload = {**self._valid}
        payload.pop(missing_key)
        r = client.post("/api/hotfix-booking/book", json=payload)
        assert r.status_code == 400
        assert "Missing required fields" in r.json()["error"]

    def test_empty_components_returns_400(self, client: TestClient) -> None:
        payload = {**self._valid, "components": []}
        r = client.post("/api/hotfix-booking/book", json=payload)
        assert r.status_code == 400
        assert r.json() == {"error": "At least one component is required"}

    def test_non_list_components_returns_400(self, client: TestClient) -> None:
        payload = {**self._valid, "components": "Alerts"}
        r = client.post("/api/hotfix-booking/book", json=payload)
        assert r.status_code == 400
        assert r.json() == {"error": "At least one component is required"}

    def test_empty_client_environments_returns_400(self, client: TestClient) -> None:
        payload = {**self._valid, "clientEnvironments": []}
        r = client.post("/api/hotfix-booking/book", json=payload)
        assert r.status_code == 400
        assert r.json() == {"error": "At least one client environment is required"}

    def test_duplicate_version_returns_409_with_existing(
        self, client: TestClient, bookings_file: Path
    ) -> None:
        existing = {
            "id": "HB-existing",
            "version": "9.94.19",
            "components": ["X"],
            "clientEnvironments": ["CL999"],
            "bookedBy": "First",
            "bookedAt": "2026-01-01T00:00:00Z",
            "status": "booked",
        }
        write_bookings(bookings_file, [existing])
        r = client.post("/api/hotfix-booking/book", json=self._valid)
        assert r.status_code == 409
        body = r.json()
        assert body["error"] == "Version 9.94.19 is already booked"
        assert body["existingBooking"] == existing
        # File unchanged
        assert read_bookings(bookings_file) == [existing]

    def test_appends_to_existing_bookings(
        self, client: TestClient, bookings_file: Path
    ) -> None:
        write_bookings(bookings_file, [
            {"id": "HB-1", "version": "9.94.10", "components": ["A"],
             "clientEnvironments": ["CL001"], "bookedBy": "U", "bookedAt": "T",
             "status": "booked"}
        ])
        r = client.post("/api/hotfix-booking/book", json=self._valid)
        assert r.status_code == 200
        after = read_bookings(bookings_file)
        assert len(after) == 2
        assert after[0]["version"] == "9.94.10"
        assert after[1]["version"] == "9.94.19"

    @pytest.mark.parametrize(
        "bad_version",
        ["1.2", "1.2.3.4", "1.2.a", "v1.2.3", "latest"],
    )
    def test_invalid_version_format_returns_400(
        self, client: TestClient, bad_version: str
    ) -> None:
        payload = {**self._valid, "version": bad_version}
        r = client.post("/api/hotfix-booking/book", json=payload)
        assert r.status_code == 400
        assert "x.y.z" in r.json()["error"]


class TestMalformedBookingsFile:
    """When the JSON store is corrupted, endpoints must surface an error
    instead of silently returning an empty state (which would mask data loss)."""

    def _corrupt(self, path: Path) -> None:
        path.write_text("not-valid-json{{{")

    def test_bookings_endpoint_returns_500(
        self, client: TestClient, bookings_file: Path
    ) -> None:
        self._corrupt(bookings_file)
        r = client.get("/api/hotfix-booking/bookings")
        assert r.status_code == 500
        assert "malformed" in r.json()["error"].lower()

    def test_book_endpoint_returns_500_and_does_not_overwrite(
        self, client: TestClient, bookings_file: Path
    ) -> None:
        self._corrupt(bookings_file)
        r = client.post("/api/hotfix-booking/book", json={
            "version": "9.94.19",
            "components": ["A"],
            "clientEnvironments": ["CL"],
        })
        assert r.status_code == 500
        # File was NOT rewritten (still corrupt) — data preserved for recovery
        assert bookings_file.read_text() == "not-valid-json{{{"
