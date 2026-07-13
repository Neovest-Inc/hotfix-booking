"""HTTP routes."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from .auth import UserContext, require_user
from .config import get_settings
from .dependencies import cm_to_pseudo_booking
from .history import (
    calculate_next_version,
    cleanup_bookings,
    derive_minor_versions,
    deployed_versions,
    merge_hotfixes,
)
from .jira_client import JiraClient, clear_cache as _clear_jira_cache
from .matrix import build_version_matrix
from .store import (
    AlreadyBookedError,
    AlreadyCancelledError,
    BookingNotFoundError,
    InvalidVersionError,
    MalformedBookingsError,
    bookings_lock,
    cancel_booking,
    create_booking,
    load_bookings,
    save_bookings,
)
from .teams_notifier import notify_booking_cancelled, notify_booking_created
from .versioning import is_semver, parse_version

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hotfix-booking")


def _jira(request: Request) -> JiraClient:
    """Build a JiraClient bound to the app-lifetime httpx client.

    Reusing the persistent client (created in `app._lifespan`) avoids the
    per-request TLS handshake to Jira (~50-100ms/call). The JiraClient
    context manager sees `_owns_client=False` when a client is passed in
    and will NOT close it — the lifespan owns closing at shutdown.

    Tests may override the client entirely via
    `request.app.state.jira_client_factory` (unused at present but kept for
    flexibility). Tests may also override `app.state.httpx_client` directly.
    """
    settings = get_settings()
    factory = getattr(request.app.state, "jira_client_factory", None)
    if factory is not None:
        return factory(settings)
    shared = getattr(request.app.state, "httpx_client", None)
    return JiraClient(settings, client=shared)


def _malformed_response() -> JSONResponse:
    return JSONResponse(
        {"error": "Bookings file is malformed. Refusing to proceed."},
        status_code=500,
    )


# ---------------------------------------------------------------------------
# GET /field-options
# ---------------------------------------------------------------------------
@router.get("/field-options")
async def field_options(
    request: Request,
    fresh: bool = Query(default=False),
    _user: UserContext = Depends(require_user),
) -> Any:
    if fresh:
        _clear_jira_cache()
    try:
        async with _jira(request) as jira:
            components = await jira.fetch_components()
            clients = await jira.fetch_client_options()
        return {"components": components, "clients": clients}
    except Exception as e:  # noqa: BLE001 — matches Node's broad catch
        log.error("Field options error: %s", e)
        return JSONResponse({"error": "Failed to fetch field options"}, status_code=500)


# ---------------------------------------------------------------------------
# GET /deployed-cms
# ---------------------------------------------------------------------------
@router.get("/deployed-cms")
async def deployed_cms(
    request: Request,
    fresh: bool = Query(default=False),
    _user: UserContext = Depends(require_user),
) -> Any:
    if fresh:
        _clear_jira_cache()
    try:
        async with _jira(request) as jira:
            cms = await jira.fetch_deployed_cms(deployed_only=False)
        return {"cms": cms}
    except Exception as e:  # noqa: BLE001
        log.error("Deployed CMs error: %s", e)
        return JSONResponse({"error": "Failed to fetch deployed CMs"}, status_code=500)


# ---------------------------------------------------------------------------
# GET /next-version  (with auto-cleanup side effect)
# ---------------------------------------------------------------------------
@router.get("/next-version")
async def next_version(
    request: Request,
    minor: int | None = Query(default=None),
    major: int | None = Query(default=None),
    fresh: bool = Query(default=False),
    _user: UserContext = Depends(require_user),
) -> Any:
    """Compute the next available hotfix version.

    Query params:
    - Neither `major` nor `minor` → uses the current (highest) release line
      auto-discovered from recent CMs. Default use case.
    - `?major=X&minor=Y` → fetches the full history of `X.Y.*` from Jira
      (no 100-day cap) so users can book hotfixes for older, still-supported
      minors — including previous majors (e.g. 9.99.x while 10.0.x is active).

    Response always includes `currentMajor`, `currentMinor`, and `minorVersions`
    so the front-end can populate its release-selector dropdown from a single call.
    """
    settings = get_settings()
    filter_requested = major is not None and minor is not None
    if fresh:
        _clear_jira_cache()
    try:
        async with _jira(request) as jira:
            # Fetch ALL recent CMs (any status) so the release dropdown
            # includes lines that only have in-progress hotfixes (e.g. a fresh
            # 9.98 line where nothing has deployed yet). Deployed-only CMs
            # get partitioned in memory below for the max-version calc + cleanup.
            #
            # When a filter is supplied, the by-version call is independent
            # of the recent-CMs call, so dispatch both in parallel to halve
            # wall time on cache misses.
            filtered_cms: list[dict] | None = None
            if filter_requested:
                recent_cms, filtered_cms = await asyncio.gather(
                    jira.fetch_deployed_cms(deployed_only=False),
                    jira.fetch_cms_by_version(major, minor),
                )
            else:
                recent_cms = await jira.fetch_deployed_cms(deployed_only=False)
    except Exception as e:  # noqa: BLE001
        log.error("Next version error: %s", e)
        return JSONResponse({"error": "Failed to calculate next version"}, status_code=500)

    current_major, current_minor, minor_versions = derive_minor_versions(recent_cms)

    with bookings_lock():
        try:
            data = load_bookings(settings.bookings_file)
        except MalformedBookingsError:
            return _malformed_response()

        # Auto-cleanup runs on the deploy-based (recent) view regardless of
        # whether a minor filter was supplied — keeps housekeeping consistent.
        kept, removed = cleanup_bookings(
            data.get("bookings", []),
            deployed=deployed_versions(recent_cms),
            now=datetime.now(timezone.utc),
            retention_days=settings.booking_retention_days,
        )
        if removed:
            for b in removed:
                log.info("Auto-cleanup: removed booking %s", b.get("version"))
            data["bookings"] = kept
            save_bookings(settings.bookings_file, data)
        else:
            data["bookings"] = kept

    effective_major = major if filter_requested else current_major
    effective_minor = minor if filter_requested else current_minor
    cms_for_calc = filtered_cms if filtered_cms is not None else recent_cms
    result = calculate_next_version(
        cms_for_calc,
        data.get("bookings", []),
        major=effective_major if filter_requested or current_major else None,
        minor=effective_minor if filter_requested or current_minor else None,
    )

    response: dict[str, Any] = {
        "major": effective_major,
        "minor": effective_minor,
        "currentMajor": current_major,
        "currentMinor": current_minor,
        "minorVersions": minor_versions,
    }
    response.update(result)
    return response


# ---------------------------------------------------------------------------
# GET /bookings
# ---------------------------------------------------------------------------
@router.get("/bookings")
def bookings(
    minor: int | None = Query(default=None),
    major: int = Query(default=9),
    _user: UserContext = Depends(require_user),
) -> Any:
    """Return pending bookings. With `?minor=X` filters to that release line."""
    settings = get_settings()
    try:
        data = load_bookings(settings.bookings_file)
    except MalformedBookingsError:
        return _malformed_response()

    if minor is not None:
        filtered = []
        for b in data.get("bookings", []):
            v = b.get("version", "")
            if not is_semver(v):
                continue
            p = parse_version(v)
            if p.major == major and p.minor == minor:
                filtered.append(b)
        return {"bookings": filtered}

    return data


# ---------------------------------------------------------------------------
# POST /book
# ---------------------------------------------------------------------------
@router.post("/book")
async def book(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: dict = Body(default_factory=dict),
    user: UserContext = Depends(require_user),
) -> Any:
    settings = get_settings()

    version = payload.get("version")
    components = payload.get("components")
    client_envs = payload.get("clientEnvironments")
    # Identity always comes from the session — any `bookedBy` / `bookedByEmail`
    # in the payload is ignored (was a spoofing vector under the old flow).
    booked_by = user.displayName
    booked_by_email = user.email

    if not version or components is None or client_envs is None:
        return JSONResponse(
            {"error": "Missing required fields: version, components, clientEnvironments"},
            status_code=400,
        )
    if not isinstance(components, list) or len(components) == 0:
        return JSONResponse(
            {"error": "At least one component is required"}, status_code=400
        )
    if not isinstance(client_envs, list) or len(client_envs) == 0:
        return JSONResponse(
            {"error": "At least one client environment is required"}, status_code=400
        )
    if not is_semver(version):
        return JSONResponse(
            {"error": f"Invalid version format: {version!r}. Expected x.y.z (e.g. 9.97.82)."},
            status_code=400,
        )

    # Fresh-next check: the submitted version tells us which minor line the
    # user is targeting (e.g. 9.95.7 → the 9.95.x release). Query Jira for the
    # full history of that specific minor (no 100-day cap) so hotfixes for
    # older, still-supported minors work.
    parsed = parse_version(version)
    try:
        async with _jira(request) as jira:
            cms = await jira.fetch_cms_by_version(parsed.major, parsed.minor)
    except Exception as e:  # noqa: BLE001
        log.error("Book (jira fetch) error: %s", e)
        return JSONResponse(
            {"error": "Failed to verify next version against Jira"}, status_code=500
        )

    with bookings_lock():
        try:
            data = load_bookings(settings.bookings_file)
        except MalformedBookingsError:
            return _malformed_response()

        current = calculate_next_version(
            cms,
            data.get("bookings", []),
            major=parsed.major,
            minor=parsed.minor,
        )
        if current.get("nextVersion") is None:
            return JSONResponse(
                {
                    "error": (
                        f"No prior hotfixes found for {parsed.major}.{parsed.minor}.x "
                        "in Jira; cannot determine next version."
                    ),
                },
                status_code=409,
            )
        if version != current["nextVersion"]:
            return JSONResponse(
                {
                    "error": (
                        f"Version {version} is no longer the next available for "
                        f"{parsed.major}.{parsed.minor}.x. Current next is "
                        f"{current['nextVersion']}."
                    ),
                    "currentNext": current["nextVersion"],
                },
                status_code=409,
            )

        try:
            new_booking, data = create_booking(
                data,
                version=version,
                components=components,
                client_environments=client_envs,
                booked_by=booked_by,
                booked_by_email=booked_by_email,
                additional_priors=[
                    p for p in (
                        cm_to_pseudo_booking(cm, major=parsed.major, minor=parsed.minor)
                        for cm in cms
                    ) if p is not None
                ],
            )
        except InvalidVersionError as e:
            return JSONResponse(
                {"error": f"Invalid version format: {e}. Expected x.y.z (e.g. 9.97.82)."},
                status_code=400,
            )
        except AlreadyBookedError as e:
            # Defense in depth — the fresh-next check above should already have caught this.
            return JSONResponse(
                {"error": f"Version {e.version} is already booked", "existingBooking": e.existing},
                status_code=409,
            )
        except Exception as e:  # noqa: BLE001
            log.error("Booking error: %s", e)
            return JSONResponse({"error": "Failed to book version"}, status_code=500)

        save_bookings(settings.bookings_file, data)

    # Backward-compat notification into the legacy hotfix Teams chat.
    # Best-effort — the notifier itself swallows any error, but keeping this
    # after the lock and only on the success path is intentional.
    background_tasks.add_task(notify_booking_created, new_booking)

    return {"success": True, "booking": new_booking}


# ---------------------------------------------------------------------------
# GET /client-versions
# ---------------------------------------------------------------------------
@router.get("/client-versions")
async def client_versions(
    request: Request,
    fresh: bool = Query(default=False),
    _user: UserContext = Depends(require_user),
) -> Any:
    if fresh:
        _clear_jira_cache()
    settings = get_settings()
    try:
        async with _jira(request) as jira:
            # deployed_only=False so we get everything (deployed +
            # in-flight + cancelled) and let `build_version_matrix`
            # bucket by status. `in_flight_only=True` narrows the
            # output to CMs currently moving through the workflow —
            # the matrix intentionally hides already-shipped versions
            # here because the "what is really deployed" question is
            # answered elsewhere (a DevOps tool that reads the actual
            # client servers). This surface focuses on work in flight.
            cms = await jira.fetch_deployed_cms(deployed_only=False)
    except Exception as e:  # noqa: BLE001
        log.error("Client versions error: %s", e)
        return JSONResponse({"error": "Failed to fetch client versions"}, status_code=500)

    result = build_version_matrix(cms, in_flight_only=True)
    # Expose the Jira base URL so the matrix in-flight chips can link CM
    # keys to their tickets, independent of whether the History view has
    # been visited this session.
    result["jiraBaseUrl"] = settings.jira_base_url
    return result


# ---------------------------------------------------------------------------
# GET /history?minor=&major=
# ---------------------------------------------------------------------------
@router.get("/history")
async def history(
    request: Request,
    minor: int | None = Query(default=None),
    major: int | None = Query(default=None),
    fresh: bool = Query(default=False),
    _user: UserContext = Depends(require_user),
) -> Any:
    settings = get_settings()
    filter_requested = major is not None and minor is not None
    if fresh:
        _clear_jira_cache()
    try:
        async with _jira(request) as jira:
            if filter_requested:
                # `target_major/minor` come from query params, so both Jira
                # calls are independent — dispatch in parallel to halve wall
                # time on cache misses (roughly 700ms → 400ms end-to-end).
                recent_cms, version_cms = await asyncio.gather(
                    jira.fetch_deployed_cms(deployed_only=False),
                    jira.fetch_cms_by_version(major, minor),
                )
                current_major, current_minor, minor_versions = derive_minor_versions(recent_cms)
                target_major = major
                target_minor = minor
            else:
                # Without a filter, the second call's args depend on the first
                # call's result (derived current release), so this path must
                # stay sequential.
                recent_cms = await jira.fetch_deployed_cms(deployed_only=False)
                current_major, current_minor, minor_versions = derive_minor_versions(recent_cms)
                target_major = current_major
                target_minor = current_minor
                version_cms = await jira.fetch_cms_by_version(target_major, target_minor)
    except Exception as e:  # noqa: BLE001
        log.error("Hotfix history error: %s", e)
        return JSONResponse({"error": "Failed to fetch hotfix history"}, status_code=500)

    try:
        booking_data = load_bookings(settings.bookings_file)
    except MalformedBookingsError:
        return _malformed_response()
    hotfixes = merge_hotfixes(
        version_cms, booking_data.get("bookings", []), major=target_major, target_minor=target_minor
    )

    return {
        "minorVersions": minor_versions,
        "currentMajor": current_major,
        "currentMinor": current_minor,
        "targetMajor": target_major,
        "targetMinor": target_minor,
        "hotfixes": hotfixes,
        "jiraBaseUrl": settings.jira_base_url,
    }


# ---------------------------------------------------------------------------
# POST /cancel
# ---------------------------------------------------------------------------
# Jira CM statuses that represent a SETTLED change — either shipped (positive
# terminals) or abandoned (negative terminals). If the ONLY CMs for a version
# are in one of these states, cancelling the local booking is not surprising
# (there's no live work in Jira to coordinate on) so we suppress the amber
# "Active CM in Jira" warning.
#
# Positive terminals: Done and Deployment Completed both mean shipped.
# Global Review is a post-deploy audit state — CMs land there AFTER
# deployment, so it's treated as terminal-successful too.
# Negative terminals: Rollback / Rejected / Cancelled never shipped.
#
# Any status NOT in this set is considered in-flight (Open, DL Approved,
# Today's Deployments, In Progress, Business Approved, QA Approved, plus any
# future workflow status) and DOES trigger the warning — the safer default
# for unknown future statuses.
_TERMINAL_CM_STATUSES = {
    "done",
    "deployment completed",
    "global review",
    "rollback",
    "rejected",
    "cancelled",
}


def _active_cm_for_version(cms: list[dict], version: str) -> dict | None:
    """Return the first Jira CM whose fixVersions include `version` and whose
    status is IN FLIGHT (not terminal). See `_TERMINAL_CM_STATUSES`."""
    for cm in cms:
        if version not in (cm.get("fixVersions") or []):
            continue
        status_name = (cm.get("status") or "").strip().lower()
        if status_name in _TERMINAL_CM_STATUSES:
            continue
        return cm
    return None


def _is_cm_reporter(cms: list[dict], version: str, display_name: str) -> bool:
    """True iff any CM for `version` has `reporter` matching `display_name`
    (case-insensitive, whitespace-trimmed).

    Being the reporter of the Jira CM is a valid alternative to being the
    booker or an admin — the person who filed the change request can also
    close out the local booking.
    """
    if not display_name:
        return False
    target = display_name.strip().lower()
    for cm in cms:
        if version not in (cm.get("fixVersions") or []):
            continue
        reporter = (cm.get("reporter") or "").strip().lower()
        if reporter and reporter == target:
            return True
    return False


@router.post("/cancel")
async def cancel(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: dict = Body(default_factory=dict),
    user: UserContext = Depends(require_user),
) -> Any:
    """Cancel a booking and rebase its direct downstream children.

    Caller identity comes from the signed session cookie — any
    `cancelledByEmail` in the payload is ignored (was a spoofing vector).

    Authorization — any ONE of the following grants permission:
      - session email matches the booking's `bookedByEmail` (case-insensitive)
      - session email is in `ADMIN_EMAILS`
      - session displayName matches the `reporter` on a Jira CM for the
        booking's version (i.e. the person who filed the CM in Jira can
        also close out the app-side booking)

    Cancellation is a soft-delete — the version stays in the store (burned) so
    `next-version` keeps counting past it and the History view keeps an audit
    trail. Direct children have their `parents` recomputed against the store
    as-if the cancelled record never existed.

    Note: this endpoint operates on LOCAL bookings only. Cancelling a Jira CM
    (a version that has no corresponding local booking) is not supported here
    by design — CM lifecycle belongs in Jira, and merging CM state into the
    local DAG would require synchronisation we deliberately avoid.
    """
    settings = get_settings()

    booking_id = payload.get("bookingId")
    if not booking_id or not isinstance(booking_id, str):
        return JSONResponse(
            {"error": "Missing required field: bookingId"}, status_code=400
        )

    cancelled_by_email = user.email
    cancelled_by_name = user.displayName
    email_lc = cancelled_by_email.strip().lower()
    is_admin = email_lc in {e.lower() for e in settings.admin_emails}

    # Preliminary read (no lock) to learn the target booking's version so we
    # can query Jira for the associated CMs. Any race between this read and
    # the mutating lock below is caught inside the lock (404 / 409).
    try:
        prelim = load_bookings(settings.bookings_file)
    except MalformedBookingsError:
        return _malformed_response()
    prelim_target = next(
        (b for b in prelim.get("bookings", []) if b.get("id") == booking_id), None
    )
    if prelim_target is None:
        return JSONResponse(
            {"error": f"Booking {booking_id} not found"}, status_code=404
        )
    version = prelim_target.get("version") or ""

    # Fetch Jira CMs for that version once — we use them for BOTH the reporter
    # auth path AND the post-cancel "active CM in Jira" warning.
    version_cms: list[dict] = []
    if is_semver(version):
        parsed = parse_version(version)
        try:
            async with _jira(request) as jira:
                version_cms = await jira.fetch_cms_by_version(parsed.major, parsed.minor)
        except Exception as e:  # noqa: BLE001
            # Non-fatal: we still enforce owner/admin auth. Only the reporter
            # shortcut and the active-CM warning become unavailable.
            log.warning("Cancel: Jira CM lookup failed (non-fatal): %s", e)
    is_cm_reporter = _is_cm_reporter(version_cms, version, cancelled_by_name)

    with bookings_lock():
        try:
            data = load_bookings(settings.bookings_file)
        except MalformedBookingsError:
            return _malformed_response()

        target = next(
            (b for b in data.get("bookings", []) if b.get("id") == booking_id), None
        )
        if target is None:
            return JSONResponse(
                {"error": f"Booking {booking_id} not found"}, status_code=404
            )

        booker_email = (target.get("bookedByEmail") or "").strip().lower()
        is_owner = bool(booker_email) and booker_email == email_lc
        if not (is_owner or is_admin or is_cm_reporter):
            return JSONResponse(
                {
                    "error": (
                        "Only the booker, an administrator, or the CM's Jira "
                        "reporter can cancel this booking."
                    ),
                },
                status_code=403,
            )

        try:
            cancelled_record, affected = cancel_booking(
                data,
                booking_id=booking_id,
                cancelled_by=cancelled_by_name,
                cancelled_by_email=cancelled_by_email,
                additional_priors=(
                    [
                        p for p in (
                            cm_to_pseudo_booking(cm, major=parsed.major, minor=parsed.minor)
                            for cm in version_cms
                        ) if p is not None
                    ]
                    if is_semver(version) else []
                ),
            )
        except BookingNotFoundError:
            return JSONResponse(
                {"error": f"Booking {booking_id} not found"}, status_code=404
            )
        except AlreadyCancelledError:
            return JSONResponse(
                {"error": f"Booking {booking_id} is already cancelled"},
                status_code=409,
            )
        except Exception as e:  # noqa: BLE001
            log.error("Cancel (rebase) error: %s", e)
            return JSONResponse({"error": "Failed to cancel booking"}, status_code=500)

        save_bookings(settings.bookings_file, data)

    # Reuse the CMs we already fetched to check for the "Active CM in Jira"
    # anomaly (a live CM exists for a version we just cancelled locally).
    active_cm_warning: dict | None = None
    cm = _active_cm_for_version(version_cms, version) if is_semver(version) else None
    if cm is not None:
        active_cm_warning = {
            "cmKey": cm.get("key") or "",
            "status": cm.get("status") or "",
        }

    affected_payload: list[dict] = []
    for child in affected:
        # The most-recently-appended rebaseHistory entry is this cancel's event.
        last_event = (child.get("rebaseHistory") or [{}])[-1]
        affected_payload.append(
            {
                "id": child.get("id"),
                "version": child.get("version"),
                "bookedBy": child.get("bookedBy"),
                "bookedByEmail": child.get("bookedByEmail", ""),
                "previousParentVersions": last_event.get("previousParentVersions", []),
                "newParentVersions": last_event.get("newParentVersions", []),
            }
        )

    # Backward-compat notification into the legacy hotfix Teams chat.
    # Best-effort — see teams_notifier for failure semantics.
    background_tasks.add_task(
        notify_booking_cancelled,
        cancelled_record,
        affected_payload,
        active_cm_warning=active_cm_warning,
    )

    return {
        "cancelled": cancelled_record,
        "affected": affected_payload,
        "activeCmWarning": active_cm_warning,
    }