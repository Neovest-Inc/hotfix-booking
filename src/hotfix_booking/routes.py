"""HTTP routes."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Body, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from .config import get_settings
from .history import (
    calculate_next_version,
    cleanup_bookings,
    derive_minor_versions,
    deployed_versions,
    merge_hotfixes,
)
from .jira_client import JiraClient
from .matrix import build_version_matrix
from .store import (
    AlreadyBookedError,
    InvalidVersionError,
    MalformedBookingsError,
    bookings_lock,
    create_booking,
    load_bookings,
    save_bookings,
)
from .versioning import is_semver, parse_version

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hotfix-booking")


def _jira(request: Request) -> JiraClient:
    """Build a JiraClient — tests may swap `request.app.state.jira_client_factory`."""
    settings = get_settings()
    factory = getattr(request.app.state, "jira_client_factory", None)
    if factory is not None:
        return factory(settings)
    return JiraClient(settings)


def _malformed_response() -> JSONResponse:
    return JSONResponse(
        {"error": "Bookings file is malformed. Refusing to proceed."},
        status_code=500,
    )


# ---------------------------------------------------------------------------
# GET /field-options
# ---------------------------------------------------------------------------
@router.get("/field-options")
async def field_options(request: Request) -> Any:
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
async def deployed_cms(request: Request) -> Any:
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
    major: int = Query(default=9),
) -> Any:
    """Compute the next available hotfix version.

    - Without `?minor=` → uses last-100-day deployed CMs; returns the next hotfix
      for whichever minor line is currently highest (default use case).
    - With `?minor=X` → also queries Jira for the full history of `major.minor.*`
      (no 100-day cap) so users can book hotfixes for older, still-supported minors.

    Response always includes `currentMinor` and `minorVersions` so the front-end
    can populate its release-selector dropdown from a single call.
    """
    settings = get_settings()
    try:
        async with _jira(request) as jira:
            recent_cms = await jira.fetch_deployed_cms(deployed_only=True)
            filtered_cms: list[dict] | None = None
            if minor is not None:
                filtered_cms = await jira.fetch_cms_by_version(major, minor)
    except Exception as e:  # noqa: BLE001
        log.error("Next version error: %s", e)
        return JSONResponse({"error": "Failed to calculate next version"}, status_code=500)

    current_minor, minor_versions = derive_minor_versions(recent_cms, major=major)

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

    effective_minor = minor if minor is not None else current_minor
    cms_for_calc = filtered_cms if filtered_cms is not None else recent_cms
    result = calculate_next_version(
        cms_for_calc,
        data.get("bookings", []),
        major=major if minor is not None else None,
        minor=minor,
    )

    response: dict[str, Any] = {
        "major": major,
        "minor": effective_minor,
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
async def book(request: Request, payload: dict = Body(default_factory=dict)) -> Any:
    settings = get_settings()

    version = payload.get("version")
    components = payload.get("components")
    client_envs = payload.get("clientEnvironments")
    booked_by = payload.get("bookedBy")

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

    return {"success": True, "booking": new_booking}


# ---------------------------------------------------------------------------
# GET /client-versions
# ---------------------------------------------------------------------------
@router.get("/client-versions")
async def client_versions(request: Request) -> Any:
    try:
        async with _jira(request) as jira:
            cms = await jira.fetch_deployed_cms(deployed_only=True)
    except Exception as e:  # noqa: BLE001
        log.error("Client versions error: %s", e)
        return JSONResponse({"error": "Failed to fetch client versions"}, status_code=500)

    return build_version_matrix(cms)


# ---------------------------------------------------------------------------
# GET /history?minor=&major=
# ---------------------------------------------------------------------------
@router.get("/history")
async def history(
    request: Request,
    minor: int | None = Query(default=None),
    major: int = Query(default=9),
) -> Any:
    settings = get_settings()
    try:
        async with _jira(request) as jira:
            recent_cms = await jira.fetch_deployed_cms(deployed_only=False)
            current_minor, minor_versions = derive_minor_versions(recent_cms, major=major)
            target_minor = minor if minor is not None else current_minor
            version_cms = await jira.fetch_cms_by_version(major, target_minor)
    except Exception as e:  # noqa: BLE001
        log.error("Hotfix history error: %s", e)
        return JSONResponse({"error": "Failed to fetch hotfix history"}, status_code=500)

    try:
        booking_data = load_bookings(settings.bookings_file)
    except MalformedBookingsError:
        return _malformed_response()
    hotfixes = merge_hotfixes(
        version_cms, booking_data.get("bookings", []), major=major, target_minor=target_minor
    )

    return {
        "minorVersions": minor_versions,
        "currentMinor": current_minor,
        "targetMinor": target_minor,
        "hotfixes": hotfixes,
        "jiraBaseUrl": settings.jira_base_url,
    }
