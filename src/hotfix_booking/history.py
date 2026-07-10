"""Hotfix history: merges deployed CMs + pending bookings for a given (major, minor).

Also owns booking cleanup rules — both deploy-based and age-based.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from functools import cmp_to_key
from typing import Any

from .versioning import compare_versions, is_semver, parse_version


# Jira CM statuses that mean the change is actually LIVE in production. Only
# these should trigger deploy-based booking cleanup, and only these should let
# a deployed row "swallow" a local booking during merge — a CM in "Approved",
# "Ready", "In Progress" etc. is still an in-flight change and the local
# booking remains the source of truth for cancellation.
_DEPLOYED_STATUSES = frozenset({"done", "deployment completed"})


def _is_deployed_status(name: str | None) -> bool:
    return (name or "").strip().lower() in _DEPLOYED_STATUSES


def derive_minor_versions(
    recent_cms: list[dict], count: int = 8
) -> tuple[int, int, list[dict]]:
    """Return the top-N most recent (major, minor) release lines from actual data.

    Cross-major: after 9.99 the next release is 10.0, and both may be actively
    supported for some time. The dropdown shows both.

    Returns `(current_major, current_minor, entries)`:
      - `current_*` = the highest (major, minor) pair present, or `(0, 0)` if empty
      - `entries` = list of `{major, minor, label}` sorted descending by
        `(major, minor)`, deduplicated, at most `count` items
    """
    pairs: set[tuple[int, int]] = set()
    for cm in recent_cms:
        for version in cm.get("fixVersions", []) or []:
            if is_semver(version):
                v = parse_version(version)
                pairs.add((v.major, v.minor))

    if not pairs:
        return 0, 0, []

    ordered = sorted(pairs, reverse=True)
    current_major, current_minor = ordered[0]
    entries = [
        {"major": major, "minor": minor, "label": f"{major}.{minor}.x"}
        for major, minor in ordered[:count]
    ]
    return current_major, current_minor, entries


def merge_hotfixes(
    version_cms: list[dict],
    bookings: list[dict],
    major: int,
    target_minor: int,
) -> list[dict]:
    """Merge deployed CMs (matching major.minor) with pending bookings.

    Semantics: ONE row per version. The Jira CM and the local booking are the
    same thing — the CM is the deployment ticket for a hotfix that was first
    booked in the app.

      - CM only (no booking)        → deployed row (no Cancel button)
      - Booking only (no CM yet)    → booked row (Cancel button if allowed)
      - Both CM and booking exist   → single UNIFIED row that carries the CM
        fields (cmKey, deployedAt, Jira status) AND the booking fields (id,
        parents, rebaseHistory) so the user still sees the CM link + status
        while retaining the ability to cancel the booking. `type` stays
        "booked" so the client renders the basis line, Rebased chip, and
        Cancel button just like a booking-only row.

    Sorted descending by version.
    """
    hotfixes: list[dict] = []

    # Version -> booking lookup (one booking per version at most).
    booking_by_version: dict[str, dict] = {}
    for b in bookings:
        v = b.get("version")
        if v:
            booking_by_version[v] = b

    covered_by_unified: set[str] = set()

    for cm in version_cms:
        for version in cm.get("fixVersions", []) or []:
            if not is_semver(version):
                continue
            v = parse_version(version)
            if v.major != major or v.minor != target_minor:
                continue

            booking = booking_by_version.get(version)
            if booking is not None:
                # UNIFIED row — CM data + booking data. Type stays 'booked'
                # so the client renders Cancel + basis + Rebased chip.
                status = booking.get("status", "booked")
                is_cancelled = status == "cancelled"
                hotfixes.append({
                    "version": version,
                    "type": "booked",
                    "cmKey": cm.get("key"),
                    "summary": cm.get("summary"),
                    "status": cm.get("status") or ("Cancelled" if is_cancelled else "Booked"),
                    "cmStatus": cm.get("status"),
                    "components": cm.get("components", []) or booking.get("components", []),
                    "clientEnvironments": cm.get("clientEnvironments", []) or booking.get("clientEnvironments", []),
                    "deployedAt": cm.get("targetDeploymentDate"),
                    "reporter": cm.get("reporter") or booking.get("bookedBy"),
                    "bookedAt": booking.get("bookedAt"),
                    "bookedBy": booking.get("bookedBy"),
                    "bookedByEmail": booking.get("bookedByEmail"),
                    "id": booking.get("id"),
                    "parents": booking.get("parents", []),
                    "originalParents": booking.get("originalParents", []),
                    "rebaseHistory": booking.get("rebaseHistory", []),
                    "bookingStatus": status,
                    "cancelledAt": booking.get("cancelledAt"),
                    "cancelledBy": booking.get("cancelledBy"),
                    "cancelledByEmail": booking.get("cancelledByEmail"),
                    "deployedInJira": True,
                })
                covered_by_unified.add(version)
            else:
                hotfixes.append({
                    "version": version,
                    "type": "deployed",
                    "cmKey": cm.get("key"),
                    "summary": cm.get("summary"),
                    "status": cm.get("status"),
                    "components": cm.get("components", []),
                    "clientEnvironments": cm.get("clientEnvironments", []),
                    "deployedAt": cm.get("targetDeploymentDate"),
                    "reporter": cm.get("reporter"),
                })

    for booking in bookings:
        v = parse_version(booking.get("version", ""))
        if v.major != major or v.minor != target_minor:
            continue
        version = booking["version"]
        # Already emitted as a unified row above.
        if version in covered_by_unified:
            continue
        # Booking-only row (no CM in Jira yet).
        status = booking.get("status", "booked")
        is_cancelled = status == "cancelled"
        hotfixes.append({
            "version": version,
            "type": "booked",
            "cmKey": None,
            "summary": None,
            "status": "Cancelled" if is_cancelled else "Booked",
            "components": booking.get("components", []),
            "clientEnvironments": booking.get("clientEnvironments", []),
            "bookedAt": booking.get("bookedAt"),
            "bookedBy": booking.get("bookedBy"),
            "bookedByEmail": booking.get("bookedByEmail"),
            "reporter": booking.get("bookedBy"),
            "id": booking.get("id"),
            "parents": booking.get("parents", []),
            "originalParents": booking.get("originalParents", []),
            "rebaseHistory": booking.get("rebaseHistory", []),
            "bookingStatus": status,
            "cancelledAt": booking.get("cancelledAt"),
            "cancelledBy": booking.get("cancelledBy"),
            "cancelledByEmail": booking.get("cancelledByEmail"),
            "deployedInJira": False,
        })

    # Sort descending by version
    hotfixes.sort(key=cmp_to_key(lambda a, b: compare_versions(b["version"], a["version"])))
    return hotfixes


def calculate_next_version(
    deployed_cms: list[dict],
    remaining_bookings: list[dict],
    *,
    major: int | None = None,
    minor: int | None = None,
) -> dict[str, Any]:
    """Compute the next available hotfix version.

    Without `major`/`minor` (default) → considers every semver across all deployed
    CMs and pending bookings, returns max+1 patch bump. This is the "current
    release line" case (max of everything).

    With `major` and `minor` → filters both deployed and booked to that specific
    minor line, e.g. `major=9, minor=95` → only 9.95.x counts. Lets users book
    hotfixes for previous minor releases.

    Returns { currentHighest, nextVersion, baseVersion } or { nextVersion: None, error }.
    """
    filtered = major is not None and minor is not None

    def _matches(v: str) -> bool:
        if not is_semver(v):
            return False
        if not filtered:
            return True
        parsed = parse_version(v)
        return parsed.major == major and parsed.minor == minor

    all_versions: list[str] = []
    for cm in deployed_cms:
        for v in cm.get("fixVersions", []) or []:
            if _matches(v):
                all_versions.append(v)
    for b in remaining_bookings:
        v = b.get("version", "")
        if _matches(v):
            all_versions.append(v)

    if not all_versions:
        if filtered:
            return {
                "nextVersion": None,
                "error": f"No deployed versions found for {major}.{minor}.x.",
            }
        return {"nextVersion": None, "error": "No deployed versions found."}

    all_versions.sort(key=cmp_to_key(compare_versions))
    highest = all_versions[-1]
    p = parse_version(highest)
    return {
        "currentHighest": highest,
        "nextVersion": f"{p.major}.{p.minor}.{p.patch + 1}",
        "baseVersion": f"{p.major}.{p.minor}.0",
    }


def deployed_versions(deployed_cms: list[dict]) -> set[str]:
    """Set of semver fixVersions from CMs whose status is actually deployed.

    Only "Done" / "Deployment Completed" statuses count. Callers passing in a
    mixed-status list (e.g. `/next-version` fetches all recent CMs to feed the
    release dropdown) can safely reuse this for cleanup — a CM in "Approved"
    or "In Progress" won't cause the local booking to be purged prematurely.
    """
    out: set[str] = set()
    for cm in deployed_cms:
        if not _is_deployed_status(cm.get("status")):
            continue
        for v in cm.get("fixVersions", []) or []:
            if is_semver(v):
                out.add(v)
    return out


def cleanup_bookings(
    bookings: list[dict],
    *,
    deployed: set[str],
    now: datetime,
    retention_days: int,
) -> tuple[list[dict], list[dict]]:
    """Partition bookings into (kept, removed).

    A booking is removed if EITHER:
      - its version is present in `deployed` (Jira has confirmed the deploy)
        AND its status is NOT `cancelled`, OR
      - its `bookedAt` is older than `now - retention_days` (abandoned)

    Cancelled records are exempt from the deploy-based branch — they stay as
    audit tombstones until they age out. This lets the UI keep showing a
    "Cancelled locally, CM active in Jira" warning on rows where the two
    sources disagree.

    Bookings with missing or unparseable `bookedAt` are kept (never expire from age).
    """
    cutoff = now - timedelta(days=retention_days)
    kept: list[dict] = []
    removed: list[dict] = []

    for b in bookings:
        version = b.get("version")
        is_cancelled = b.get("status") == "cancelled"
        if version in deployed and not is_cancelled:
            removed.append(b)
            continue

        booked_at_raw = b.get("bookedAt")
        if booked_at_raw:
            try:
                booked_at = datetime.fromisoformat(booked_at_raw)
                if booked_at < cutoff:
                    removed.append(b)
                    continue
            except (TypeError, ValueError):
                # Malformed timestamp — keep the booking rather than nuke it
                pass

        kept.append(b)

    return kept, removed
