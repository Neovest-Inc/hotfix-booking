"""Tests for build_environments_compare — the pure engine behind the
Compare tab."""
from __future__ import annotations

from hotfix_booking.compare import build_environments_compare


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
def cm(
    key: str,
    version: str,
    status: str,
    components: list[str],
    envs: list[str],
    deployed_at: str | None = None,
) -> dict:
    """Build a CM dict in the same shape jira_client._issue_to_cm produces."""
    return {
        "key": key,
        "summary": key,
        "status": status,
        "components": components,
        "fixVersions": [version],
        "clientEnvironments": envs,
        "targetDeploymentDate": deployed_at,
        "reporter": "test",
        "createdAt": "2026-01-01T00:00:00Z",
    }


def booking(
    booking_id: str,
    version: str,
    components: list[str],
    envs: list[str],
    status: str = "booked",
    booked_by: str = "PM Person",
) -> dict:
    """Booking record in the store.load_bookings shape."""
    return {
        "id": booking_id,
        "version": version,
        "components": components,
        "clientEnvironments": envs,
        "bookedBy": booked_by,
        "bookedByEmail": "pm@example.com",
        "bookedAt": "2026-07-01T12:00:00Z",
        "status": status,
        "parents": [],
        "originalParents": [],
        "rebaseHistory": [],
    }


ENV_A = "CL001 - Fortress"
ENV_B = "CL002 - Convex"
ENV_OTHER = "CL003 - TPG"


# ---------------------------------------------------------------------------
# Basic shape + empty inputs
# ---------------------------------------------------------------------------
def test_empty_inputs_produce_empty_rows() -> None:
    result = build_environments_compare([], [], ENV_A, ENV_B)
    assert result == {"envA": ENV_A, "envB": ENV_B, "rows": []}


def test_response_carries_supplied_env_labels_verbatim() -> None:
    """The returned envA/envB must be the exact strings the caller passed
    — no lowercasing / trimming — so the UI header can show them as-is."""
    result = build_environments_compare([], [], "  Weird Env  ", "Another")
    assert result["envA"] == "  Weird Env  "
    assert result["envB"] == "Another"


# ---------------------------------------------------------------------------
# Deployed bucket
# ---------------------------------------------------------------------------
def test_single_deployed_cm_populates_env_a_only() -> None:
    cms = [cm("CM-1", "9.99.5", "Done", ["Alerts"], [ENV_A], "2026-01-15")]
    result = build_environments_compare(cms, [], ENV_A, ENV_B)
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["component"] == "Alerts"
    assert row["a"]["deployed"] == {
        "version": "9.99.5",
        "cmKey": "CM-1",
        "deployedAt": "2026-01-15",
        "status": "Done",
    }
    # env_b never appeared → empty cell (all three buckets empty).
    assert row["b"]["deployed"] is None
    assert row["b"]["inflight"] == []
    assert row["b"]["booked"] == []


def test_deployed_slot_keeps_highest_semver_not_first_seen() -> None:
    cms = [
        cm("CM-1", "9.99.3", "Done", ["Alerts"], [ENV_A]),
        cm("CM-2", "9.99.10", "Done", ["Alerts"], [ENV_A]),
        cm("CM-3", "9.99.5", "Deployment Completed", ["Alerts"], [ENV_A]),
    ]
    result = build_environments_compare(cms, [], ENV_A, ENV_B)
    assert result["rows"][0]["a"]["deployed"]["version"] == "9.99.10"
    assert result["rows"][0]["a"]["deployed"]["cmKey"] == "CM-2"


def test_deployed_statuses_all_three_qualify() -> None:
    """`Done`, `Deployment Completed`, `Global Review` all count as deployed."""
    cms = [
        cm("CM-1", "9.99.3", "Done", ["Alerts"], [ENV_A]),
        cm("CM-2", "9.99.5", "Deployment Completed", ["Alerts"], [ENV_B]),
        cm("CM-3", "9.99.7", "Global Review", ["Alerts"], [ENV_A]),
    ]
    result = build_environments_compare(cms, [], ENV_A, ENV_B)
    row = result["rows"][0]
    assert row["a"]["deployed"]["version"] == "9.99.7"
    assert row["b"]["deployed"]["version"] == "9.99.5"


def test_deployed_statuses_are_case_insensitive() -> None:
    cms = [cm("CM-1", "9.99.5", "done", ["Alerts"], [ENV_A])]
    result = build_environments_compare(cms, [], ENV_A, ENV_B)
    assert result["rows"][0]["a"]["deployed"]["version"] == "9.99.5"


def test_deployed_across_both_envs_shows_correct_side_by_side() -> None:
    cms = [
        cm("CM-A", "9.99.5", "Done", ["Alerts"], [ENV_A]),
        cm("CM-B", "9.99.3", "Done", ["Alerts"], [ENV_B]),
    ]
    result = build_environments_compare(cms, [], ENV_A, ENV_B)
    row = result["rows"][0]
    assert row["a"]["deployed"]["version"] == "9.99.5"
    assert row["b"]["deployed"]["version"] == "9.99.3"


def test_single_cm_deployed_to_both_envs_populates_both_sides_identically() -> None:
    cms = [cm("CM-1", "9.99.5", "Done", ["Alerts"], [ENV_A, ENV_B])]
    result = build_environments_compare(cms, [], ENV_A, ENV_B)
    row = result["rows"][0]
    assert row["a"]["deployed"]["version"] == "9.99.5"
    assert row["b"]["deployed"]["version"] == "9.99.5"


# ---------------------------------------------------------------------------
# In-flight bucket
# ---------------------------------------------------------------------------
def test_inflight_cm_populates_inflight_not_deployed() -> None:
    cms = [cm("CM-1", "9.99.7", "In Progress", ["Alerts"], [ENV_A])]
    result = build_environments_compare(cms, [], ENV_A, ENV_B)
    row = result["rows"][0]
    assert row["a"]["deployed"] is None
    assert row["a"]["inflight"] == [
        {"version": "9.99.7", "status": "In Progress", "cmKey": "CM-1"}
    ]


def test_inflight_sorted_descending_by_semver() -> None:
    cms = [
        cm("CM-1", "9.99.10", "QA Approved", ["Alerts"], [ENV_A]),
        cm("CM-2", "9.99.2", "In Progress", ["Alerts"], [ENV_A]),
        cm("CM-3", "9.99.9", "Business Approved", ["Alerts"], [ENV_A]),
    ]
    result = build_environments_compare(cms, [], ENV_A, ENV_B)
    versions = [i["version"] for i in result["rows"][0]["a"]["inflight"]]
    assert versions == ["9.99.10", "9.99.9", "9.99.2"]


def test_inflight_dedupes_by_version_across_cloned_cms() -> None:
    """Two CMs at the same in-flight version → one entry in the popover.
    Matches the matrix.py contract."""
    cms = [
        cm("CM-A", "9.99.7", "In Progress", ["Alerts"], [ENV_A]),
        cm("CM-B", "9.99.7", "QA Approved", ["Alerts"], [ENV_A]),
    ]
    result = build_environments_compare(cms, [], ENV_A, ENV_B)
    inflight = result["rows"][0]["a"]["inflight"]
    assert len(inflight) == 1
    # First seen wins — CM-A came earlier in the list.
    assert inflight[0]["cmKey"] == "CM-A"


def test_deployed_and_inflight_can_coexist_in_same_cell() -> None:
    """The "Deployed + in-flight combined" product decision — a cell can
    show both the shipped version AND newer work moving through."""
    cms = [
        cm("CM-D", "9.99.5", "Done", ["Alerts"], [ENV_A]),
        cm("CM-I", "9.99.7", "In Progress", ["Alerts"], [ENV_A]),
    ]
    result = build_environments_compare(cms, [], ENV_A, ENV_B)
    cell = result["rows"][0]["a"]
    assert cell["deployed"]["version"] == "9.99.5"
    assert [i["version"] for i in cell["inflight"]] == ["9.99.7"]


# ---------------------------------------------------------------------------
# Cancelled / rollback / rejected — invisible in both buckets
# ---------------------------------------------------------------------------
def test_cancelled_cm_excluded_from_all_buckets() -> None:
    cms = [cm("CM-1", "9.99.5", "Cancelled", ["Alerts"], [ENV_A])]
    result = build_environments_compare(cms, [], ENV_A, ENV_B)
    assert result["rows"] == []


def test_rollback_and_rejected_also_excluded() -> None:
    cms = [
        cm("CM-1", "9.99.5", "Rollback", ["Alerts"], [ENV_A]),
        cm("CM-2", "9.99.6", "Rejected", ["Alerts"], [ENV_A]),
    ]
    result = build_environments_compare(cms, [], ENV_A, ENV_B)
    assert result["rows"] == []


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------
def test_booking_shows_in_env_a_cell() -> None:
    bookings = [booking("HB-1", "9.99.9", ["Alerts"], [ENV_A])]
    result = build_environments_compare([], bookings, ENV_A, ENV_B)
    row = result["rows"][0]
    assert row["a"]["booked"] == [
        {
            "version": "9.99.9",
            "bookingId": "HB-1",
            "bookedBy": "PM Person",
            "bookedAt": "2026-07-01T12:00:00Z",
        }
    ]
    assert row["b"]["booked"] == []


def test_cancelled_booking_excluded() -> None:
    bookings = [booking("HB-1", "9.99.9", ["Alerts"], [ENV_A], status="cancelled")]
    result = build_environments_compare([], bookings, ENV_A, ENV_B)
    assert result["rows"] == []


def test_booking_sorted_descending_by_semver() -> None:
    bookings = [
        booking("HB-1", "9.99.5", ["Alerts"], [ENV_A]),
        booking("HB-2", "9.99.11", ["Alerts"], [ENV_A]),
        booking("HB-3", "9.99.9", ["Alerts"], [ENV_A]),
    ]
    result = build_environments_compare([], bookings, ENV_A, ENV_B)
    versions = [b["version"] for b in result["rows"][0]["a"]["booked"]]
    assert versions == ["9.99.11", "9.99.9", "9.99.5"]


def test_booking_targeting_other_env_ignored() -> None:
    bookings = [booking("HB-1", "9.99.5", ["Alerts"], [ENV_OTHER])]
    result = build_environments_compare([], bookings, ENV_A, ENV_B)
    assert result["rows"] == []


# ---------------------------------------------------------------------------
# Filtering by env / component / version
# ---------------------------------------------------------------------------
def test_cm_in_neither_env_ignored() -> None:
    cms = [cm("CM-1", "9.99.5", "Done", ["Alerts"], [ENV_OTHER])]
    result = build_environments_compare(cms, [], ENV_A, ENV_B)
    assert result["rows"] == []


def test_cm_with_no_semver_version_ignored() -> None:
    cms = [
        {
            **cm("CM-1", "not-a-version", "Done", ["Alerts"], [ENV_A]),
            "fixVersions": ["not-a-version"],
        }
    ]
    result = build_environments_compare(cms, [], ENV_A, ENV_B)
    assert result["rows"] == []


def test_cm_with_no_components_ignored() -> None:
    cms = [cm("CM-1", "9.99.5", "Done", [], [ENV_A])]
    result = build_environments_compare(cms, [], ENV_A, ENV_B)
    assert result["rows"] == []


def test_cm_with_no_envs_ignored() -> None:
    cms = [cm("CM-1", "9.99.5", "Done", ["Alerts"], [])]
    result = build_environments_compare(cms, [], ENV_A, ENV_B)
    assert result["rows"] == []


def test_cm_with_missing_status_ignored() -> None:
    cms = [{**cm("CM-1", "9.99.5", "", ["Alerts"], [ENV_A]), "status": None}]
    result = build_environments_compare(cms, [], ENV_A, ENV_B)
    assert result["rows"] == []


# ---------------------------------------------------------------------------
# Multi-component CMs
# ---------------------------------------------------------------------------
def test_multi_component_cm_produces_row_per_component() -> None:
    """A CM affecting two components should populate both rows."""
    cms = [cm("CM-1", "9.99.5", "Done", ["Alerts", "DV_Web"], [ENV_A])]
    result = build_environments_compare(cms, [], ENV_A, ENV_B)
    components = [r["component"] for r in result["rows"]]
    assert components == ["Alerts", "DV_Web"]
    for row in result["rows"]:
        assert row["a"]["deployed"]["version"] == "9.99.5"


# ---------------------------------------------------------------------------
# Row inclusion & ordering
# ---------------------------------------------------------------------------
def test_rows_sorted_alphabetically_by_component_case_insensitive() -> None:
    cms = [
        cm("CM-1", "9.99.5", "Done", ["Zoo"], [ENV_A]),
        cm("CM-2", "9.99.5", "Done", ["alerts"], [ENV_A]),
        cm("CM-3", "9.99.5", "Done", ["Middle"], [ENV_A]),
    ]
    result = build_environments_compare(cms, [], ENV_A, ENV_B)
    assert [r["component"] for r in result["rows"]] == ["alerts", "Middle", "Zoo"]


def test_row_present_when_only_env_b_has_data() -> None:
    """One-sided coverage still yields a row — the UI needs it to render the
    "missing on the other side" state."""
    cms = [cm("CM-1", "9.99.5", "Done", ["Alerts"], [ENV_B])]
    result = build_environments_compare(cms, [], ENV_A, ENV_B)
    assert len(result["rows"]) == 1
    assert result["rows"][0]["a"]["deployed"] is None
    assert result["rows"][0]["b"]["deployed"]["version"] == "9.99.5"


def test_row_present_when_only_bookings_touch_component() -> None:
    """No Jira CMs, only a local booking → row still appears so the UI can
    show \"nothing shipped yet, but a booking is queued\"."""
    bookings = [booking("HB-1", "9.99.5", ["Alerts"], [ENV_A])]
    result = build_environments_compare([], bookings, ENV_A, ENV_B)
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["a"]["booked"][0]["version"] == "9.99.5"
    assert row["a"]["deployed"] is None
    assert row["a"]["inflight"] == []


# ---------------------------------------------------------------------------
# env_a == env_b — self-compare should just show identical sides
# ---------------------------------------------------------------------------
def test_self_compare_is_symmetric() -> None:
    cms = [cm("CM-1", "9.99.5", "Done", ["Alerts"], [ENV_A])]
    result = build_environments_compare(cms, [], ENV_A, ENV_A)
    row = result["rows"][0]
    assert row["a"]["deployed"]["version"] == "9.99.5"
    assert row["b"]["deployed"]["version"] == "9.99.5"


# ---------------------------------------------------------------------------
# Realistic mixed scenario
# ---------------------------------------------------------------------------
def test_mixed_realistic_scenario() -> None:
    """One row where A is ahead, one where B is ahead, one where they match,
    one where B has only in-flight work, and one where A has a booking."""
    cms = [
        # Alerts: A@9.99.10 deployed, B@9.99.7 deployed → A ahead
        cm("CM-1", "9.99.10", "Done", ["Alerts"], [ENV_A]),
        cm("CM-2", "9.99.7", "Deployment Completed", ["Alerts"], [ENV_B]),
        # DV_Web: A@9.99.3 deployed, B@9.99.9 deployed → B ahead
        cm("CM-3", "9.99.3", "Done", ["DV_Web"], [ENV_A]),
        cm("CM-4", "9.99.9", "Done", ["DV_Web"], [ENV_B]),
        # Analytics: both @9.99.5 → equal
        cm("CM-5", "9.99.5", "Done", ["Analytics"], [ENV_A, ENV_B]),
        # Payments: only B has an in-flight CM
        cm("CM-6", "9.99.4", "In Progress", ["Payments"], [ENV_B]),
    ]
    bookings = [
        # Reports: only A has a booking (no deploys yet on either side)
        booking("HB-1", "9.99.2", ["Reports"], [ENV_A]),
    ]
    result = build_environments_compare(cms, bookings, ENV_A, ENV_B)
    rows = {r["component"]: r for r in result["rows"]}
    # 5 components touched: Alerts, DV_Web, Analytics, Payments, Reports.
    assert set(rows) == {"Alerts", "DV_Web", "Analytics", "Payments", "Reports"}

    assert rows["Alerts"]["a"]["deployed"]["version"] == "9.99.10"
    assert rows["Alerts"]["b"]["deployed"]["version"] == "9.99.7"

    assert rows["DV_Web"]["a"]["deployed"]["version"] == "9.99.3"
    assert rows["DV_Web"]["b"]["deployed"]["version"] == "9.99.9"

    assert rows["Analytics"]["a"]["deployed"]["version"] == "9.99.5"
    assert rows["Analytics"]["b"]["deployed"]["version"] == "9.99.5"

    assert rows["Payments"]["a"]["deployed"] is None
    assert rows["Payments"]["a"]["inflight"] == []
    assert rows["Payments"]["b"]["deployed"] is None
    assert rows["Payments"]["b"]["inflight"][0]["version"] == "9.99.4"

    assert rows["Reports"]["a"]["deployed"] is None
    assert rows["Reports"]["a"]["booked"][0]["version"] == "9.99.2"
    assert rows["Reports"]["b"]["deployed"] is None
    assert rows["Reports"]["b"]["booked"] == []
