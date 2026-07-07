import pytest

from hotfix_booking.versioning import Version, compare_versions, is_semver, parse_version


class TestParseVersion:
    @pytest.mark.parametrize(
        "s, expected",
        [
            ("9.92.76", Version(9, 92, 76)),
            ("0.0.0", Version(0, 0, 0)),
            ("10.100.1000", Version(10, 100, 1000)),
            ("9.92", Version(9, 92, 0)),        # missing patch
            ("9", Version(9, 0, 0)),            # missing minor + patch
            ("", Version(0, 0, 0)),             # empty
            ("9.92.abc", Version(9, 92, 0)),    # NaN patch → 0
            ("a.b.c", Version(0, 0, 0)),        # all NaN
            ("9..3", Version(9, 0, 3)),         # empty middle → 0
        ],
    )
    def test_parses(self, s: str, expected: Version) -> None:
        assert parse_version(s) == expected


class TestCompareVersions:
    def test_equal(self) -> None:
        assert compare_versions("9.92.76", "9.92.76") == 0

    @pytest.mark.parametrize(
        "a, b",
        [
            ("9.92.77", "9.92.76"),
            ("9.93.0", "9.92.99"),
            ("10.0.0", "9.99.99"),
            ("9.92.10", "9.92.9"),   # numeric compare, not lexical
        ],
    )
    def test_a_greater(self, a: str, b: str) -> None:
        assert compare_versions(a, b) > 0
        assert compare_versions(b, a) < 0

    def test_missing_parts_treated_as_zero(self) -> None:
        assert compare_versions("9.92", "9.92.0") == 0
        assert compare_versions("9.92.1", "9.92") > 0

    def test_used_as_sort_key(self) -> None:
        # Same use case as Node: allVersions.sort(compareVersions)
        from functools import cmp_to_key
        versions = ["9.92.10", "9.92.2", "10.0.0", "9.93.1", "9.92.9"]
        expected = ["9.92.2", "9.92.9", "9.92.10", "9.93.1", "10.0.0"]
        assert sorted(versions, key=cmp_to_key(compare_versions)) == expected


class TestIsSemver:
    @pytest.mark.parametrize("s", ["1.2.3", "9.92.76", "0.0.0", "10.100.1000"])
    def test_valid(self, s: str) -> None:
        assert is_semver(s) is True

    @pytest.mark.parametrize(
        "s", ["", "1.2", "1.2.3.4", "1.2.a", "v1.2.3", "1.2.3-alpha", " 1.2.3", "1.2.3 "]
    )
    def test_invalid(self, s: str) -> None:
        assert is_semver(s) is False
