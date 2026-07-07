"""Bookings JSON store — mirrors server/hotfix-booking.js loadBookings/saveBookings/POST /book.

Node reference:
    function loadBookings() {
      try { if (fs.existsSync(F)) return JSON.parse(fs.readFileSync(F,'utf8')); }
      catch (e) { console.error(...); }
      return { bookings: [] };
    }
    function saveBookings(data) { fs.writeFileSync(F, JSON.stringify(data, null, 2)); }
    // POST /book creates: { id: `HB-${Date.now()}`, version, components, clientEnvironments,
    //                       bookedBy: bookedBy || 'Unknown', bookedAt: new Date().toISOString(),
    //                       status: 'booked' }
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

BookingsData = dict[str, Any]


class AlreadyBookedError(Exception):
    """Raised when a version is already present in the store."""

    def __init__(self, version: str, existing: dict) -> None:
        super().__init__(f"Version {version} is already booked")
        self.version = version
        self.existing = existing


def load_bookings(path: str | Path) -> BookingsData:
    """Read the JSON store. Missing file → {"bookings": []}. Errors are logged, same default."""
    p = Path(path)
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # matches Node's broad try/catch around read+parse
        log.error("Error loading bookings: %s", e)
    return {"bookings": []}


def save_bookings(path: str | Path, data: BookingsData) -> None:
    """Write with 2-space indent (matches Node JSON.stringify(data, null, 2))."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def default_id_factory() -> str:
    """`HB-<epoch-ms>` — mirrors Node `HB-${Date.now()}`."""
    return f"HB-{int(time.time() * 1000)}"


def default_now() -> str:
    """ISO 8601 UTC timestamp. Python-native microsecond precision (accepted per plan)."""
    return datetime.now(timezone.utc).isoformat()


def create_booking(
    data: BookingsData,
    *,
    version: str,
    components: list[str],
    client_environments: list[str],
    booked_by: str | None,
    now: Callable[[], str] = default_now,
    id_factory: Callable[[], str] = default_id_factory,
) -> tuple[dict, BookingsData]:
    """Append a new booking to `data['bookings']`. Raises AlreadyBookedError on duplicate version.

    Returns (new_booking, updated_data). `data` is mutated (matches Node) — return is convenience.
    """
    bookings: list[dict] = data.setdefault("bookings", [])
    existing = next((b for b in bookings if b.get("version") == version), None)
    if existing is not None:
        raise AlreadyBookedError(version, existing)

    new_booking = {
        "id": id_factory(),
        "version": version,
        "components": components,
        "clientEnvironments": client_environments,
        "bookedBy": booked_by if booked_by else "Unknown",  # matches `|| 'Unknown'`
        "bookedAt": now(),
        "status": "booked",
    }
    bookings.append(new_booking)
    return new_booking, data
