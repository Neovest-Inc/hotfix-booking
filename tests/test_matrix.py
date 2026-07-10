from hotfix_booking.matrix import build_version_matrix


def _cm(key, clients, components, versions, deployed_at="2026-01-01", status="Done"):
    return {
        "key": key,
        "clientEnvironments": clients,
        "components": components,
        "fixVersions": versions,
        "targetDeploymentDate": deployed_at,
        "status": status,
    }


class TestBuildVersionMatrix:
    def test_empty(self) -> None:
        result = build_version_matrix([])
        assert result == {"matrix": {}, "components": [], "clients": []}

    def test_single_cm(self) -> None:
        cms = [_cm("CM-1", ["CL001"], ["Alerts"], ["9.92.10"])]
        result = build_version_matrix(cms)
        assert result["matrix"] == {
            "CL001": {"Alerts": {
                "version": "9.92.10", "cmKey": "CM-1", "deployedAt": "2026-01-01",
                "inflight": [],
            }}
        }
        assert result["clients"] == ["CL001"]
        assert result["components"] == ["Alerts"]

    def test_keeps_highest_per_client_component(self) -> None:
        cms = [
            _cm("CM-1", ["CL001"], ["Alerts"], ["9.92.5"], "2026-01-01"),
            _cm("CM-2", ["CL001"], ["Alerts"], ["9.92.10"], "2026-02-01"),
            _cm("CM-3", ["CL001"], ["Alerts"], ["9.92.9"], "2026-03-01"),
        ]
        result = build_version_matrix(cms)
        cell = result["matrix"]["CL001"]["Alerts"]
        assert cell["version"] == "9.92.10"
        assert cell["cmKey"] == "CM-2"
        assert cell["deployedAt"] == "2026-02-01"

    def test_filters_non_semver_versions(self) -> None:
        cms = [_cm("CM-1", ["CL001"], ["Alerts"], ["9.92.10", "not-a-version", "1.2"])]
        result = build_version_matrix(cms)
        assert result["matrix"]["CL001"]["Alerts"]["version"] == "9.92.10"

    def test_expands_cartesian_product(self) -> None:
        cms = [_cm("CM-1", ["CL001", "CL002"], ["Alerts", "DV_Web"], ["9.92.10"])]
        result = build_version_matrix(cms)
        assert set(result["matrix"]["CL001"].keys()) == {"Alerts", "DV_Web"}
        assert set(result["matrix"]["CL002"].keys()) == {"Alerts", "DV_Web"}

    def test_components_and_clients_sorted(self) -> None:
        cms = [
            _cm("CM-1", ["CL_Zeta", "CL_Alpha"], ["Zeta_Comp", "Alpha_Comp"], ["1.0.0"]),
        ]
        result = build_version_matrix(cms)
        assert result["clients"] == ["CL_Alpha", "CL_Zeta"]
        assert result["components"] == ["Alpha_Comp", "Zeta_Comp"]

    def test_missing_fields_handled(self) -> None:
        cms = [{"key": "CM-1"}]  # no clients/components/versions
        result = build_version_matrix(cms)
        assert result == {"matrix": {}, "components": [], "clients": []}


# ---------------------------------------------------------------------------
# Status categorisation — the matrix now buckets CMs by status:
#   * DEPLOYED  (feeds the cell's `version`, `cmKey`, `deployedAt`)
#       -> Deployment Completed, Done, Global Review
#   * IN-FLIGHT (feeds the cell's `inflight` list)
#       -> anything that isn't DEPLOYED and isn't terminally cancelled
#   * EXCLUDED  (invisible everywhere)
#       -> Rollback, Rejected, Cancelled   (+ blank / unknown, defensively)
# ---------------------------------------------------------------------------
class TestDeployedStatusRecognition:
    def test_global_review_treated_as_deployed(self) -> None:
        """Global Review is effectively deployed for matrix purposes."""
        cms = [_cm("CM-1", ["CL001"], ["Alerts"], ["9.92.10"], status="Global Review")]
        cell = build_version_matrix(cms)["matrix"]["CL001"]["Alerts"]
        assert cell["version"] == "9.92.10"
        assert cell["cmKey"] == "CM-1"
        # Global Review is deployed → nothing in the in-flight chip.
        assert cell["inflight"] == []

    def test_deployment_completed_treated_as_deployed(self) -> None:
        """Regression guard for the pre-existing behaviour."""
        cms = [_cm("CM-1", ["CL001"], ["Alerts"], ["9.92.10"], status="Deployment Completed")]
        cell = build_version_matrix(cms)["matrix"]["CL001"]["Alerts"]
        assert cell["version"] == "9.92.10"
        assert cell["inflight"] == []

    def test_deployed_status_match_is_case_insensitive(self) -> None:
        cms = [
            _cm("CM-1", ["CL001"], ["Alerts"], ["9.92.10"], status="done"),
            _cm("CM-2", ["CL002"], ["Alerts"], ["9.92.11"], status="GLOBAL REVIEW"),
        ]
        matrix = build_version_matrix(cms)["matrix"]
        assert matrix["CL001"]["Alerts"]["version"] == "9.92.10"
        assert matrix["CL002"]["Alerts"]["version"] == "9.92.11"

    def test_global_review_beats_older_done_for_latest_deployed(self) -> None:
        """Global Review and Done are peers in the deployed bucket; the
        newer semver wins regardless of which specific status labels them."""
        cms = [
            _cm("CM-1", ["CL001"], ["Alerts"], ["9.92.5"], "2026-01-01", status="Done"),
            _cm("CM-2", ["CL001"], ["Alerts"], ["9.92.10"], "2026-02-01", status="Global Review"),
        ]
        cell = build_version_matrix(cms)["matrix"]["CL001"]["Alerts"]
        assert cell["version"] == "9.92.10"
        assert cell["cmKey"] == "CM-2"


class TestInflightBucket:
    def test_in_progress_cm_populates_inflight_not_version(self) -> None:
        """A CM whose status is In Progress must not overwrite the cell's
        deployed version, but must show up in the in-flight list."""
        cms = [
            _cm("CM-1", ["CL001"], ["Alerts"], ["9.92.10"], status="Done"),
            _cm("CM-2", ["CL001"], ["Alerts"], ["9.92.15"], status="In Progress"),
        ]
        cell = build_version_matrix(cms)["matrix"]["CL001"]["Alerts"]
        assert cell["version"] == "9.92.10"
        assert cell["cmKey"] == "CM-1"
        assert cell["inflight"] == [
            {"version": "9.92.15", "status": "In Progress", "cmKey": "CM-2"}
        ]

    def test_inflight_excludes_terminal_cancelled_statuses(self) -> None:
        """Rollback / Rejected / Cancelled must be invisible in both buckets."""
        cms = [
            _cm("CM-1", ["CL001"], ["Alerts"], ["9.92.10"], status="Done"),
            _cm("CM-2", ["CL001"], ["Alerts"], ["9.92.20"], status="Rollback"),
            _cm("CM-3", ["CL001"], ["Alerts"], ["9.92.21"], status="Rejected"),
            _cm("CM-4", ["CL001"], ["Alerts"], ["9.92.22"], status="Cancelled"),
        ]
        cell = build_version_matrix(cms)["matrix"]["CL001"]["Alerts"]
        assert cell["version"] == "9.92.10"
        assert cell["inflight"] == []

    def test_cell_with_only_inflight_still_appears_in_matrix(self) -> None:
        """A client × component that has never seen a Done CM but has an
        In Progress one must still surface in the matrix, with `version=None`
        so the UI can render just the chip."""
        cms = [_cm("CM-1", ["CL001"], ["NewComp"], ["9.98.1"], status="In Progress")]
        result = build_version_matrix(cms)
        cell = result["matrix"]["CL001"]["NewComp"]
        assert cell["version"] is None
        assert cell["cmKey"] is None
        assert cell["deployedAt"] is None
        assert cell["inflight"] == [
            {"version": "9.98.1", "status": "In Progress", "cmKey": "CM-1"}
        ]
        assert result["clients"] == ["CL001"]
        assert result["components"] == ["NewComp"]

    def test_inflight_sorted_by_semver_desc(self) -> None:
        """Newest version first in the popover."""
        cms = [
            _cm("CM-A", ["CL001"], ["Alerts"], ["9.92.5"], status="In Progress"),
            _cm("CM-B", ["CL001"], ["Alerts"], ["9.92.20"], status="Ready for Deployment"),
            _cm("CM-C", ["CL001"], ["Alerts"], ["9.92.12"], status="In Review"),
        ]
        cell = build_version_matrix(cms)["matrix"]["CL001"]["Alerts"]
        versions = [i["version"] for i in cell["inflight"]]
        assert versions == ["9.92.20", "9.92.12", "9.92.5"]

    def test_inflight_skips_non_semver_versions(self) -> None:
        cms = [
            _cm("CM-1", ["CL001"], ["Alerts"], ["not-a-version", "9.92.15"], status="In Progress"),
        ]
        cell = build_version_matrix(cms)["matrix"]["CL001"]["Alerts"]
        assert cell["inflight"] == [
            {"version": "9.92.15", "status": "In Progress", "cmKey": "CM-1"}
        ]

    def test_inflight_status_match_is_case_insensitive(self) -> None:
        """Excluded set must catch mixed-case terminal statuses too."""
        cms = [
            _cm("CM-1", ["CL001"], ["Alerts"], ["9.92.10"], status="Done"),
            _cm("CM-2", ["CL001"], ["Alerts"], ["9.92.20"], status="rollback"),
            _cm("CM-3", ["CL001"], ["Alerts"], ["9.92.15"], status="in progress"),
        ]
        cell = build_version_matrix(cms)["matrix"]["CL001"]["Alerts"]
        assert cell["version"] == "9.92.10"
        assert cell["inflight"] == [
            {"version": "9.92.15", "status": "in progress", "cmKey": "CM-3"}
        ]

    def test_missing_or_empty_status_treated_as_excluded(self) -> None:
        """Defensive: a CM without a status should not leak into inflight.
        Real Jira responses always have a status, but the shape can degrade."""
        cms = [
            _cm("CM-1", ["CL001"], ["Alerts"], ["9.92.10"], status="Done"),
            {"key": "CM-2", "clientEnvironments": ["CL001"], "components": ["Alerts"],
             "fixVersions": ["9.92.15"], "targetDeploymentDate": "2026-02-01"},
            {"key": "CM-3", "clientEnvironments": ["CL001"], "components": ["Alerts"],
             "fixVersions": ["9.92.20"], "targetDeploymentDate": "2026-02-01", "status": ""},
        ]
        cell = build_version_matrix(cms)["matrix"]["CL001"]["Alerts"]
        assert cell["version"] == "9.92.10"
        assert cell["inflight"] == []


class TestExistingDeployedCellsGainInflightField:
    """Snapshot: cells that used to be `{version, cmKey, deployedAt}` now also
    carry an `inflight: []` field so the frontend can iterate uniformly."""

    def test_deployed_only_cell_has_empty_inflight(self) -> None:
        cms = [_cm("CM-1", ["CL001"], ["Alerts"], ["9.92.10"])]
        cell = build_version_matrix(cms)["matrix"]["CL001"]["Alerts"]
        assert "inflight" in cell
        assert cell["inflight"] == []


class TestInFlightOnlyMode:
    """`in_flight_only=True` reclassifies deployed statuses (Done, Deployment
    Completed, Global Review) as excluded — they don't populate cells at all.
    Only CMs currently moving through the workflow appear in the matrix.
    Used by the `/client-versions` route so the UI shows work in progress
    instead of a snapshot of what's already shipped."""

    def test_deployed_cms_produce_no_cells(self) -> None:
        cms = [
            _cm("CM-1", ["CL001"], ["Alerts"], ["9.92.10"], status="Done"),
            _cm("CM-2", ["CL002"], ["DV_Web"], ["9.92.11"], status="Deployment Completed"),
            _cm("CM-3", ["CL003"], ["Analytics"], ["9.92.12"], status="Global Review"),
        ]
        result = build_version_matrix(cms, in_flight_only=True)
        assert result == {"matrix": {}, "components": [], "clients": []}

    def test_in_flight_cm_populates_cell_with_no_deployed_headline(self) -> None:
        """An in-flight CM should appear in the cell's `inflight` list; the
        deployed-headline fields (`version`, `cmKey`, `deployedAt`) must be
        None because nothing is deployed from this bucket."""
        cms = [_cm("CM-1", ["CL001"], ["Alerts"], ["9.92.10"], status="In Progress")]
        cell = build_version_matrix(cms, in_flight_only=True)["matrix"]["CL001"]["Alerts"]
        assert cell["version"] is None
        assert cell["cmKey"] is None
        assert cell["deployedAt"] is None
        assert cell["inflight"] == [
            {"version": "9.92.10", "status": "In Progress", "cmKey": "CM-1"}
        ]

    def test_deployed_cm_does_not_shadow_in_flight_on_same_cell(self) -> None:
        """When a cell has both a deployed CM (which should be excluded) and
        an in-flight CM, only the in-flight one appears."""
        cms = [
            _cm("CM-1", ["CL001"], ["Alerts"], ["9.92.10"], status="Done"),
            _cm("CM-2", ["CL001"], ["Alerts"], ["9.92.15"], status="In Progress"),
        ]
        cell = build_version_matrix(cms, in_flight_only=True)["matrix"]["CL001"]["Alerts"]
        assert cell["version"] is None
        assert cell["inflight"] == [
            {"version": "9.92.15", "status": "In Progress", "cmKey": "CM-2"}
        ]

    def test_terminal_cancelled_still_excluded(self) -> None:
        """Rollback / Rejected / Cancelled are still invisible in in-flight
        mode — they're neither deployed nor in progress."""
        cms = [
            _cm("CM-1", ["CL001"], ["Alerts"], ["9.92.10"], status="Rollback"),
            _cm("CM-2", ["CL001"], ["Alerts"], ["9.92.11"], status="Rejected"),
            _cm("CM-3", ["CL001"], ["Alerts"], ["9.92.12"], status="Cancelled"),
            _cm("CM-4", ["CL001"], ["Alerts"], ["9.92.15"], status="In Progress"),
        ]
        cell = build_version_matrix(cms, in_flight_only=True)["matrix"]["CL001"]["Alerts"]
        assert [i["cmKey"] for i in cell["inflight"]] == ["CM-4"]

    def test_default_mode_unchanged(self) -> None:
        """Regression guard: without `in_flight_only`, deployed CMs still
        become the headline as before."""
        cms = [_cm("CM-1", ["CL001"], ["Alerts"], ["9.92.10"], status="Done")]
        cell = build_version_matrix(cms)["matrix"]["CL001"]["Alerts"]
        assert cell["version"] == "9.92.10"
        assert cell["cmKey"] == "CM-1"
