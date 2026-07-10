"""Seed the local bookings file with a rich demo dataset for UI testing.

USAGE
-----

Load the seed (backs up any existing file to `<bookings-file>.pre-seed.bak`):

    python tools/seed.py load                      # attributes every booking to synthetic emails
    python tools/seed.py load --email me@nv.com    # ALSO attributes 3 bookings to you (so the Cancel button shows on your own rows)

When ``--email`` is provided the script calls Jira's user-search API to
resolve the address to a real ``displayName`` (using the same credentials the
app uses). If the email doesn't correspond to an active Jira account the
script aborts with a non-zero exit code and prints the reason to stderr —
no file is written.

Restore the pre-seed data (or clear if there was no backup):

    python tools/seed.py clear

The seed writes to release line `9.98.x` starting at patch `500` so it sits
above realistic Jira traffic without colliding. Every UI state the Cancel
feature produces has at least one row demonstrating it — see the SCENARIOS
table below.

SCENARIOS
---------

    #  Versions        Story                                       UI state exercised
   ─── ─────────────── ─────────────────────────────────────────── ───────────────────────────────
    A  500-501-502     Active chain of 3                            Basis line: `Based on 9.98.501`
    B  505-506-507     Middle cancelled (Bob cancels 506)           507 shows Rebased chip; popover
                                                                    reads "was based on 506 → now
                                                                    based on 505 because Bob
                                                                    cancelled 506"
    C  510-511-512     Fan-out — parent 510 cancelled;              Both 511 AND 512 Rebased chips;
                       511 & 512 rebase to baseline                 basis lines both say baseline
    D  515-516-517     DAG multi-parent (no cancels)                517 basis: "Based on 9.98.516,
                                                                    9.98.515"
    E  520-521-522     DAG multi-parent + partial cancel            522 basis reduces from
                                                                    [521,520] → [521]
    F  525-526-527     Repeat rebase — 527 rebased TWICE            527 Rebased chip; popover
                       (both 526 and 525 cancelled in order)        shows 2 chronological events
    G  530             Standalone cancelled tombstone               Cancelled chip + strikethrough
    H  531             Rebased-to-baseline singleton (was 530)      Rebased chip; basis = baseline
    I  535             Many components + many clients               Tests the "+N more" expand
    J  540-541         Your own active chain (--email only)         Cancel button visible;
                                                                    cancelling 540 rebases 541
                                                                    live in the UI

MANUAL ACTIONS to test what the seed alone can't
------------------------------------------------

1. **Active-CM anomaly warning (amber chip).** The seed can't fake a Jira CM,
   so this requires the real thing:
     a) Find a version that exists in Jira with a non-terminal status (e.g.
        "In Progress" or "Done") — check any recent CM ticket in the CM
        project for a fixVersion in the release line you're viewing.
     b) Book that exact version through the app (or edit
        `data/hotfix-bookings.json` manually).
     c) Cancel it via the UI.
     d) The cancel-result modal shows an amber "Active CM in Jira" strip
        with the CM key and its live status. That same amber chip persists
        on the row in My Hotfixes / Hotfix History thereafter.

2. **Race / concurrency.** Open two browser tabs, hit Cancel on the same
   booking in both within a second. The second one gets a `409 already
   cancelled` toast. Store lock keeps everything consistent.

3. **Admin override.** Set `ADMIN_EMAILS=you@neovest.com` in `.env`, restart
   uvicorn, then try cancelling a booking whose email is NOT yours (e.g.
   9.98.505 which is Alice's). You'll succeed. Without the admin allow-list
   the UI hides the button and the server returns 403.

4. **Legacy backfill.** Manually strip the `parents`, `originalParents`,
   `rebaseHistory` fields from a record in `data/hotfix-bookings.json`,
   restart the app, then reload the UI. The next mutating call
   (`/next-version` triggers a re-save via cleanup, or booking / cancelling
   anything) will re-populate them from the DAG computation.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_BOOKINGS_FILE = Path(__file__).resolve().parent.parent / "data" / "hotfix-bookings.json"
BACKUP_SUFFIX = ".pre-seed.bak"

# Synthetic identities — non-real emails so nothing gets sent by accident and
# so Jira user-search legitimately rejects them (which is what would happen if
# a random attacker tried to cancel someone else's booking).
ALICE = ("Alice Seed", "alice.seed@example.com")
BOB = ("Bob Seed", "bob.seed@example.com")
CAROL = ("Carol Seed", "carol.seed@example.com")
DAVID = ("David Seed", "david.seed@example.com")
EVE = ("Eve Seed", "eve.seed@example.com")
FRANK = ("Frank Seed", "frank.seed@example.com")

# Fixed UTC hours we anchor events to — chosen so the ET display reads as a
# round number (14:00 UTC = 10:00 AM ET in EDT / 9:00 AM ET in EST;
# 19:00 UTC = 3:00 PM ET in EDT). Two hours is enough to space same-day events
# apart without the reader having to squint at minutes.
MORNING_UTC = 14
AFTERNOON_UTC = 19


def _at(days_ago: int, hour_utc: int = MORNING_UTC) -> str:
    """ISO timestamp at `hour_utc:00 UTC` on the day `days_ago` days before today.

    Deterministic time-of-day makes the Rebased-chip popover read as e.g.
    "Jul 5, 10:00 AM ET" instead of whatever irregular minute the wall clock
    happens to be on when the seed is loaded.
    """
    today_utc = datetime.now(timezone.utc).date()
    target_date = today_utc - timedelta(days=days_ago)
    dt = datetime(
        target_date.year, target_date.month, target_date.day,
        hour_utc, 0, 0, tzinfo=timezone.utc,
    )
    return dt.isoformat()


def _rebase_event(
    *,
    cancelled_id: str,
    cancelled_version: str,
    cancelled_by: str,
    at: str,
    previous_parents: list[str],
    new_parents: list[str],
    previous_parent_versions: list[str],
    new_parent_versions: list[str],
) -> dict:
    return {
        "at": at,
        "cancelledBookingId": cancelled_id,
        "cancelledVersion": cancelled_version,
        "cancelledBy": cancelled_by,
        "previousParents": previous_parents,
        "newParents": new_parents,
        "previousParentVersions": previous_parent_versions,
        "newParentVersions": new_parent_versions,
    }


def _mk(
    *,
    hid: str,
    version: str,
    components: list[str],
    clients: list[str],
    booker: tuple[str, str],
    booked_at: str,
    parents: list[str] | None = None,
    original_parents: list[str] | None = None,
    rebase_history: list[dict] | None = None,
    status: str = "booked",
    cancelled_by: tuple[str, str] | None = None,
    cancelled_at: str | None = None,
) -> dict:
    name, email = booker
    record: dict = {
        "id": hid,
        "version": version,
        "components": components,
        "clientEnvironments": clients,
        "bookedBy": name,
        "bookedByEmail": email,
        "bookedAt": booked_at,
        "status": status,
        "parents": parents or [],
        "originalParents": original_parents if original_parents is not None else list(parents or []),
        "rebaseHistory": rebase_history or [],
    }
    if status == "cancelled":
        assert cancelled_by is not None and cancelled_at is not None
        cname, cemail = cancelled_by
        record["cancelledBy"] = cname
        record["cancelledByEmail"] = cemail
        record["cancelledAt"] = cancelled_at
    return record


def build_seed(current_user: tuple[str, str] | None) -> list[dict]:
    """Assemble the full seed dataset.

    ``current_user`` is either ``None`` (no user-owned rows) or a
    ``(display_name, email)`` tuple that gets stamped onto the J and L
    scenarios. Resolving the email to a real Jira displayName is the caller's
    job — see ``resolve_user_or_error``.

    Version numbers cluster around `9.98.5xx` (well above realistic Jira
    traffic) EXCEPT scenario L which deliberately uses `9.98.1` so it collides
    with a version that may already exist in Jira — that's how we exercise the
    "Active CM in Jira" amber warning end-to-end.
    """
    seed: list[dict] = []
    now_users_own = current_user

    # ==================================================================
    # A) Active chain of 3 — no cancels
    # ==================================================================
    seed.append(_mk(hid="HB-SEED-500", version="9.98.500",
        components=["Alerts_REST"], clients=["CL001 - Fortress"],
        booker=ALICE, booked_at=_at(6, MORNING_UTC)))
    seed.append(_mk(hid="HB-SEED-501", version="9.98.501",
        components=["Alerts_REST"], clients=["CL001 - Fortress"],
        booker=BOB, booked_at=_at(6, AFTERNOON_UTC),
        parents=["HB-SEED-500"], original_parents=["HB-SEED-500"]))
    seed.append(_mk(hid="HB-SEED-502", version="9.98.502",
        components=["Alerts_REST"], clients=["CL001 - Fortress"],
        booker=CAROL, booked_at=_at(5, MORNING_UTC),
        parents=["HB-SEED-501"], original_parents=["HB-SEED-501"]))

    # ==================================================================
    # B) Middle-of-chain cancelled (user's Example 2)
    #    505 → 506 → 507; Bob cancels 506; 507 rebased to 505.
    # ==================================================================
    seed.append(_mk(hid="HB-SEED-505", version="9.98.505",
        components=["Analytics"], clients=["CL002 - Convex"],
        booker=ALICE, booked_at=_at(5, AFTERNOON_UTC)))
    b_cancel_at = _at(4, MORNING_UTC)  # cancelled next morning
    seed.append(_mk(hid="HB-SEED-506", version="9.98.506",
        components=["Analytics"], clients=["CL002 - Convex"],
        booker=BOB, booked_at=_at(5, AFTERNOON_UTC),
        parents=["HB-SEED-505"], original_parents=["HB-SEED-505"],
        status="cancelled", cancelled_by=BOB, cancelled_at=b_cancel_at))
    seed.append(_mk(hid="HB-SEED-507", version="9.98.507",
        components=["Analytics"], clients=["CL002 - Convex"],
        booker=CAROL, booked_at=_at(5, AFTERNOON_UTC),
        parents=["HB-SEED-505"], original_parents=["HB-SEED-506"],
        rebase_history=[_rebase_event(
            cancelled_id="HB-SEED-506", cancelled_version="9.98.506",
            cancelled_by=BOB[0], at=b_cancel_at,
            previous_parents=["HB-SEED-506"], new_parents=["HB-SEED-505"],
            previous_parent_versions=["9.98.506"], new_parent_versions=["9.98.505"],
        )]))

    # ==================================================================
    # C) Fan-out cancel — parent covers 2 cells, 2 children on disjoint cells.
    #    510 (REST × C3 + TM × C4) → children 511 (REST × C3) and 512 (TM × C4).
    #    Alice cancels 510; both children rebase to baseline.
    # ==================================================================
    c_cancel_at = _at(3, MORNING_UTC)
    seed.append(_mk(hid="HB-SEED-510", version="9.98.510",
        components=["Alerts_REST", "TradeMatcher"],
        clients=["CL003 - TPG", "CL004 - Quadratic"],
        booker=ALICE, booked_at=_at(4, MORNING_UTC),
        status="cancelled", cancelled_by=ALICE, cancelled_at=c_cancel_at))
    seed.append(_mk(hid="HB-SEED-511", version="9.98.511",
        components=["Alerts_REST"], clients=["CL003 - TPG"],
        booker=BOB, booked_at=_at(4, AFTERNOON_UTC),
        parents=[], original_parents=["HB-SEED-510"],
        rebase_history=[_rebase_event(
            cancelled_id="HB-SEED-510", cancelled_version="9.98.510",
            cancelled_by=ALICE[0], at=c_cancel_at,
            previous_parents=["HB-SEED-510"], new_parents=[],
            previous_parent_versions=["9.98.510"], new_parent_versions=[],
        )]))
    seed.append(_mk(hid="HB-SEED-512", version="9.98.512",
        components=["TradeMatcher"], clients=["CL004 - Quadratic"],
        booker=CAROL, booked_at=_at(4, AFTERNOON_UTC),
        parents=[], original_parents=["HB-SEED-510"],
        rebase_history=[_rebase_event(
            cancelled_id="HB-SEED-510", cancelled_version="9.98.510",
            cancelled_by=ALICE[0], at=c_cancel_at,
            previous_parents=["HB-SEED-510"], new_parents=[],
            previous_parent_versions=["9.98.510"], new_parent_versions=[],
        )]))

    # ==================================================================
    # D) DAG multi-parent (all active) — 517 has two parents on disjoint cells.
    # ==================================================================
    seed.append(_mk(hid="HB-SEED-515", version="9.98.515",
        components=["Alerts_REST"], clients=["CL005 - Balyasny"],
        booker=ALICE, booked_at=_at(3, AFTERNOON_UTC)))
    seed.append(_mk(hid="HB-SEED-516", version="9.98.516",
        components=["TradeMatcher"], clients=["CL005 - Balyasny"],
        booker=BOB, booked_at=_at(3, AFTERNOON_UTC)))
    seed.append(_mk(hid="HB-SEED-517", version="9.98.517",
        components=["Alerts_REST", "TradeMatcher"], clients=["CL005 - Balyasny"],
        booker=CAROL, booked_at=_at(2, MORNING_UTC),
        parents=["HB-SEED-516", "HB-SEED-515"],
        original_parents=["HB-SEED-516", "HB-SEED-515"]))

    # ==================================================================
    # E) DAG partial cancel WITH FALLBACK to an older overlapping hotfix.
    #    519 (REST × C7)  — Frank, active. Predates the cancelled 520.
    #    520 (REST × C7)  — Alice, based on 519, LATER CANCELLED.
    #    521 (TM × C7)    — Bob, active.
    #    522 (REST+TM × C7) — Carol, was [521, 520].
    #    Alice cancels 520 → 522's REST cell falls back to 519 (NOT baseline)
    #    because 519 still covers (C7, REST). Result: 522.parents = [521, 519].
    # ==================================================================
    e_cancel_at = _at(1, MORNING_UTC)
    seed.append(_mk(hid="HB-SEED-519", version="9.98.519",
        components=["Alerts_REST"], clients=["CL007 - Haidar"],
        booker=FRANK, booked_at=_at(3, MORNING_UTC)))
    seed.append(_mk(hid="HB-SEED-520", version="9.98.520",
        components=["Alerts_REST"], clients=["CL007 - Haidar"],
        booker=ALICE, booked_at=_at(2, MORNING_UTC),
        parents=["HB-SEED-519"], original_parents=["HB-SEED-519"],
        status="cancelled", cancelled_by=ALICE, cancelled_at=e_cancel_at))
    seed.append(_mk(hid="HB-SEED-521", version="9.98.521",
        components=["TradeMatcher"], clients=["CL007 - Haidar"],
        booker=BOB, booked_at=_at(2, MORNING_UTC)))
    seed.append(_mk(hid="HB-SEED-522", version="9.98.522",
        components=["Alerts_REST", "TradeMatcher"], clients=["CL007 - Haidar"],
        booker=CAROL, booked_at=_at(2, AFTERNOON_UTC),
        parents=["HB-SEED-521", "HB-SEED-519"],
        original_parents=["HB-SEED-521", "HB-SEED-520"],
        rebase_history=[_rebase_event(
            cancelled_id="HB-SEED-520", cancelled_version="9.98.520",
            cancelled_by=ALICE[0], at=e_cancel_at,
            previous_parents=["HB-SEED-521", "HB-SEED-520"],
            new_parents=["HB-SEED-521", "HB-SEED-519"],
            previous_parent_versions=["9.98.521", "9.98.520"],
            new_parent_versions=["9.98.521", "9.98.519"],
        )]))

    # ==================================================================
    # F) Repeat rebase — 527 rebased twice through two upstream cancels.
    #    Chain: 525 → 526 → 527.
    #    Step 1: Bob cancels 526 → 527 rebases 526 → 525.
    #    Step 2: Alice cancels 525 → 527 rebases 525 → baseline.
    #    527 ends with a 2-event rebaseHistory (chronologically ordered).
    # ==================================================================
    f_cancel_526_at = _at(1, MORNING_UTC)
    f_cancel_525_at = _at(1, AFTERNOON_UTC)
    seed.append(_mk(hid="HB-SEED-525", version="9.98.525",
        components=["DV_Web"], clients=["CL009 - Shay"],
        booker=ALICE, booked_at=_at(2, AFTERNOON_UTC),
        status="cancelled", cancelled_by=ALICE, cancelled_at=f_cancel_525_at))
    seed.append(_mk(hid="HB-SEED-526", version="9.98.526",
        components=["DV_Web"], clients=["CL009 - Shay"],
        booker=BOB, booked_at=_at(2, AFTERNOON_UTC),
        parents=[], original_parents=["HB-SEED-525"],
        status="cancelled", cancelled_by=BOB, cancelled_at=f_cancel_526_at,
        rebase_history=[_rebase_event(
            cancelled_id="HB-SEED-525", cancelled_version="9.98.525",
            cancelled_by=ALICE[0], at=f_cancel_525_at,
            previous_parents=["HB-SEED-525"], new_parents=[],
            previous_parent_versions=["9.98.525"], new_parent_versions=[],
        )]))
    seed.append(_mk(hid="HB-SEED-527", version="9.98.527",
        components=["DV_Web"], clients=["CL009 - Shay"],
        booker=CAROL, booked_at=_at(2, AFTERNOON_UTC),
        parents=[], original_parents=["HB-SEED-526"],
        rebase_history=[
            _rebase_event(
                cancelled_id="HB-SEED-526", cancelled_version="9.98.526",
                cancelled_by=BOB[0], at=f_cancel_526_at,
                previous_parents=["HB-SEED-526"], new_parents=["HB-SEED-525"],
                previous_parent_versions=["9.98.526"], new_parent_versions=["9.98.525"],
            ),
            _rebase_event(
                cancelled_id="HB-SEED-525", cancelled_version="9.98.525",
                cancelled_by=ALICE[0], at=f_cancel_525_at,
                previous_parents=["HB-SEED-525"], new_parents=[],
                previous_parent_versions=["9.98.525"], new_parent_versions=[],
            ),
        ]))

    # ==================================================================
    # G) Standalone cancelled tombstone (no downstream impact).
    # ==================================================================
    g_cancel_at = _at(1, AFTERNOON_UTC)
    seed.append(_mk(hid="HB-SEED-530", version="9.98.530",
        components=["BBG SAPI (2)"], clients=["CL011 - Linden"],
        booker=DAVID, booked_at=_at(2, MORNING_UTC),
        status="cancelled", cancelled_by=DAVID, cancelled_at=g_cancel_at))

    # ==================================================================
    # H) Rebased singleton — 531 was originally based on 530; David
    #    cancelled 530 so 531 is now baseline with 1 rebase event.
    # ==================================================================
    seed.append(_mk(hid="HB-SEED-531", version="9.98.531",
        components=["BBG SAPI (2)"], clients=["CL011 - Linden"],
        booker=EVE, booked_at=_at(2, AFTERNOON_UTC),
        parents=[], original_parents=["HB-SEED-530"],
        rebase_history=[_rebase_event(
            cancelled_id="HB-SEED-530", cancelled_version="9.98.530",
            cancelled_by=DAVID[0], at=g_cancel_at,
            previous_parents=["HB-SEED-530"], new_parents=[],
            previous_parent_versions=["9.98.530"], new_parent_versions=[],
        )]))

    # ==================================================================
    # I) Booking with many components + many clients — exercises the
    #    "+N more" expandable tag row in My Hotfixes.
    # ==================================================================
    seed.append(_mk(hid="HB-SEED-535", version="9.98.535",
        components=["Alerts_REST", "TradeMatcher", "Analytics", "DV_Web",
                    "BBG SAPI (2)", "ScenarioBatch", "ScenarioEngine"],
        clients=["CL001 - Fortress", "CL002 - Convex", "CL003 - TPG",
                 "CL004 - Quadratic", "CL005 - Balyasny", "CL007 - Haidar"],
        booker=FRANK, booked_at=_at(0, MORNING_UTC)))

    # ==================================================================
    # K) Grandchild UNTOUCHED when grandparent is cancelled (linear chain).
    #    599 → 600 → 601. Alice cancels 599 → 600 rebases to baseline;
    #    601 stays put (its direct parent 600 still exists).
    # ==================================================================
    k_cancel_at = _at(0, AFTERNOON_UTC)
    seed.append(_mk(hid="HB-SEED-599", version="9.98.599",
        components=["ScenarioBatch"], clients=["CL024 - Balyasny"],
        booker=ALICE, booked_at=_at(1, MORNING_UTC),
        status="cancelled", cancelled_by=ALICE, cancelled_at=k_cancel_at))
    seed.append(_mk(hid="HB-SEED-600", version="9.98.600",
        components=["ScenarioBatch"], clients=["CL024 - Balyasny"],
        booker=BOB, booked_at=_at(1, AFTERNOON_UTC),
        parents=[], original_parents=["HB-SEED-599"],
        rebase_history=[_rebase_event(
            cancelled_id="HB-SEED-599", cancelled_version="9.98.599",
            cancelled_by=ALICE[0], at=k_cancel_at,
            previous_parents=["HB-SEED-599"], new_parents=[],
            previous_parent_versions=["9.98.599"], new_parent_versions=[],
        )]))
    seed.append(_mk(hid="HB-SEED-601", version="9.98.601",
        components=["ScenarioBatch"], clients=["CL024 - Balyasny"],
        booker=CAROL, booked_at=_at(0, MORNING_UTC),
        # 601's direct parent is 600 (still exists) — no rebase applied to 601.
        parents=["HB-SEED-600"], original_parents=["HB-SEED-600"]))

    # ==================================================================
    # J) Your own active chain (only when --email was passed).
    # ==================================================================
    if now_users_own:
        seed.append(_mk(hid="HB-SEED-540", version="9.98.540",
            components=["Alerts_REST"], clients=["CL015 - Millennium"],
            booker=now_users_own, booked_at=_at(0, MORNING_UTC)))
        seed.append(_mk(hid="HB-SEED-541", version="9.98.541",
            components=["Alerts_REST"], clients=["CL015 - Millennium"],
            booker=now_users_own, booked_at=_at(0, AFTERNOON_UTC),
            parents=["HB-SEED-540"], original_parents=["HB-SEED-540"]))

        # ==============================================================
        # L) Booking for 9.98.1 (deliberate collision with a real Jira CM).
        #    Pretends you'd booked 9.98.1 in the app BEFORE the CM was
        #    created. Two rows will show for 9.98.1 — the deployed Jira
        #    row (read-only) and this booked one (with a Cancel button).
        #    Cancelling triggers the amber "Active CM in Jira" warning
        #    when the CM's status is anything other than terminal
        #    ({Rollback, Rejected, Cancelled, Open}).
        #    NB: `/next-version`'s deploy-based cleanup now filters on the
        #    CM's ACTUAL deploy status, so this booking survives unless
        #    CM-11687 flips to Done / Deployment Completed.
        # ==============================================================
        seed.append(_mk(hid="HB-SEED-CM-COLLIDE", version="9.98.1",
            components=["Alerts_REST"], clients=["CL001 - Fortress"],
            booker=now_users_own, booked_at=_at(0, MORNING_UTC)))

    return seed


def _backup(path: Path) -> Path | None:
    """Create a pre-seed backup ONCE. Subsequent `load` runs preserve the
    original backup instead of overwriting it with the current (already-seeded)
    data — otherwise `clear` would restore seed data, not the original.
    """
    if not path.exists():
        return None
    bak = path.with_suffix(path.suffix + BACKUP_SUFFIX)
    if bak.exists():
        # Already backed up — don't clobber the original.
        return bak
    shutil.copy2(path, bak)
    return bak


def _restore_or_clear(path: Path) -> str:
    bak = path.with_suffix(path.suffix + BACKUP_SUFFIX)
    if bak.exists():
        shutil.copy2(bak, path)
        return f"restored {path.name} from {bak.name}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"bookings": []}, indent=2), encoding="utf-8")
    return f"no backup found — wrote empty bookings to {path.name}"


def resolve_user_or_error(email: str) -> tuple[str, str]:
    """Resolve ``email`` to ``(displayName, email)`` via Jira's user-search API.

    Uses the same credentials (``JIRA_BASE_URL`` / ``JIRA_EMAIL`` /
    ``JIRA_API_TOKEN``) the app reads from ``.env``. Raises ``RuntimeError``
    with a user-facing message when:
      * credentials aren't configured,
      * the HTTP call fails,
      * or no active real user record matches the query email
        (Jira's ``qm:``-prefixed service stubs are filtered out).
    """
    # Imports live inside the function so ``import seed`` in tests doesn't
    # eagerly execute ``get_settings()`` (which reads .env at import time via
    # ``hotfix_booking.config``). Keeping the import lazy also means running
    # ``python tools/seed.py load`` (no --email) never touches Jira.
    import httpx

    from hotfix_booking.config import get_settings
    from hotfix_booking.jira_client import _auth_header
    from hotfix_booking.users import resolve_jira_user

    settings = get_settings()
    if not (settings.jira_base_url and settings.jira_email and settings.jira_api_token):
        raise RuntimeError(
            "Jira credentials not configured. Populate JIRA_BASE_URL, "
            "JIRA_EMAIL and JIRA_API_TOKEN in .env before running with --email."
        )

    try:
        with httpx.Client(
            base_url=settings.jira_base_url,
            headers=_auth_header(settings),
            timeout=30.0,
        ) as client:
            resp = client.get("/rest/api/3/user/search", params={"query": email})
            resp.raise_for_status()
            users = resp.json() or []
    except httpx.HTTPError as e:
        raise RuntimeError(f"Jira user-search request failed: {e}") from e

    user = resolve_jira_user(email, users)
    if user is None:
        raise RuntimeError(
            f"No active Jira user found for '{email}'. "
            "Check the address and try again."
        )
    return (user["displayName"], email)


def cmd_load(bookings_file: Path, current_user_email: str | None) -> int:
    current_user: tuple[str, str] | None = None
    if current_user_email:
        try:
            current_user = resolve_user_or_error(current_user_email)
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        print(f"Resolved '{current_user_email}' to Jira user '{current_user[0]}'")

    bak = _backup(bookings_file)
    seed = build_seed(current_user)
    bookings_file.parent.mkdir(parents=True, exist_ok=True)
    bookings_file.write_text(
        json.dumps({"bookings": seed}, indent=2), encoding="utf-8"
    )
    print(f"Wrote {len(seed)} seed bookings to {bookings_file}")
    if bak:
        print(f"  (previous file backed up to {bak.name})")
    print("  Scenarios covered:")
    print("    A) 500-502  active chain of 3")
    print("    B) 505-507  middle cancelled (Bob cancels 506) -> 507 rebased to 505")
    print("    C) 510-512  fan-out: Alice cancels 510, both 511 and 512 rebase to baseline")
    print("    D) 515-517  DAG multi-parent (no cancels) -- 517 based on both 515+516")
    print("    E) 519-522  DAG partial cancel WITH fallback:")
    print("                Alice cancels 520 -> 522 falls back to 519 (not baseline)")
    print("                because 519 still covers (CL007, Alerts_REST)")
    print("    F) 525-527  repeat rebase -- 527 rebased twice (see popover)")
    print("    G) 530      standalone cancelled tombstone")
    print("    H) 531      rebased singleton (was based on 530)")
    print("    I) 535      many components + clients (tests +N more)")
    print("    K) 599-601  grandchild UNTOUCHED -- Alice cancels 599, 600 rebases,")
    print("                but 601 (parent=600) stays put with no rebase history")
    if current_user:
        print(f"    J) 540-541  {current_user[0]}'s chain -- cancel 540 to see 541 rebase live")
        print(f"    L) 9.98.1   {current_user[0]}'s booking that collides with Jira CM-11687.")
        print("                Shows as TWO rows (Jira deployed + your booked).")
        print("                Cancel the booked row to trigger the 'Active CM' warning.")
    else:
        print("  Tip: run with --email you@neovest.com to also get scenarios J and L")
        print("       (your own bookings with a Cancel button).")
    return 0


def cmd_clear(bookings_file: Path) -> int:
    result = _restore_or_clear(bookings_file)
    print(result)
    return 0


def _resolve_bookings_file(cli_path: str | None) -> Path:
    if cli_path:
        return Path(cli_path)
    import os
    env_path = os.environ.get("BOOKINGS_FILE")
    if env_path:
        return Path(env_path)
    return DEFAULT_BOOKINGS_FILE


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="seed",
        description="Load or clear a UI-testing seed for hotfix bookings.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_load = sub.add_parser("load", help="Write the seed dataset (backs up current file).")
    p_load.add_argument("--email", default=None,
                         help="Attribute 3 bookings to this email so Cancel buttons appear.")
    p_load.add_argument("--file", default=None,
                         help="Override the bookings JSON path (default: BOOKINGS_FILE env or data/hotfix-bookings.json).")

    p_clear = sub.add_parser("clear", help="Restore from backup, or clear to empty if no backup.")
    p_clear.add_argument("--file", default=None,
                          help="Override the bookings JSON path.")

    args = parser.parse_args(argv)
    bookings_file = _resolve_bookings_file(args.file)

    if args.command == "load":
        return cmd_load(bookings_file, args.email)
    if args.command == "clear":
        return cmd_clear(bookings_file)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
