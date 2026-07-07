"""Client × Component version matrix. Mirrors server/hotfix-booking.js /client-versions."""
from __future__ import annotations

from typing import Any

from .versioning import compare_versions, is_semver


def build_version_matrix(cms: list[dict]) -> dict[str, Any]:
    """For each (client, component, semver-fixVersion) keep the highest version.

    Node reference:
        cms.forEach(cm => cm.clientEnvironments.forEach(client =>
          cm.components.forEach(component => cm.fixVersions.forEach(version => {
            if (!/^\\d+\\.\\d+\\.\\d+$/.test(version)) return;
            const existing = matrix[client][component];
            if (!existing || compareVersions(version, existing.version) > 0) {
              matrix[client][component] = { version, cmKey: cm.key, deployedAt: cm.targetDeploymentDate };
            }
          }))));

    Returns {"matrix": {...}, "components": [sorted unique], "clients": [sorted unique]}.
    """
    matrix: dict[str, dict[str, dict]] = {}

    for cm in cms:
        for client in cm.get("clientEnvironments", []) or []:
            if client not in matrix:
                matrix[client] = {}
            for component in cm.get("components", []) or []:
                for version in cm.get("fixVersions", []) or []:
                    if not is_semver(version):
                        continue
                    existing = matrix[client].get(component)
                    if existing is None or compare_versions(version, existing["version"]) > 0:
                        matrix[client][component] = {
                            "version": version,
                            "cmKey": cm.get("key"),
                            "deployedAt": cm.get("targetDeploymentDate"),
                        }

    all_components: set[str] = set()
    for client_data in matrix.values():
        all_components.update(client_data.keys())

    return {
        "matrix": matrix,
        "components": sorted(all_components),
        "clients": sorted(matrix.keys()),
    }
