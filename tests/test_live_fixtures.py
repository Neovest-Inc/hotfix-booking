"""Integration-shape tests using real Jira fixtures captured from live Jira.

These tests are less strict than the unit tests — they don't assert exact values
(those change every time Jira is re-queried). Instead they verify that:

  1. Real Jira responses parse cleanly through the code path
  2. Aggregations produce sensible, well-typed output
  3. The Jira response shape hasn't drifted from what the code expects

Regenerate the fixtures with:

    node tools/capture-hotfix-fixtures.js   # from the val-dashboard repo

Skips itself gracefully if `tests/fixtures/jira-live/` isn't present.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hotfix_booking.history import (
    calculate_next_version,
    derive_minor_versions,
    deployed_versions,
    merge_hotfixes,
)
from hotfix_booking.jira_client import _issue_to_cm
from hotfix_booking.matrix import build_version_matrix

LIVE_DIR = Path(__file__).parent / "fixtures" / "jira-live"

pytestmark = pytest.mark.skipif(
    not LIVE_DIR.exists(),
    reason="Live Jira fixtures not captured — run tools/capture-hotfix-fixtures.js",
)


def _load(name: str) -> dict | list:
    return json.loads((LIVE_DIR / name).read_text(encoding="utf-8"))


def _cms_from_search(name: str) -> list[dict]:
    payload = _load(name)
    return [_issue_to_cm(i) for i in payload.get("issues", [])]


class TestFieldOptionsLive:
    def test_components_shape(self) -> None:
        data = _load("cm_components.json")
        assert isinstance(data, list)
        assert len(data) > 0, "expected some CM components from live Jira"
        for c in data:
            assert isinstance(c.get("id"), str)
            assert isinstance(c.get("name"), str) and c["name"]

    def test_client_options_shape(self) -> None:
        data = _load("client_options.json")
        assert isinstance(data.get("values"), list)
        assert len(data["values"]) > 0, "expected some client options"
        for c in data["values"]:
            assert isinstance(c.get("id"), str)
            assert isinstance(c.get("value"), str) and c["value"]


class TestSearchDeployedLive:
    def test_maps_cleanly(self) -> None:
        cms = _cms_from_search("search_deployed.json")
        assert len(cms) > 0, "expected some deployed CMs"
        for cm in cms:
            assert isinstance(cm["key"], str)
            assert isinstance(cm["components"], list)
            assert isinstance(cm["fixVersions"], list)
            assert isinstance(cm["clientEnvironments"], list)

    def test_next_version_is_valid_semver(self) -> None:
        import re
        cms = _cms_from_search("search_deployed.json")
        result = calculate_next_version(cms, [])
        if result.get("nextVersion") is None:
            pytest.skip("no semver versions in live deployed data")
        assert re.match(r"^\d+\.\d+\.\d+$", result["nextVersion"])
        assert re.match(r"^\d+\.\d+\.\d+$", result["currentHighest"])
        assert re.match(r"^\d+\.\d+\.\d+$", result["baseVersion"])

    def test_matrix_produces_output(self) -> None:
        cms = _cms_from_search("search_deployed.json")
        result = build_version_matrix(cms)
        assert isinstance(result["matrix"], dict)
        assert isinstance(result["components"], list)
        assert isinstance(result["clients"], list)
        # If there are any deployed CMs with clients+components+semver, matrix != empty
        has_semver_deployment = any(
            cm["clientEnvironments"] and cm["components"] and any(
                v.count(".") == 2 and v.replace(".", "").isdigit() for v in cm["fixVersions"]
            )
            for cm in cms
        )
        if has_semver_deployment:
            assert len(result["matrix"]) > 0


class TestSearchAllLive:
    def test_derives_current_minor(self) -> None:
        cms = _cms_from_search("search_all.json")
        current, minors = derive_minor_versions(cms, major=9)
        assert current >= 0
        # up to 5 items, all with major=9, minor descending, clamped ≥ 0
        assert len(minors) <= 5
        prev = None
        for m in minors:
            assert m["major"] == 9
            assert m["minor"] >= 0
            if prev is not None:
                assert m["minor"] < prev
            prev = m["minor"]


class TestHistoryMergeLive:
    def test_merges_by_version_fixture(self) -> None:
        # Pick any search_by_version_*.json fixture that exists
        by_ver = list(LIVE_DIR.glob("search_by_version_*.json"))
        if not by_ver:
            pytest.skip("no search_by_version_* fixture present")
        # Extract major.minor from filename: search_by_version_9_97.json → 9, 97
        stem = by_ver[0].stem  # search_by_version_9_97
        _, _, _, maj_s, min_s = stem.split("_")
        major, minor = int(maj_s), int(min_s)
        cms = _cms_from_search(by_ver[0].name)
        hotfixes = merge_hotfixes(cms, [], major=major, target_minor=minor)
        # Every entry belongs to this major.minor and is marked deployed
        for h in hotfixes:
            parts = h["version"].split(".")
            assert int(parts[0]) == major and int(parts[1]) == minor
            assert h["type"] == "deployed"

    def test_deployed_versions_are_all_semver(self) -> None:
        cms = _cms_from_search("search_deployed.json")
        versions = deployed_versions(cms)
        import re
        for v in versions:
            assert re.match(r"^\d+\.\d+\.\d+$", v)
