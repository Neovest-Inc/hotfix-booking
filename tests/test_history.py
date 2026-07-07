from hotfix_booking.history import (
    calculate_next_version,
    derive_minor_versions,
    deployed_versions,
    merge_hotfixes,
)


def _cm(key, versions, status="Done", components=None, clients=None,
        summary="s", reporter="R", deployed_at="2026-01-01"):
    return {
        "key": key,
        "summary": summary,
        "status": status,
        "components": components or ["Alerts"],
        "clientEnvironments": clients or ["CL001"],
        "fixVersions": versions,
        "targetDeploymentDate": deployed_at,
        "reporter": reporter,
    }


def _booking(version, booked_at="2026-02-01T00:00:00Z", booked_by="Dashboard User"):
    return {
        "id": f"HB-{version}",
        "version": version,
        "components": ["Alerts"],
        "clientEnvironments": ["CL001"],
        "bookedBy": booked_by,
        "bookedAt": booked_at,
        "status": "booked",
    }


class TestDeriveMinorVersions:
    def test_empty_input(self) -> None:
        current, minors = derive_minor_versions([], major=9)
        assert current == 0
        assert minors == [{"major": 9, "minor": 0, "label": "9.0.x"}]

    def test_finds_max_minor_for_major(self) -> None:
        cms = [_cm("CM-1", ["9.92.1"]), _cm("CM-2", ["9.94.5"]), _cm("CM-3", ["8.99.1"])]
        current, minors = derive_minor_versions(cms, major=9)
        assert current == 94
        labels = [m["label"] for m in minors]
        assert labels == ["9.94.x", "9.93.x", "9.92.x", "9.91.x", "9.90.x"]

    def test_clamps_at_zero(self) -> None:
        cms = [_cm("CM-1", ["9.2.0"])]
        current, minors = derive_minor_versions(cms, major=9)
        assert current == 2
        # Should only return 3 items: 9.2.x, 9.1.x, 9.0.x
        assert [m["minor"] for m in minors] == [2, 1, 0]

    def test_ignores_non_semver(self) -> None:
        cms = [_cm("CM-1", ["not-a-version", "9.92.1", "9.95"])]
        current, _ = derive_minor_versions(cms, major=9)
        assert current == 92  # 9.95 is not semver (no patch)


class TestMergeHotfixes:
    def test_deployed_only(self) -> None:
        cms = [_cm("CM-1", ["9.92.10"], status="Deployment Completed")]
        result = merge_hotfixes(cms, [], major=9, target_minor=92)
        assert len(result) == 1
        assert result[0]["type"] == "deployed"
        assert result[0]["version"] == "9.92.10"
        assert result[0]["status"] == "Deployment Completed"
        assert result[0]["cmKey"] == "CM-1"

    def test_booked_only(self) -> None:
        result = merge_hotfixes([], [_booking("9.92.19")], major=9, target_minor=92)
        assert len(result) == 1
        assert result[0]["type"] == "booked"
        assert result[0]["status"] == "Booked"
        assert result[0]["cmKey"] is None
        assert result[0]["reporter"] == "Dashboard User"  # reporter == bookedBy for bookings

    def test_booking_hidden_when_deployed(self) -> None:
        cms = [_cm("CM-1", ["9.92.19"])]
        result = merge_hotfixes(cms, [_booking("9.92.19")], major=9, target_minor=92)
        assert len(result) == 1
        assert result[0]["type"] == "deployed"

    def test_filters_by_major_minor(self) -> None:
        cms = [
            _cm("CM-A", ["9.92.10"]),
            _cm("CM-B", ["9.93.1"]),   # different minor
            _cm("CM-C", ["8.92.1"]),   # different major
        ]
        result = merge_hotfixes(cms, [], major=9, target_minor=92)
        assert [h["version"] for h in result] == ["9.92.10"]

    def test_sorted_descending(self) -> None:
        cms = [_cm("CM-1", ["9.92.2"]), _cm("CM-2", ["9.92.10"]), _cm("CM-3", ["9.92.5"])]
        result = merge_hotfixes(cms, [_booking("9.92.19")], major=9, target_minor=92)
        assert [h["version"] for h in result] == ["9.92.19", "9.92.10", "9.92.5", "9.92.2"]

    def test_one_entry_per_matching_fixversion(self) -> None:
        # CM with two matching fixVersions should produce two hotfixes
        cms = [_cm("CM-1", ["9.92.10", "9.92.11"])]
        result = merge_hotfixes(cms, [], major=9, target_minor=92)
        assert len(result) == 2
        assert {h["version"] for h in result} == {"9.92.10", "9.92.11"}


class TestCalculateNextVersion:
    def test_empty_returns_error(self) -> None:
        result = calculate_next_version([], [])
        assert result == {"nextVersion": None, "error": "No deployed versions found."}

    def test_uses_max_and_increments_patch(self) -> None:
        cms = [_cm("CM-1", ["9.92.5"]), _cm("CM-2", ["9.92.10"])]
        result = calculate_next_version(cms, [])
        assert result["currentHighest"] == "9.92.10"
        assert result["nextVersion"] == "9.92.11"
        assert result["baseVersion"] == "9.92.0"

    def test_bookings_counted_toward_max(self) -> None:
        cms = [_cm("CM-1", ["9.92.10"])]
        bookings = [_booking("9.92.15")]
        result = calculate_next_version(cms, bookings)
        assert result["currentHighest"] == "9.92.15"
        assert result["nextVersion"] == "9.92.16"

    def test_crosses_minor_boundary(self) -> None:
        cms = [_cm("CM-1", ["9.92.99"]), _cm("CM-2", ["9.93.0"])]
        result = calculate_next_version(cms, [])
        assert result["nextVersion"] == "9.93.1"

    def test_ignores_non_semver(self) -> None:
        cms = [_cm("CM-1", ["9.92.10", "9.92-rc1", "latest"])]
        result = calculate_next_version(cms, [])
        assert result["currentHighest"] == "9.92.10"


class TestDeployedVersions:
    def test_returns_semver_set(self) -> None:
        cms = [
            {"fixVersions": ["9.92.10", "not-semver"]},
            {"fixVersions": ["9.93.1", "9.92.10"]},
        ]
        assert deployed_versions(cms) == {"9.92.10", "9.93.1"}

    def test_missing_fixversions_key(self) -> None:
        assert deployed_versions([{}]) == set()
