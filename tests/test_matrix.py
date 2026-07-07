from hotfix_booking.matrix import build_version_matrix


def _cm(key, clients, components, versions, deployed_at="2026-01-01"):
    return {
        "key": key,
        "clientEnvironments": clients,
        "components": components,
        "fixVersions": versions,
        "targetDeploymentDate": deployed_at,
    }


class TestBuildVersionMatrix:
    def test_empty(self) -> None:
        result = build_version_matrix([])
        assert result == {"matrix": {}, "components": [], "clients": []}

    def test_single_cm(self) -> None:
        cms = [_cm("CM-1", ["CL001"], ["Alerts"], ["9.92.10"])]
        result = build_version_matrix(cms)
        assert result["matrix"] == {
            "CL001": {"Alerts": {"version": "9.92.10", "cmKey": "CM-1", "deployedAt": "2026-01-01"}}
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
