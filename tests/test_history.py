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
        current_major, current_minor, minors = derive_minor_versions([])
        assert current_major == 0
        assert current_minor == 0
        assert minors == []

    def test_returns_actual_pairs_from_data(self) -> None:
        # Includes a gap (no 9.93.x): must NOT invent a phantom option.
        cms = [_cm("CM-1", ["9.94.5"]), _cm("CM-2", ["9.92.1"])]
        current_major, current_minor, minors = derive_minor_versions(cms)
        assert (current_major, current_minor) == (9, 94)
        labels = [m["label"] for m in minors]
        assert labels == ["9.94.x", "9.92.x"]

    def test_crosses_major_boundary(self) -> None:
        """After 9.99, when 10.0.0 exists in Jira, it must appear at the top."""
        cms = [
            _cm("CM-A", ["9.99.5"]),
            _cm("CM-B", ["10.0.1"]),
            _cm("CM-C", ["10.0.0"]),
            _cm("CM-D", ["9.98.3"]),
        ]
        current_major, current_minor, minors = derive_minor_versions(cms)
        assert (current_major, current_minor) == (10, 0)
        labels = [m["label"] for m in minors]
        assert labels == ["10.0.x", "9.99.x", "9.98.x"]

    def test_top_n_defaults_to_8(self) -> None:
        cms = [_cm(f"CM-{i}", [f"9.{i}.0"]) for i in range(1, 15)]
        _, _, minors = derive_minor_versions(cms)
        assert len(minors) == 8
        # Latest 8 minors: 14 down to 7
        assert [m["minor"] for m in minors] == [14, 13, 12, 11, 10, 9, 8, 7]

    def test_count_override(self) -> None:
        cms = [_cm(f"CM-{i}", [f"9.{i}.0"]) for i in range(1, 10)]
        _, _, minors = derive_minor_versions(cms, count=3)
        assert [m["minor"] for m in minors] == [9, 8, 7]

    def test_deduplicates_repeated_pairs(self) -> None:
        cms = [_cm("A", ["9.94.1"]), _cm("B", ["9.94.5"]), _cm("C", ["9.94.10"])]
        _, _, minors = derive_minor_versions(cms)
        # 9.94.x appears once, not three times
        assert [m["minor"] for m in minors] == [94]

    def test_ignores_non_semver(self) -> None:
        cms = [_cm("CM-1", ["not-a-version", "9.92.1", "9.95"])]
        current_major, current_minor, minors = derive_minor_versions(cms)
        assert (current_major, current_minor) == (9, 92)  # "9.95" is not semver
        assert [m["minor"] for m in minors] == [92]

    def test_entry_shape(self) -> None:
        cms = [_cm("CM-1", ["10.0.5"])]
        _, _, minors = derive_minor_versions(cms)
        assert minors == [{"major": 10, "minor": 0, "label": "10.0.x"}]


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


class TestCalculateNextVersionWithMinorFilter:
    """When given `major` + `minor`, only versions on that specific minor line count."""

    def test_filter_isolates_specific_minor(self) -> None:
        cms = [
            _cm("CM-1", ["9.97.5"]),
            _cm("CM-2", ["9.95.3"]),
            _cm("CM-3", ["9.95.7"]),
        ]
        result = calculate_next_version(cms, [], major=9, minor=95)
        assert result["currentHighest"] == "9.95.7"
        assert result["nextVersion"] == "9.95.8"
        assert result["baseVersion"] == "9.95.0"

    def test_bookings_filtered_by_same_minor(self) -> None:
        cms = [_cm("CM-1", ["9.95.3"])]
        bookings = [_booking("9.95.5"), _booking("9.97.10")]
        result = calculate_next_version(cms, bookings, major=9, minor=95)
        assert result["nextVersion"] == "9.95.6"

    def test_no_matches_for_minor_returns_specific_error(self) -> None:
        cms = [_cm("CM-1", ["9.97.5"])]
        result = calculate_next_version(cms, [], major=9, minor=95)
        assert result["nextVersion"] is None
        assert "9.95" in result["error"]

    def test_bookings_alone_are_enough_for_the_minor(self) -> None:
        # No deployed CMs for 9.95 but a booking exists — should still compute next.
        cms = [_cm("CM-1", ["9.97.5"])]
        bookings = [_booking("9.95.2")]
        result = calculate_next_version(cms, bookings, major=9, minor=95)
        assert result["nextVersion"] == "9.95.3"

    def test_major_also_respected(self) -> None:
        cms = [_cm("CM-1", ["8.95.10"]), _cm("CM-2", ["9.95.3"])]
        result = calculate_next_version(cms, [], major=9, minor=95)
        # 8.95.10 must not be considered when major=9
        assert result["currentHighest"] == "9.95.3"


class TestDeployedVersions:
    def test_returns_semver_set(self) -> None:
        cms = [
            {"fixVersions": ["9.92.10", "not-semver"]},
            {"fixVersions": ["9.93.1", "9.92.10"]},
        ]
        assert deployed_versions(cms) == {"9.92.10", "9.93.1"}

    def test_missing_fixversions_key(self) -> None:
        assert deployed_versions([{}]) == set()
