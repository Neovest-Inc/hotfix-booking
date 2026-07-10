"""Booking dependency graph — computes DAG parents and finds direct children.

Two bookings A and B "overlap" iff they share at least one (client, component)
pair: `A.clients ∩ B.clients ≠ ∅` AND `A.components ∩ B.components ≠ ∅`. The
overlap is the dependency edge — a new booking must be based on every prior
booking that touches any of its (client, component) cells.

For each cell in a new booking, its immediate parent is the **most-recent
non-cancelled** prior booking that also covers that cell. The booking's
`parents` field is the deduped union across cells.

Cancelling a booking rebases each direct child by recomputing that child's
parents against the store as-if the cancelled booking never existed.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from .versioning import compare_versions, is_semver, parse_version


def overlaps(a: dict, b: dict) -> bool:
    """True iff bookings A and B share at least one (client, component) cell.

    Missing / non-list fields are treated as empty — the function never raises
    even on malformed input, so callers doing bulk graph updates can trust it.
    """
    a_clients = set(a.get("clientEnvironments") or [])
    a_components = set(a.get("components") or [])
    b_clients = set(b.get("clientEnvironments") or [])
    b_components = set(b.get("components") or [])
    if not a_clients or not a_components or not b_clients or not b_components:
        return False
    if not (a_clients & b_clients):
        return False
    if not (a_components & b_components):
        return False
    return True


def _same_release_line(a: dict, b: dict) -> bool:
    va = a.get("version") or ""
    vb = b.get("version") or ""
    if not is_semver(va) or not is_semver(vb):
        return False
    pa = parse_version(va)
    pb = parse_version(vb)
    return pa.major == pb.major and pa.minor == pb.minor


def _sort_key_most_recent_first(b: dict) -> tuple:
    """Sort key that puts the most-recent booking first.

    Primary key: `bookedAt` (parsed as datetime; unparseable → epoch).
    Tie-break: patch version DESC (higher patch wins) — deterministic when two
    bookings share a timestamp, which the id-factory (millisecond epoch) can
    occasionally produce in tight loops.
    """
    at_raw = b.get("bookedAt") or ""
    try:
        at = datetime.fromisoformat(at_raw)
    except (TypeError, ValueError):
        at = datetime.min
    version = b.get("version") or "0.0.0"
    patch = parse_version(version).patch if is_semver(version) else 0
    # Negative timestamp/patch so ascending sort yields most-recent-first
    return (-at.timestamp() if at != datetime.min else 0, -patch)


def _active_priors(new_booking: dict, all_bookings: Iterable[dict]) -> list[dict]:
    """Prior non-cancelled bookings on the SAME release line, excluding self.

    "Prior" means booked strictly BEFORE `new_booking`. This matters during a
    rebase: when we recompute a child's parents after its own parent has been
    cancelled, we must not consider later bookings as candidate parents — a
    booking's dependencies are frozen at its own creation time (only the DAG
    edge into the cancelled record gets rewired, not the timeline).

    Sorted most-recent-first (see `_sort_key_most_recent_first`).
    """
    new_id = new_booking.get("id")
    new_at_raw = new_booking.get("bookedAt") or ""
    try:
        new_at = datetime.fromisoformat(new_at_raw)
    except (TypeError, ValueError):
        new_at = None
    out: list[dict] = []
    for b in all_bookings:
        if b.get("id") == new_id:
            continue
        if b.get("status") == "cancelled":
            continue
        if not _same_release_line(new_booking, b):
            continue
        if new_at is not None:
            b_at_raw = b.get("bookedAt") or ""
            try:
                b_at = datetime.fromisoformat(b_at_raw)
            except (TypeError, ValueError):
                b_at = None
            if b_at is not None and b_at >= new_at:
                # Same or later timestamp than new_booking → not a prior.
                # (Same-timestamp is treated as "not a prior" to avoid the
                # ambiguity of two records claiming to be each other's parent.)
                continue
        out.append(b)
    out.sort(key=_sort_key_most_recent_first)
    return out


def compute_parents(new_booking: dict, all_bookings: Iterable[dict]) -> list[str]:
    """Return the list of booking IDs that `new_booking` depends on.

    Algorithm: for each (client, component) cell of `new_booking`, find the
    most-recent non-cancelled prior booking that also covers that cell. Deduplicate
    and preserve most-recent-first order.

    An empty result means "based on the baseline `major.minor.0`" — the caller
    does not need to substitute a sentinel; empty is the wire representation.
    """
    new_clients = list(new_booking.get("clientEnvironments") or [])
    new_components = list(new_booking.get("components") or [])
    if not new_clients or not new_components:
        return []

    priors = _active_priors(new_booking, all_bookings)
    if not priors:
        return []

    seen: set[str] = set()
    result: list[str] = []
    # For each cell, walk priors most-recent-first and take the first overlap.
    for client in new_clients:
        for component in new_components:
            for b in priors:
                b_clients = set(b.get("clientEnvironments") or [])
                b_components = set(b.get("components") or [])
                if client in b_clients and component in b_components:
                    bid = b.get("id")
                    if bid and bid not in seen:
                        seen.add(bid)
                        result.append(bid)
                    break
    # `priors` is already most-recent-first; because we appended in scan order
    # (cell-by-cell), the emitted list needs a final re-sort by recency so
    # multi-parent output is stable regardless of client/component input order.
    priors_by_id = {b.get("id"): b for b in priors}
    result.sort(key=lambda bid: _sort_key_most_recent_first(priors_by_id[bid]))
    return result


def direct_children(cancelled_id: str, all_bookings: Iterable[dict]) -> list[dict]:
    """Return non-cancelled bookings whose CURRENT `parents` include `cancelled_id`.

    Sorted by version ASC so callers can process rebases deterministically.
    Uses `parents` (mutable, current basis), NOT `originalParents`.
    """
    out: list[dict] = []
    for b in all_bookings:
        if b.get("id") == cancelled_id:
            continue
        if b.get("status") == "cancelled":
            continue
        parents = b.get("parents") or []
        if cancelled_id in parents:
            out.append(b)
    out.sort(key=lambda b: 0 if not is_semver(b.get("version") or "") else parse_version(b["version"]).patch)
    return out


def parent_versions(parent_ids: list[str], all_bookings: Iterable[dict]) -> list[str]:
    """Map a list of parent booking IDs to their versions (for UI + API responses).

    IDs not found in `all_bookings` are dropped — should not happen in practice
    but keeps the helper defensive against orphaned references.
    """
    by_id = {b.get("id"): b.get("version") for b in all_bookings}
    return [by_id[bid] for bid in parent_ids if bid in by_id and by_id[bid]]


# ---------------------------------------------------------------------------
# Jira-CM-as-parent bridge
# ---------------------------------------------------------------------------
# CM statuses that mean the change was abandoned and is NOT going to ship.
# These CMs are excluded as parent candidates — a subsequent booking must not
# be based on a change that will never land. Mirrors the exclusion rule for
# locally-cancelled bookings, applied to Jira CMs originating from the
# legacy Teams-chat workflow.
_INELIGIBLE_CM_STATUSES = frozenset({"rollback", "rejected", "cancelled"})


def cm_to_pseudo_booking(cm: dict, *, major: int, minor: int) -> dict | None:
    """Convert a Jira CM into a booking-shaped dict for `compute_parents`.

    Returns None if the CM cannot be a valid parent for the given release
    line — either because it has no matching fixVersion, no components /
    clients, or its status is terminal-negative (Rollback / Rejected /
    Cancelled).

    The returned dict uses `id = "jira:<KEY>"` so callers can distinguish
    pseudo-priors from real local booking IDs (`HB-<epoch>`), and the frontend
    can render them by resolving the CM key back to its version via the
    hotfix history it already has in memory.

    `bookedAt` is populated from Jira's `createdAt` (issue creation time).
    Missing or unparseable timestamps degrade gracefully in
    `_sort_key_most_recent_first` (treated as epoch), which is safe: the CM
    still counts as a valid prior — it just sorts older than any dated
    booking, which is acceptable behaviour for a legacy record with no
    reliable timestamp.
    """
    status = (cm.get("status") or "").strip().lower()
    if status in _INELIGIBLE_CM_STATUSES:
        return None

    components = list(cm.get("components") or [])
    clients = list(cm.get("clientEnvironments") or [])
    if not components or not clients:
        return None

    matching_version: str | None = None
    for v in cm.get("fixVersions") or []:
        if not is_semver(v):
            continue
        parsed = parse_version(v)
        if parsed.major == major and parsed.minor == minor:
            matching_version = v
            break
    if matching_version is None:
        return None

    key = cm.get("key") or ""
    return {
        "id": f"jira:{key}",
        "version": matching_version,
        "components": components,
        "clientEnvironments": clients,
        "bookedAt": cm.get("createdAt") or "",
        "status": "booked",
    }
