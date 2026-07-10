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

from .dependencies import compute_parents, direct_children, parent_versions

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


class BookingNotFoundError(Exception):
    """Raised when a booking id is not present in the store."""

    def __init__(self, booking_id: str) -> None:
        super().__init__(f"Booking {booking_id} not found")
        self.booking_id = booking_id


class AlreadyCancelledError(Exception):
    """Raised when a booking is already in `status: cancelled`."""

    def __init__(self, booking_id: str) -> None:
        super().__init__(f"Booking {booking_id} is already cancelled")
        self.booking_id = booking_id


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

    Normalizes each booking with sane defaults (`status`, `parents`,
    `originalParents`, `rebaseHistory`) and computes `parents` for legacy
    records that never had the field. Idempotent — records that already have
    a schema field are left untouched, so a cancel-driven rebase is never
    silently reverted. Persisted changes happen only when a mutating caller
    calls `save_bookings` on the returned data.
    """
    p = Path(path)
    if not p.exists():
        return {"bookings": []}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log.error("Bookings file %s is malformed: %s", p, e)
        raise MalformedBookingsError(f"Bookings file {p} is malformed: {e}") from e
    _normalize_and_backfill(raw.get("bookings", []) or [])
    return raw


def _normalize_and_backfill(bookings: list[dict]) -> None:
    """Fill in schema fields on legacy records. In place. Idempotent.

    Runs in `bookedAt` ASC order so each record's `parents` is computed against
    the priors that already existed when it was booked. Records that already
    have a `parents` key are trusted — that's how we avoid overwriting a
    rebased booking's current basis with a "clean" recomputation.
    """
    # Scalar defaults are always safe to fill.
    for b in bookings:
        b.setdefault("status", "booked")
        b.setdefault("rebaseHistory", [])

    ordered = sorted(bookings, key=lambda b: b.get("bookedAt") or "")
    processed: list[dict] = []
    for b in ordered:
        has_parents_key = "parents" in b
        has_original_key = "originalParents" in b
        if not has_parents_key:
            b["parents"] = compute_parents(b, processed)
        if not has_original_key:
            # Best available approximation for a legacy record: its current
            # parents == originalParents (no rebase history to reason about).
            b["originalParents"] = list(b.get("parents") or [])
        processed.append(b)


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
    booked_by_email: str | None = None,
    now: Callable[[], str] = default_now,
    id_factory: Callable[[], str] = default_id_factory,
    additional_priors: list[dict] | None = None,
) -> tuple[dict, BookingsData]:
    """Append a new booking. Raises on invalid version or duplicate.

    `additional_priors` — optional list of booking-shaped dicts (typically
    Jira CMs converted via `dependencies.cm_to_pseudo_booking`) fed into the
    dependency graph as candidate parents. They are NOT persisted into the
    local store; they only influence the computed `parents` for the new
    booking. When a pseudo-prior and a local booking share the same version,
    the local booking wins (the local record is authoritative for its own
    version).

    Returns (new_booking, updated_data). `data` is mutated in place.
    """
    if not isinstance(version, str) or not _SEMVER_RE.match(version):
        raise InvalidVersionError(f"Version {version!r} must match x.y.z")

    bookings: list[dict] = data.setdefault("bookings", [])
    existing = next((b for b in bookings if b.get("version") == version), None)
    if existing is not None:
        raise AlreadyBookedError(version, existing)

    stub = {
        "version": version,
        "components": components,
        "clientEnvironments": client_environments,
    }
    local_versions = {b.get("version") for b in bookings if b.get("version")}
    extra = [
        p for p in (additional_priors or [])
        if p.get("version") and p.get("version") not in local_versions
    ]
    parents = compute_parents(stub, list(bookings) + extra)

    new_booking = {
        "id": id_factory(),
        "version": version,
        "components": components,
        "clientEnvironments": client_environments,
        "bookedBy": booked_by if booked_by else "Unknown",
        "bookedByEmail": booked_by_email or "",
        "bookedAt": now(),
        "status": "booked",
        "parents": parents,
        "originalParents": list(parents),
        "rebaseHistory": [],
    }
    bookings.append(new_booking)
    return new_booking, data


def cancel_booking(
    data: BookingsData,
    *,
    booking_id: str,
    cancelled_by: str,
    cancelled_by_email: str,
    now: Callable[[], str] = default_now,
    additional_priors: list[dict] | None = None,
) -> tuple[dict, list[dict]]:
    """Mark a booking cancelled and rebase every direct child.

    Returns `(cancelled_booking, affected_children_with_snapshots)`.

    Each entry in `affected_children_with_snapshots` is the child record
    itself (mutated in place: `parents` updated, `rebaseHistory` appended).
    Grandchildren are not touched — their direct parent still exists in the
    store; only the DAG edge into the cancelled record is rewired.

    `additional_priors` — optional pseudo-bookings representing Jira CMs on
    the same release line. Passed into the child rebase recomputation so a
    rebased child can be re-parented onto a Jira CM (matching the behaviour
    at initial booking time). Same version-dedup rule as `create_booking`.
    """
    bookings: list[dict] = data.setdefault("bookings", [])
    target = next((b for b in bookings if b.get("id") == booking_id), None)
    if target is None:
        raise BookingNotFoundError(booking_id)
    if target.get("status") == "cancelled":
        raise AlreadyCancelledError(booking_id)

    cancelled_at = now()
    target["status"] = "cancelled"
    target["cancelledAt"] = cancelled_at
    target["cancelledBy"] = cancelled_by or "Unknown"
    target["cancelledByEmail"] = cancelled_by_email or ""

    local_versions = {b.get("version") for b in bookings if b.get("version")}
    extra = [
        p for p in (additional_priors or [])
        if p.get("version") and p.get("version") not in local_versions
    ]
    combined_priors = list(bookings) + extra

    children = direct_children(booking_id, bookings)
    affected: list[dict] = []
    for child in children:
        previous_parents = list(child.get("parents") or [])
        # Recompute against local bookings + pseudo priors — `target` is now
        # cancelled so `compute_parents` will skip it automatically.
        new_parents = compute_parents(child, combined_priors)
        child["parents"] = new_parents
        rebase_event = {
            "at": cancelled_at,
            "cancelledBookingId": booking_id,
            "cancelledVersion": target.get("version"),
            "cancelledBy": cancelled_by or "Unknown",
            "previousParents": previous_parents,
            "newParents": list(new_parents),
            "previousParentVersions": parent_versions(previous_parents, combined_priors),
            "newParentVersions": parent_versions(new_parents, combined_priors),
        }
        child.setdefault("rebaseHistory", []).append(rebase_event)
        affected.append(child)

    return target, affected
