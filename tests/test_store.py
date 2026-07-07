import json
from pathlib import Path

import pytest

from hotfix_booking.store import (
    AlreadyBookedError,
    create_booking,
    load_bookings,
    save_bookings,
)


class TestLoadBookings:
    def test_missing_file_returns_empty_default(self, tmp_path: Path) -> None:
        assert load_bookings(tmp_path / "nope.json") == {"bookings": []}

    def test_reads_valid_file(self, tmp_path: Path) -> None:
        f = tmp_path / "b.json"
        f.write_text(json.dumps({"bookings": [{"id": "HB-1", "version": "9.92.1"}]}))
        assert load_bookings(f) == {"bookings": [{"id": "HB-1", "version": "9.92.1"}]}

    def test_malformed_json_returns_default_and_logs(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        f = tmp_path / "b.json"
        f.write_text("not-json{")
        with caplog.at_level("ERROR"):
            assert load_bookings(f) == {"bookings": []}
        assert any("Error loading bookings" in r.message for r in caplog.records)


class TestSaveBookings:
    def test_writes_with_two_space_indent(self, tmp_path: Path) -> None:
        f = tmp_path / "b.json"
        save_bookings(f, {"bookings": [{"id": "HB-1", "version": "9.92.1"}]})
        text = f.read_text(encoding="utf-8")
        assert '"bookings": [' in text
        # 2-space indent (matches Node JSON.stringify(data, null, 2))
        assert '\n  "bookings"' in text
        assert '\n    {' in text

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        f = tmp_path / "nested" / "deep" / "b.json"
        save_bookings(f, {"bookings": []})
        assert f.exists()

    def test_roundtrip(self, tmp_path: Path) -> None:
        f = tmp_path / "b.json"
        payload = {"bookings": [{"id": "HB-x", "version": "1.2.3", "components": ["A"]}]}
        save_bookings(f, payload)
        assert load_bookings(f) == payload


class TestCreateBooking:
    def _mk(self):
        return create_booking(
            {"bookings": []},
            version="9.92.19",
            components=["Alerts"],
            client_environments=["CL001 - Fortress"],
            booked_by="Alice",
            now=lambda: "2026-07-07T12:00:00+00:00",
            id_factory=lambda: "HB-STATIC",
        )

    def test_happy_path_shape(self) -> None:
        booking, data = self._mk()
        assert booking == {
            "id": "HB-STATIC",
            "version": "9.92.19",
            "components": ["Alerts"],
            "clientEnvironments": ["CL001 - Fortress"],
            "bookedBy": "Alice",
            "bookedAt": "2026-07-07T12:00:00+00:00",
            "status": "booked",
        }
        assert data["bookings"] == [booking]

    def test_booked_by_defaults_to_unknown_when_falsy(self) -> None:
        for value in [None, ""]:
            b, _ = create_booking(
                {"bookings": []},
                version="1.2.3",
                components=["c"],
                client_environments=["e"],
                booked_by=value,
                now=lambda: "T",
                id_factory=lambda: "I",
            )
            assert b["bookedBy"] == "Unknown"

    def test_duplicate_version_raises(self) -> None:
        data = {"bookings": [{"id": "HB-1", "version": "9.92.19", "status": "booked"}]}
        with pytest.raises(AlreadyBookedError) as exc:
            create_booking(
                data,
                version="9.92.19",
                components=["c"],
                client_environments=["e"],
                booked_by="a",
            )
        assert exc.value.version == "9.92.19"
        assert exc.value.existing["id"] == "HB-1"
        # store not mutated on failure
        assert len(data["bookings"]) == 1

    def test_preserves_existing_order(self) -> None:
        data = {"bookings": [{"id": "HB-1", "version": "9.92.1"}, {"id": "HB-2", "version": "9.92.2"}]}
        new, updated = create_booking(
            data,
            version="9.92.3",
            components=["c"],
            client_environments=["e"],
            booked_by="a",
            now=lambda: "T",
            id_factory=lambda: "HB-3",
        )
        assert [b["id"] for b in updated["bookings"]] == ["HB-1", "HB-2", "HB-3"]

    def test_initializes_bookings_key_if_missing(self) -> None:
        data: dict = {}
        create_booking(
            data,
            version="1.2.3",
            components=["c"],
            client_environments=["e"],
            booked_by="a",
            now=lambda: "T",
            id_factory=lambda: "I",
        )
        assert data["bookings"][0]["version"] == "1.2.3"
