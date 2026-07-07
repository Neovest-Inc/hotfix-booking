"""Bookings JSON store.

Storage model: a single JSON file `{"bookings": [...]}`.

Concurrency: writes happen under a process-wide `threading.Lock`. Callers doing
read-modify-write cycles must wrap the whole cycle in `with bookings_lock():`.
This is safe for a single uvicorn worker (our default). If we ever run multiple
worker processes, we'll need an OS-level file lock — but the API stays the same
because we already funnel through this module.

Data integrity: `load_bookings` distinguishes "file missing" (empty default) from
"file exists but is malformed" (raises `MalformedBookingsError`). This prevents
silent data loss where a bad edit would look like an empty store.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

log = logging.getLogger(__name__)

BookingsData = dict[str, Any]

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
_LOCK = threading.Lock()


class AlreadyBookedError(Exception):
    """Raised when a version is already present in the store."""

    def __init__(self, version: str, existing: dict) -> None:
        super().__init__(f"Version {version} is already booked")
        self.version = version
        self.existing = existing


class MalformedBookingsError(Exception):
    """Raised when the bookings file exists but cannot be parsed as JSON."""


class InvalidVersionError(Exception):
    """Raised when a booking version doesn't match `x.y.z`."""


@contextmanager
def bookings_lock() -> Iterator[None]:
    """Serialize read-modify-write cycles on the bookings file within this process."""
    _LOCK.acquire()
    try:
        yield
    finally:
        _LOCK.release()


def load_bookings(path: str | Path) -> BookingsData:
    """Read the JSON store.

    - Missing file → `{"bookings": []}`
    - Existing but unparseable → raises `MalformedBookingsError` so callers can
      surface the problem instead of silently wiping data.
    """
    p = Path(path)
    if not p.exists():
        return {"bookings": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log.error("Bookings file %s is malformed: %s", p, e)
        raise MalformedBookingsError(f"Bookings file {p} is malformed: {e}") from e


def save_bookings(path: str | Path, data: BookingsData) -> None:
    """Write with 2-space indent, atomically via temp-file + rename."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def default_id_factory() -> str:
    """`HB-<epoch-ms>`."""
    return f"HB-{int(time.time() * 1000)}"


def default_now() -> str:
    """ISO 8601 UTC timestamp with microsecond precision."""
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
    """Append a new booking. Raises on invalid version or duplicate.

    Returns (new_booking, updated_data). `data` is mutated in place.
    """
    if not isinstance(version, str) or not _SEMVER_RE.match(version):
        raise InvalidVersionError(f"Version {version!r} must match x.y.z")

    bookings: list[dict] = data.setdefault("bookings", [])
    existing = next((b for b in bookings if b.get("version") == version), None)
    if existing is not None:
        raise AlreadyBookedError(version, existing)

    new_booking = {
        "id": id_factory(),
        "version": version,
        "components": components,
        "clientEnvironments": client_environments,
        "bookedBy": booked_by if booked_by else "Unknown",
        "bookedAt": now(),
        "status": "booked",
    }
    bookings.append(new_booking)
    return new_booking, data
