"""Side-by-side environment comparison.

Given the last 120 days of Jira CMs + the local booking store, produce a
row-per-component view where each row has two cells (one per environment).
Each cell holds three buckets:

* ``deployed`` — the highest-semver CM that actually shipped (status in
  ``Deployment Completed`` / ``Done`` / ``Global Review``). Answers
  "what's really running there".
* ``inflight`` — CMs currently moving through the workflow (any status
  that isn't deployed and isn't terminally cancelled). Deduped by
  version, sorted DESC.
* ``booked``  — local bookings targeting this (env, component) that
  aren't cancelled. Sorted by version DESC.

Rows appear whenever either side has at least one non-empty bucket. Row
coloring (ahead / behind / equal / asymmetric) is intentionally left to
the client — this module returns raw versions and the JS decides visuals.
"""
from __future__ import annotations

from typing import Any

from .matrix import DEPLOYED_STATUSES, EXCLUDED_FROM_INFLIGHT
from .versioning import compare_versions, is_semver


def _cell_template() -> dict:
    return {"deployed": None, "inflight": [], "booked": []}


def _seed_cell(
    per_env: dict[str, dict[str, dict]], env: str, component: str
) -> dict:
    """Ensure ``per_env[env][component]`` exists, return the cell dict."""
    bucket = per_env.setdefault(env, {})
    cell = bucket.get(component)
    if cell is None:
        cell = _cell_template()
        bucket[component] = cell
    return cell


def _accept_deployed(cell: dict, cm: dict, version: str) -> None:
    """Update the deployed slot iff `version` is strictly higher."""
    current = cell["deployed"]
    if current is None or compare_versions(version, current["version"]) > 0:
        cell["deployed"] = {
            "version": version,
            "cmKey": cm.get("key"),
            "deployedAt": cm.get("targetDeploymentDate"),
            "status": cm.get("status") or "",
        }


def _accept_inflight(cell: dict, cm: dict, version: str) -> None:
    """Dedupe by version so clones at the same version collapse."""
    for existing in cell["inflight"]:
        if existing["version"] == version:
            return
    cell["inflight"].append(
        {
            "version": version,
            "status": cm.get("status") or "",
            "cmKey": cm.get("key") or "",
        }
    )


def _accept_booking(cell: dict, b: dict) -> None:
    cell["booked"].append(
        {
            "version": b.get("version"),
            "bookingId": b.get("id"),
            "bookedBy": b.get("bookedBy") or "",
            "bookedAt": b.get("bookedAt") or "",
        }
    )


def _semver_key(v: str | None) -> tuple[int, int, int]:
    """Numeric tuple for semver sort. Non-semver sinks to the bottom."""
    if not v or not is_semver(v):
        return (-1, -1, -1)
    a, b, c = (int(x) for x in v.split("."))
    return (a, b, c)


def _cell_is_empty(cell: dict) -> bool:
    return (
        cell["deployed"] is None
        and not cell["inflight"]
        and not cell["booked"]
    )


def build_environments_compare(
    cms: list[dict],
    bookings: list[dict],
    env_a: str,
    env_b: str,
) -> dict[str, Any]:
    """Compute the side-by-side compare payload for the Compare tab.

    Parameters
    ----------
    cms : list[dict]
        CMs as returned by ``jira_client._issue_to_cm``.
    bookings : list[dict]
        Local bookings from ``store.load_bookings``. Cancelled bookings
        are skipped.
    env_a, env_b : str
        Client-environment names to compare. Case-sensitive exact match
        against each CM's ``clientEnvironments`` list (same contract as
        ``matrix.py``). ``env_a`` may equal ``env_b``.

    Returns
    -------
    dict
        ``{
            "envA": str,
            "envB": str,
            "rows": [
                {
                    "component": str,
                    "a": {"deployed": {...}|None, "inflight": [...], "booked": [...]},
                    "b": {"deployed": {...}|None, "inflight": [...], "booked": [...]},
                },
                ...
            ],
        }``
        Rows are sorted case-insensitively by component. Only components
        with at least one non-empty bucket on either side appear.
    """
    envs = (env_a, env_b)
    # {env: {component: cell}}
    per_env: dict[str, dict[str, dict]] = {}

    for cm in cms:
        status = (cm.get("status") or "").strip().lower()
        if not status:
            continue
        is_deployed = status in DEPLOYED_STATUSES
        # EXCLUDED_FROM_INFLIGHT = deployed statuses ∪ terminal cancelled.
        # If a CM isn't deployed AND is in that set → terminal cancelled →
        # skip entirely.
        if not is_deployed and status in EXCLUDED_FROM_INFLIGHT:
            continue

        cm_envs = cm.get("clientEnvironments") or []
        cm_components = cm.get("components") or []
        cm_versions = [v for v in (cm.get("fixVersions") or []) if is_semver(v)]
        if not cm_envs or not cm_components or not cm_versions:
            continue

        for env in envs:
            if env not in cm_envs:
                continue
            for component in cm_components:
                cell = _seed_cell(per_env, env, component)
                for version in cm_versions:
                    if is_deployed:
                        _accept_deployed(cell, cm, version)
                    else:
                        _accept_inflight(cell, cm, version)

    for booking_rec in bookings:
        if (booking_rec.get("status") or "").lower() == "cancelled":
            continue
        b_envs = booking_rec.get("clientEnvironments") or []
        b_components = booking_rec.get("components") or []
        version = booking_rec.get("version")
        if not b_envs or not b_components or not is_semver(version or ""):
            continue
        for env in envs:
            if env not in b_envs:
                continue
            for component in b_components:
                cell = _seed_cell(per_env, env, component)
                _accept_booking(cell, booking_rec)

    # Union of components across both envs (only those actually touched).
    components = sorted(
        {c for env_map in per_env.values() for c in env_map.keys()},
        key=str.lower,
    )

    rows: list[dict[str, Any]] = []
    for component in components:
        cell_a = per_env.get(env_a, {}).get(component) or _cell_template()
        cell_b = per_env.get(env_b, {}).get(component) or _cell_template()
        # Deterministic ordering — semver-DESC for both in-flight and booked.
        cell_a["inflight"].sort(key=lambda i: _semver_key(i["version"]), reverse=True)
        cell_b["inflight"].sort(key=lambda i: _semver_key(i["version"]), reverse=True)
        cell_a["booked"].sort(key=lambda b: _semver_key(b["version"]), reverse=True)
        cell_b["booked"].sort(key=lambda b: _semver_key(b["version"]), reverse=True)
        if _cell_is_empty(cell_a) and _cell_is_empty(cell_b):
            continue
        rows.append({"component": component, "a": cell_a, "b": cell_b})

    return {"envA": env_a, "envB": env_b, "rows": rows}
