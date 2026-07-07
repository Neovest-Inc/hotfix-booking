"""Hotfix history: merges deployed CMs + pending bookings for a given (major, minor).

Also owns booking cleanup rules — both deploy-based and age-based.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from functools import cmp_to_key
from typing import Any

from .versioning import compare_versions, is_semver, parse_version


def derive_minor_versions(
    recent_cms: list[dict], major: int, count: int = 5
) -> tuple[int, list[dict]]:
    """From recent CMs find the highest `minor` where major matches `major`.

    Returns (current_minor, [{"major","minor","label"} for last `count` minors clamped ≥0]).
    Matches Node's `for (let i = 0; i < 5; i++) if (currentMinor - i >= 0)`.
    """
    current_minor = 0
    for cm in recent_cms:
        for version in cm.get("fixVersions", []) or []:
            if is_semver(version):
                v = parse_version(version)
                if v.major == major and v.minor > current_minor:
                    current_minor = v.minor

    minors = []
    for i in range(count):
        m = current_minor - i
        if m >= 0:
            minors.append({"major": major, "minor": m, "label": f"{major}.{m}.x"})
    return current_minor, minors


def merge_hotfixes(
    version_cms: list[dict],
    bookings: list[dict],
    major: int,
    target_minor: int,
) -> list[dict]:
    """Merge deployed CMs (matching major.minor) with pending bookings.

    Booking is included only if its version isn't already in the deployed list.
    Sorted by version desc.

    Deployed entry shape:  { version, type:'deployed', cmKey, summary, status, components,
                             clientEnvironments, deployedAt, reporter }
    Booked   entry shape:  { version, type:'booked', cmKey:None, summary:None, status:'Booked',
                             components, clientEnvironments, bookedAt, bookedBy, reporter=bookedBy }
    """
    hotfixes: list[dict] = []

    for cm in version_cms:
        for version in cm.get("fixVersions", []) or []:
            if not is_semver(version):
                continue
            v = parse_version(version)
            if v.major == major and v.minor == target_minor:
                hotfixes.append(
                    {
                        "version": version,
                        "type": "deployed",
                        "cmKey": cm.get("key"),
                        "summary": cm.get("summary"),
                        "status": cm.get("status"),
                        "components": cm.get("components", []),
                        "clientEnvironments": cm.get("clientEnvironments", []),
                        "deployedAt": cm.get("targetDeploymentDate"),
                        "reporter": cm.get("reporter"),
                    }
                )

    for booking in bookings:
        v = parse_version(booking.get("version", ""))
        if v.major != major or v.minor != target_minor:
            continue
        already_deployed = any(
            h["version"] == booking["version"] and h["type"] == "deployed" for h in hotfixes
        )
        if already_deployed:
            continue
        hotfixes.append(
            {
                "version": booking["version"],
                "type": "booked",
                "cmKey": None,
                "summary": None,
                "status": "Booked",
                "components": booking.get("components", []),
                "clientEnvironments": booking.get("clientEnvironments", []),
                "bookedAt": booking.get("bookedAt"),
                "bookedBy": booking.get("bookedBy"),
                "reporter": booking.get("bookedBy"),
            }
        )

    # Sort descending by version
    hotfixes.sort(key=cmp_to_key(lambda a, b: compare_versions(b["version"], a["version"])))
    return hotfixes


def calculate_next_version(
    deployed_cms: list[dict], remaining_bookings: list[dict]
) -> dict[str, Any]:
    """Compute { currentHighest, nextVersion, baseVersion } or { nextVersion: None, error }.

    Node reference:
        allVersions = deployed fixVersions (semver) + remaining booking versions (semver)
        highest = sort(allVersions).pop()
        next = `${major}.${minor}.${patch+1}`
    """
    all_versions: list[str] = []
    for cm in deployed_cms:
        for v in cm.get("fixVersions", []) or []:
            if is_semver(v):
                all_versions.append(v)
    for b in remaining_bookings:
        v = b.get("version", "")
        if is_semver(v):
            all_versions.append(v)

    if not all_versions:
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
    """Set of semver fixVersions from deployed CMs (used for booking auto-cleanup)."""
    out: set[str] = set()
    for cm in deployed_cms:
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
      - its version is present in `deployed` (Jira has confirmed the deploy), OR
      - its `bookedAt` is older than `now - retention_days` (abandoned)

    Bookings with missing or unparseable `bookedAt` are kept (never expire from age).
    """
    cutoff = now - timedelta(days=retention_days)
    kept: list[dict] = []
    removed: list[dict] = []

    for b in bookings:
        version = b.get("version")
        if version in deployed:
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
