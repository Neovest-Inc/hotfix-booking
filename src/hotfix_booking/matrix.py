"""Client × Component version matrix. Mirrors server/hotfix-booking.js /client-versions."""
from __future__ import annotations

from typing import Any

from .versioning import compare_versions, is_semver


# Statuses that a hotfix CM must have to count as "deployed" for matrix
# purposes — i.e. it feeds the cell's headline version. Compared
# case-insensitively so mixed-case Jira statuses (`Done`, `done`) both hit.
DEPLOYED_STATUSES: frozenset[str] = frozenset({
    "deployment completed",
    "done",
    "global review",
})

# Statuses that we ignore in BOTH buckets (headline version AND in-flight
# chip). Anything terminally cancelled / rolled back is dead work — it must
# not clutter the matrix.
_TERMINAL_CANCELLED: frozenset[str] = frozenset({
    "rollback",
    "rejected",
    "cancelled",
})

# Union of "already visible as the deployed version" + "hidden by design".
# Everything else is IN-FLIGHT and shows in the cell's popover.
EXCLUDED_FROM_INFLIGHT: frozenset[str] = DEPLOYED_STATUSES | _TERMINAL_CANCELLED


def _status_key(cm: dict) -> str:
    """Normalised status string for bucketing (lower + strip). Empty on missing."""
    return (cm.get("status") or "").strip().lower()


def build_version_matrix(
    cms: list[dict], in_flight_only: bool = False
) -> dict[str, Any]:
    """For each (client, component) cell, bucket CMs by status:

    * DEPLOYED (`Deployment Completed`, `Done`, `Global Review`) →
      keep the highest semver as the cell's headline `version`.
    * IN-FLIGHT (anything not deployed and not terminally cancelled) →
      appended to the cell's `inflight` list, deduped by version,
      sorted DESC.
    * EXCLUDED (`Rollback`, `Rejected`, `Cancelled`, missing/blank status) →
      invisible.

    When ``in_flight_only=True``, deployed statuses are reclassified as
    EXCLUDED — the headline `version`/`cmKey`/`deployedAt` fields stay
    None on every cell, and only CMs currently moving through the workflow
    (`In Progress`, `QA Approved`, `Ready for Deployment`, ...) populate
    the cell's `inflight` list. Cells with no in-flight CMs are omitted.

    Cells that only have in-flight CMs (no deploys yet, or deploys hidden
    by ``in_flight_only``) still appear in the matrix with `version=None`
    so the UI can render just the chip.

    Returns
    -------
    ``{"matrix": {client: {component: cell}}, "components": [...], "clients": [...]}``
    where each ``cell`` is
    ``{"version": str|None, "cmKey": str|None, "deployedAt": str|None,
       "inflight": [{"version": str, "status": str, "cmKey": str}, ...]}``.
    """
    # Effective bucket boundaries — narrow when caller only wants in-flight.
    deployed_statuses: frozenset[str] = (
        frozenset() if in_flight_only else DEPLOYED_STATUSES
    )
    excluded_statuses: frozenset[str] = (
        _TERMINAL_CANCELLED | DEPLOYED_STATUSES if in_flight_only else _TERMINAL_CANCELLED
    )

    matrix: dict[str, dict[str, dict]] = {}
    # Per-cell in-flight staging: (client, component) -> {version -> (status, cmKey)}
    # Dedupe by version so multiple in-flight CMs at the same version collapse
    # into one popover row (rare, but happens when a CM gets cloned).
    inflight_stage: dict[tuple[str, str], dict[str, tuple[str, str]]] = {}

    for cm in cms:
        status = _status_key(cm)
        if not status:
            # Defensive: unknown status shouldn't leak into either bucket.
            continue

        is_deployed = status in deployed_statuses
        is_excluded = status in excluded_statuses
        if is_excluded:
            continue

        clients = cm.get("clientEnvironments") or []
        components = cm.get("components") or []
        versions = [v for v in (cm.get("fixVersions") or []) if is_semver(v)]
        if not clients or not components or not versions:
            continue

        for client in clients:
            for component in components:
                for version in versions:
                    if is_deployed:
                        if client not in matrix:
                            matrix[client] = {}
                        existing = matrix[client].get(component)
                        if existing is None or compare_versions(version, existing["version"]) > 0:
                            matrix[client][component] = {
                                "version": version,
                                "cmKey": cm.get("key"),
                                "deployedAt": cm.get("targetDeploymentDate"),
                            }
                    else:
                        cell_key = (client, component)
                        bucket = inflight_stage.setdefault(cell_key, {})
                        # Dedupe by version — first seen wins (Jira order is
                        # already `created DESC`, so newer CMs land first).
                        if version not in bucket:
                            bucket[version] = (cm.get("status") or "", cm.get("key") or "")

    # Materialise in-flight into the matrix, creating placeholder cells for
    # clients/components that have never seen a deployed CM.
    for (client, component), version_map in inflight_stage.items():
        if client not in matrix:
            matrix[client] = {}
        cell = matrix[client].get(component)
        if cell is None:
            cell = {
                "version": None,
                "cmKey": None,
                "deployedAt": None,
            }
            matrix[client][component] = cell
        # Sorted DESC (newest first) so the popover reads top-down.
        sorted_versions = sorted(
            version_map.keys(),
            key=lambda v: v,
            reverse=False,
        )
        # Semver-aware sort (string sort would misorder e.g. "9.92.5" vs "9.92.20").
        sorted_versions.sort(key=lambda v: [int(part) for part in v.split(".")], reverse=True)
        cell["inflight"] = [
            {
                "version": v,
                "status": version_map[v][0],
                "cmKey": version_map[v][1],
            }
            for v in sorted_versions
        ]

    # Every cell needs an `inflight` key so the frontend can iterate uniformly,
    # even when the cell only ever had deployed CMs.
    for client_data in matrix.values():
        for cell in client_data.values():
            cell.setdefault("inflight", [])

    all_components: set[str] = set()
    for client_data in matrix.values():
        all_components.update(client_data.keys())

    return {
        "matrix": matrix,
        "components": sorted(all_components),
        "clients": sorted(matrix.keys()),
    }
