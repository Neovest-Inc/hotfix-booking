"""Pure dependency-graph math (no HTTP, no store, no Jira).

Covers `overlaps`, `compute_parents`, `direct_children`, and the store's
`_normalize_and_backfill` migration for legacy records that predate the
`parents` field.
"""
from __future__ import annotations

import pytest

from hotfix_booking.dependencies import (
    cm_to_pseudo_booking,
    compute_parents,
    direct_children,
    overlaps,
    parent_versions,
)
from hotfix_booking.store import _normalize_and_backfill, create_booking


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _mk(
    *,
    id: str,
    version: str,
    components: list[str],
    clients: list[str],
    at: str,
    status: str = "booked",
    parents: list[str] | None = None,
) -> dict:
    return {
        "id": id,
        "version": version,
        "components": components,
        "clientEnvironments": clients,
        "bookedAt": at,
        "status": status,
        "parents": parents if parents is not None else [],
    }


# ---------------------------------------------------------------------------
# D1–D4 — overlap semantics
# ---------------------------------------------------------------------------
class TestOverlaps:
    def test_D1_totally_disjoint_no_overlap(self) -> None:
        a = _mk(id="A", version="9.97.1", components=["Comp1"], clients=["C1"], at="t1")
        b = _mk(id="B", version="9.97.2", components=["Comp2"], clients=["C2"], at="t2")
        assert overlaps(a, b) is False

    def test_D2_shared_client_disjoint_components_no_overlap(self) -> None:
        a = _mk(id="A", version="9.97.1", components=["Comp1"], clients=["C1"], at="t1")
        b = _mk(id="B", version="9.97.2", components=["Comp2"], clients=["C1"], at="t2")
        assert overlaps(a, b) is False

    def test_D3_shared_component_disjoint_clients_no_overlap(self) -> None:
        a = _mk(id="A", version="9.97.1", components=["Comp1"], clients=["C1"], at="t1")
        b = _mk(id="B", version="9.97.2", components=["Comp1"], clients=["C2"], at="t2")
        assert overlaps(a, b) is False

    def test_D4_any_single_shared_cell_overlaps(self) -> None:
        a = _mk(id="A", version="9.97.1",
                components=["Comp1", "Comp2"], clients=["C1", "C2"], at="t1")
        b = _mk(id="B", version="9.97.2",
                components=["Comp2"], clients=["C2"], at="t2")
        assert overlaps(a, b) is True

    def test_D4_reflexive(self) -> None:
        a = _mk(id="A", version="9.97.1", components=["C"], clients=["C1"], at="t1")
        assert overlaps(a, a) is True

    def test_D15_empty_lists_never_overlap_and_do_not_raise(self) -> None:
        a = _mk(id="A", version="9.97.1", components=[], clients=["C1"], at="t1")
        b = _mk(id="B", version="9.97.2", components=["Comp1"], clients=["C1"], at="t2")
        assert overlaps(a, b) is False
        assert overlaps({}, {}) is False
        assert overlaps(a, {}) is False


# ---------------------------------------------------------------------------
# D5–D9, D12–D14 — compute_parents
# ---------------------------------------------------------------------------
class TestComputeParents:
    def test_D5_empty_store_returns_empty(self) -> None:
        new = _mk(id="N", version="9.97.1", components=["C"], clients=["C1"], at="t1")
        assert compute_parents(new, []) == []

    def test_D6_only_non_overlapping_priors_returns_empty(self) -> None:
        prior = _mk(id="A", version="9.97.1", components=["X"], clients=["C1"], at="t1")
        new = _mk(id="N", version="9.97.2", components=["Y"], clients=["C1"], at="t2")
        assert compute_parents(new, [prior]) == []

    def test_D7_chain_returns_only_most_recent(self) -> None:
        # 9.97.1 → 9.97.2 → new 9.97.3 all touch (C1, REST).
        p1 = _mk(id="A", version="9.97.1", components=["REST"], clients=["C1"], at="2026-07-01T10:00:00+00:00")
        p2 = _mk(id="B", version="9.97.2", components=["REST"], clients=["C1"], at="2026-07-02T10:00:00+00:00")
        new = _mk(id="N", version="9.97.3", components=["REST"], clients=["C1"], at="2026-07-03T10:00:00+00:00")
        assert compute_parents(new, [p1, p2]) == ["B"]

    def test_D8_multiple_parents_when_priors_cover_different_cells(self) -> None:
        # 9.97.1 covers (C3, REST); 9.97.2 covers (C3, TM). New covers both cells.
        p1 = _mk(id="A", version="9.97.1", components=["REST"], clients=["C3"], at="2026-07-01T10:00:00+00:00")
        p2 = _mk(id="B", version="9.97.2", components=["TM"], clients=["C3"], at="2026-07-02T10:00:00+00:00")
        new = _mk(id="N", version="9.97.3", components=["REST", "TM"], clients=["C3"], at="2026-07-03T10:00:00+00:00")
        result = compute_parents(new, [p1, p2])
        assert set(result) == {"A", "B"}
        # Most-recent-first ordering
        assert result == ["B", "A"]

    def test_D9_skips_cancelled_priors(self) -> None:
        p1 = _mk(id="A", version="9.97.1", components=["REST"], clients=["C1"],
                 at="2026-07-01T10:00:00+00:00")
        p2 = _mk(id="B", version="9.97.2", components=["REST"], clients=["C1"],
                 at="2026-07-02T10:00:00+00:00", status="cancelled")
        new = _mk(id="N", version="9.97.3", components=["REST"], clients=["C1"],
                  at="2026-07-03T10:00:00+00:00")
        # Cancelled B is skipped; A becomes the parent.
        assert compute_parents(new, [p1, p2]) == ["A"]

    def test_D12_stable_ordering_regardless_of_input_order(self) -> None:
        p1 = _mk(id="A", version="9.97.1", components=["REST"], clients=["C3"],
                 at="2026-07-01T10:00:00+00:00")
        p2 = _mk(id="B", version="9.97.2", components=["TM"], clients=["C3"],
                 at="2026-07-02T10:00:00+00:00")
        new = _mk(id="N", version="9.97.3", components=["TM", "REST"], clients=["C3"],
                  at="2026-07-03T10:00:00+00:00")
        # Different iteration orders of clients/components must yield the same
        # parent list (most-recent-first, deduped).
        assert compute_parents(new, [p1, p2]) == ["B", "A"]
        assert compute_parents(new, [p2, p1]) == ["B", "A"]

    def test_D13_only_the_most_recent_link_of_a_deep_chain(self) -> None:
        priors = [
            _mk(id="v1", version="9.97.1", components=["REST"], clients=["C1"], at="2026-07-01T10:00:00+00:00"),
            _mk(id="v2", version="9.97.2", components=["REST"], clients=["C1"], at="2026-07-02T10:00:00+00:00"),
            _mk(id="v3", version="9.97.3", components=["REST"], clients=["C1"], at="2026-07-03T10:00:00+00:00"),
        ]
        new = _mk(id="N", version="9.97.4", components=["REST"], clients=["C1"],
                  at="2026-07-04T10:00:00+00:00")
        assert compute_parents(new, priors) == ["v3"]

    def test_D14_same_timestamp_tiebreak_by_higher_patch(self) -> None:
        same_at = "2026-07-02T10:00:00+00:00"
        priors = [
            _mk(id="lo", version="9.97.1", components=["REST"], clients=["C1"], at=same_at),
            _mk(id="hi", version="9.97.5", components=["REST"], clients=["C1"], at=same_at),
        ]
        new = _mk(id="N", version="9.97.6", components=["REST"], clients=["C1"],
                  at="2026-07-03T10:00:00+00:00")
        # Same bookedAt → higher patch (9.97.5) wins.
        assert compute_parents(new, priors) == ["hi"]

    def test_ignores_priors_on_a_different_release_line(self) -> None:
        # A booking on 9.98.x must not become a parent of a 9.97.x booking.
        p_other_line = _mk(id="X", version="9.98.1", components=["REST"], clients=["C1"],
                           at="2026-07-01T10:00:00+00:00")
        new = _mk(id="N", version="9.97.2", components=["REST"], clients=["C1"],
                  at="2026-07-03T10:00:00+00:00")
        assert compute_parents(new, [p_other_line]) == []

    def test_new_booking_with_empty_cell_grid_returns_empty(self) -> None:
        # No components → no cells → no parents.
        prior = _mk(id="A", version="9.97.1", components=["REST"], clients=["C1"], at="t1")
        new_no_components = _mk(id="N", version="9.97.2", components=[], clients=["C1"], at="t2")
        assert compute_parents(new_no_components, [prior]) == []

    def test_ignores_own_id_defensively(self) -> None:
        # Should never happen in practice, but safe: don't self-reference.
        self_ = _mk(id="SELF", version="9.97.2", components=["REST"], clients=["C1"],
                    at="2026-07-02T10:00:00+00:00")
        # `self_` in the priors list simulates a bad caller passing pre-inserted.
        assert compute_parents(self_, [self_]) == []


# ---------------------------------------------------------------------------
# D11 — direct_children
# ---------------------------------------------------------------------------
class TestDirectChildren:
    def test_D11_uses_current_parents_not_original_parents(self) -> None:
        # `child` has originalParents = ["OLD"] but current parents = ["TARGET"]
        # after a prior rebase. direct_children("TARGET") must find it.
        child = {
            "id": "CHILD",
            "version": "9.97.3",
            "components": ["REST"],
            "clientEnvironments": ["C1"],
            "bookedAt": "t3",
            "status": "booked",
            "parents": ["TARGET"],
            "originalParents": ["OLD"],
        }
        assert direct_children("TARGET", [child])[0]["id"] == "CHILD"
        assert direct_children("OLD", [child]) == []

    def test_skips_cancelled_children(self) -> None:
        child = _mk(id="C", version="9.97.2", components=["REST"], clients=["C1"],
                    at="t2", status="cancelled", parents=["P"])
        assert direct_children("P", [child]) == []

    def test_ignores_self(self) -> None:
        b = _mk(id="X", version="9.97.1", components=["REST"], clients=["C1"], at="t1",
                parents=["X"])
        assert direct_children("X", [b]) == []

    def test_returns_deterministic_order_by_patch(self) -> None:
        c1 = _mk(id="C1", version="9.97.5", components=["REST"], clients=["C1"], at="t5",
                 parents=["P"])
        c2 = _mk(id="C2", version="9.97.2", components=["REST"], clients=["C1"], at="t2",
                 parents=["P"])
        c3 = _mk(id="C3", version="9.97.9", components=["REST"], clients=["C1"], at="t9",
                 parents=["P"])
        assert [b["id"] for b in direct_children("P", [c1, c2, c3])] == ["C2", "C1", "C3"]


# ---------------------------------------------------------------------------
# parent_versions helper
# ---------------------------------------------------------------------------
class TestParentVersions:
    def test_maps_ids_to_versions_and_drops_unknown(self) -> None:
        bookings = [
            {"id": "A", "version": "9.97.1"},
            {"id": "B", "version": "9.97.2"},
        ]
        assert parent_versions(["A", "B", "C"], bookings) == ["9.97.1", "9.97.2"]

    def test_empty_input_returns_empty(self) -> None:
        assert parent_versions([], [{"id": "A", "version": "9.97.1"}]) == []


# ---------------------------------------------------------------------------
# D10 — backfill migration
# ---------------------------------------------------------------------------
class TestBackfillMigration:
    def test_D10_backfill_matches_create_one_at_a_time(self) -> None:
        """A legacy JSON file (records lack `parents`) after backfill must
        yield the same DAG as if the bookings had been created one at a time
        through `create_booking`.
        """
        # Version A: (C1, REST); B: (C1, REST) → depends on A; C: (C1, TM) → no overlap.
        legacy = [
            {"id": "A", "version": "9.97.1", "components": ["REST"],
             "clientEnvironments": ["C1"], "bookedAt": "2026-07-01T10:00:00+00:00",
             "bookedBy": "u1"},
            {"id": "B", "version": "9.97.2", "components": ["REST"],
             "clientEnvironments": ["C1"], "bookedAt": "2026-07-02T10:00:00+00:00",
             "bookedBy": "u2"},
            {"id": "C", "version": "9.97.3", "components": ["TM"],
             "clientEnvironments": ["C1"], "bookedAt": "2026-07-03T10:00:00+00:00",
             "bookedBy": "u3"},
        ]
        _normalize_and_backfill(legacy)
        parents_after = {b["id"]: b["parents"] for b in legacy}
        assert parents_after == {"A": [], "B": ["A"], "C": []}
        # Backfill also sets originalParents to match parents for legacy records,
        # and rebaseHistory to an empty list.
        for b in legacy:
            assert b["originalParents"] == b["parents"]
            assert b["rebaseHistory"] == []
            assert b["status"] == "booked"

        # Cross-check against building the same DAG via create_booking.
        data: dict = {"bookings": []}
        ids = iter(["A", "B", "C"])
        times = iter([
            "2026-07-01T10:00:00+00:00",
            "2026-07-02T10:00:00+00:00",
            "2026-07-03T10:00:00+00:00",
        ])
        for version, comps, clients in [
            ("9.97.1", ["REST"], ["C1"]),
            ("9.97.2", ["REST"], ["C1"]),
            ("9.97.3", ["TM"], ["C1"]),
        ]:
            create_booking(
                data,
                version=version,
                components=comps,
                client_environments=clients,
                booked_by="u",
                id_factory=lambda i=next(ids): i,
                now=lambda t=next(times): t,
            )
        created_parents = {b["id"]: b["parents"] for b in data["bookings"]}
        assert created_parents == parents_after

    def test_backfill_is_idempotent(self) -> None:
        recs = [
            {"id": "A", "version": "9.97.1", "components": ["REST"],
             "clientEnvironments": ["C1"], "bookedAt": "2026-07-01T10:00:00+00:00",
             "bookedBy": "u"},
        ]
        _normalize_and_backfill(recs)
        first = [dict(r) for r in recs]
        _normalize_and_backfill(recs)
        assert recs == first

class TestCmToPseudoBooking:
    """`cm_to_pseudo_booking` converts a Jira CM dict into a booking-shaped
    dict so `compute_parents` can consider it as a candidate parent alongside
    real local bookings. The pseudo-booking's id uses the `jira:<KEY>` prefix
    to keep it distinguishable from real HB-<epoch> ids.
    """

    def _cm(
        self,
        *,
        key: str = "CM-1234",
        fix_versions: list[str] | None = None,
        components: list[str] | None = None,
        clients: list[str] | None = None,
        status: str = "Done",
        created_at: str | None = "2026-06-01T09:00:00.000+0000",
    ) -> dict:
        return {
            "key": key,
            "status": status,
            "components": components if components is not None else ["REST"],
            "clientEnvironments": clients if clients is not None else ["C1"],
            "fixVersions": fix_versions if fix_versions is not None else ["9.98.12"],
            "createdAt": created_at,
        }

    def test_returns_pseudo_booking_with_jira_prefixed_id(self) -> None:
        cm = self._cm(key="CM-1234", fix_versions=["9.98.12"])
        pseudo = cm_to_pseudo_booking(cm, major=9, minor=98)
        assert pseudo is not None
        assert pseudo["id"] == "jira:CM-1234"
        assert pseudo["version"] == "9.98.12"
        assert pseudo["components"] == ["REST"]
        assert pseudo["clientEnvironments"] == ["C1"]
        assert pseudo["status"] == "booked"
        assert pseudo["bookedAt"] == "2026-06-01T09:00:00.000+0000"

    def test_picks_matching_fix_version_when_multiple(self) -> None:
        # A CM listing both a same-line version and a stray other-line one
        # must pick the version on the requested (major, minor).
        cm = self._cm(fix_versions=["not-semver", "9.98.5", "9.99.1"])
        pseudo = cm_to_pseudo_booking(cm, major=9, minor=98)
        assert pseudo is not None
        assert pseudo["version"] == "9.98.5"

    def test_returns_none_when_no_matching_fix_version(self) -> None:
        cm = self._cm(fix_versions=["9.99.1"])
        assert cm_to_pseudo_booking(cm, major=9, minor=98) is None

    def test_returns_none_when_no_fix_versions_at_all(self) -> None:
        cm = self._cm(fix_versions=[])
        assert cm_to_pseudo_booking(cm, major=9, minor=98) is None

    def test_returns_none_when_no_components_or_clients(self) -> None:
        assert cm_to_pseudo_booking(self._cm(components=[]), major=9, minor=98) is None
        assert cm_to_pseudo_booking(self._cm(clients=[]), major=9, minor=98) is None

    @pytest.mark.parametrize(
        "status",
        ["Rollback", "rollback", "Rejected", "rejected", "Cancelled", "cancelled"],
    )
    def test_excludes_cancelled_and_rejected_cms(self, status: str) -> None:
        # Terminal-negative CM states are not eligible parents — nothing is
        # actually going to ship for these tickets.
        cm = self._cm(status=status)
        assert cm_to_pseudo_booking(cm, major=9, minor=98) is None

    @pytest.mark.parametrize(
        "status",
        # In-flight (someone is actively working to deploy) + shipped-successful
        # states. All of these count as valid parents.
        ["Open", "DL Approved", "Today's Deployments", "In Progress",
         "Business Approved", "QA Approved",
         "Done", "Deployment Completed", "Global Review"],
    )
    def test_includes_open_and_in_flight_cms(self, status: str) -> None:
        # An Open / Approved / In Progress CM represents live intent to ship —
        # subsequent bookings should stack on top of it. Deployed CMs
        # (Done / Deployment Completed / Global Review) are the canonical
        # shipped-hotfix parents.
        cm = self._cm(status=status)
        assert cm_to_pseudo_booking(cm, major=9, minor=98) is not None

    def test_used_by_compute_parents_alongside_real_bookings(self) -> None:
        # Regression-guard for the user's real-world bug: a CM created outside
        # the app (Teams-chat legacy path) at 9.98.12 must be picked up as the
        # parent of a new 9.98.602 booking that touches the same (client,
        # component) cell.
        pseudo = cm_to_pseudo_booking(
            self._cm(key="CM-999", fix_versions=["9.98.12"],
                     components=["REST"], clients=["C1"],
                     created_at="2026-06-01T09:00:00+00:00"),
            major=9, minor=98,
        )
        new = _mk(id="N", version="9.98.602",
                 components=["REST"], clients=["C1"],
                 at="2026-07-15T09:00:00+00:00")
        assert compute_parents(new, [pseudo]) == ["jira:CM-999"]

    def test_parent_versions_resolves_jira_pseudo_id(self) -> None:
        pseudo = cm_to_pseudo_booking(
            self._cm(key="CM-42", fix_versions=["9.98.12"]),
            major=9, minor=98,
        )
        # parent_versions has to see the pseudo-booking in `all_bookings` to
        # be able to map jira:CM-42 → 9.98.12. The routes layer will merge
        # the local + pseudo lists before calling parent_versions.
        assert parent_versions(["jira:CM-42"], [pseudo]) == ["9.98.12"]


class TestBackfillMigrationExtras:
    """Split off from `TestBackfillMigration` so newer cases can be added
    without disturbing the D10 chronological-replay test above.
    """

    def test_backfill_preserves_already_populated_parents(self) -> None:
        # A record that already has `parents` (e.g. from a prior rebase) must
        # NOT be recomputed — that would silently revert the rebase.
        recs = [
            {"id": "A", "version": "9.97.1", "components": ["REST"],
             "clientEnvironments": ["C1"], "bookedAt": "2026-07-01T10:00:00+00:00",
             "bookedBy": "u"},
            {"id": "B", "version": "9.97.2", "components": ["REST"],
             "clientEnvironments": ["C1"], "bookedAt": "2026-07-02T10:00:00+00:00",
             "bookedBy": "u",
             "parents": [],  # already computed — pretend a prior cancel rebased it.
             "originalParents": ["A"],
             "rebaseHistory": [{"cancelledBookingId": "A"}]},
        ]
        _normalize_and_backfill(recs)
        b = next(r for r in recs if r["id"] == "B")
        assert b["parents"] == []  # unchanged — NOT recomputed to ["A"]
        assert b["originalParents"] == ["A"]
        assert len(b["rebaseHistory"]) == 1
