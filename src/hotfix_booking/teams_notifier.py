"""Teams webhook notifier — backward-compatible bridge to the legacy hotfix chat.

Purpose
-------
Before this app existed, developers announced hotfix bookings by typing in a
Teams group chat. Some people still watch that chat. Until everyone has moved
to the app UI, every successful ``/book`` and ``/cancel`` also POSTs an
Adaptive Card to a Power Automate / Teams Workflow webhook that reposts it
into the chat.

Best-effort semantics
---------------------
- If ``settings.teams_webhook_url`` is empty, every function here is a no-op.
  This is what tests and local dev rely on, and it's the safe default.
- Any HTTP failure (non-2xx, timeout, transport error) is logged at WARNING
  and swallowed. The caller (a booking handler) must never see an exception
  from this module — a Teams outage must not block a hotfix.

Payload shape
-------------
The webhook body is the "message with attachments" envelope Microsoft
documents for Teams incoming webhooks and Workflow "Post adaptive card"
actions::

    {"type": "message",
     "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                      "content": <AdaptiveCard>}]}
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .config import Settings, get_settings

log = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5.0
_ADAPTIVE_CARD_SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"
_ADAPTIVE_CARD_VERSION = "1.5"
_OPEN_BUTTON_TITLE = "Open in Hotfix Booking tool →"
_ET = ZoneInfo("America/New_York")
_INLINE_LIST_CAP = 10  # max items in a comma-joined FactSet value


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def notify_booking_created(
    booking: dict, *, settings: Settings | None = None
) -> None:
    """Post an Adaptive Card announcing a new booking. Best-effort."""
    s = settings or get_settings()
    if not s.teams_webhook_url:
        return
    card = _build_booking_created_card(booking, app_base_url=s.app_base_url)
    _post(s.teams_webhook_url, _envelope(card))


def notify_booking_cancelled(
    cancelled: dict,
    affected: list[dict],
    *,
    active_cm_warning: dict | None = None,
    settings: Settings | None = None,
) -> None:
    """Post an Adaptive Card announcing a cancellation. Best-effort.

    `affected` mirrors the ``/cancel`` endpoint's response shape: a list of
    ``{id, version, bookedBy, bookedByEmail, previousParentVersions,
    newParentVersions}`` records for the direct children that got rebased.
    """
    s = settings or get_settings()
    if not s.teams_webhook_url:
        return
    card = _build_booking_cancelled_card(
        cancelled,
        affected,
        active_cm_warning=active_cm_warning,
        app_base_url=s.app_base_url,
    )
    _post(s.teams_webhook_url, _envelope(card))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _post(url: str, payload: dict) -> None:
    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            resp = client.post(url, json=payload)
        if resp.status_code >= 400:
            log.warning(
                "Teams webhook returned %s (non-fatal): %s",
                resp.status_code,
                (resp.text or "")[:500],
            )
    except Exception as e:  # noqa: BLE001 — deliberate: never surface to caller
        log.warning("Teams webhook post failed (non-fatal): %s", e)


def _envelope(card: dict) -> dict:
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": card,
            }
        ],
    }


# ---------------------------------------------------------------------------
# Card builders
# ---------------------------------------------------------------------------
def _base_card(body: list[dict], *, app_base_url: str) -> dict:
    card: dict[str, Any] = {
        "type": "AdaptiveCard",
        "$schema": _ADAPTIVE_CARD_SCHEMA,
        "version": _ADAPTIVE_CARD_VERSION,
        "body": body,
    }
    if app_base_url:
        card["actions"] = [
            {
                "type": "Action.OpenUrl",
                "title": _OPEN_BUTTON_TITLE,
                "url": app_base_url,
            }
        ]
    return card


def _fact(title: str, value: str) -> dict:
    return {"title": title, "value": value}


def _join_truncated(values: list[str] | None, max_items: int = _INLINE_LIST_CAP) -> str:
    """Comma-join with an explicit '(+N more)' overflow indicator so a booking
    with 40 clients doesn't produce a wall of text in the chat."""
    if not values:
        return "—"
    if len(values) <= max_items:
        return ", ".join(values)
    shown = ", ".join(values[:max_items])
    return f"{shown} (+{len(values) - max_items} more)"


def _format_et(iso_ts: str | None) -> str:
    """Convert a UTC ISO 8601 timestamp to `MM/DD/YYYY HH:MM AM/PM ET`.

    Matches the rest of the app's UI convention (all user-facing times shown
    in Eastern Time with an ' ET' suffix). Falls back to the raw string on
    any parse failure so a bad timestamp can never break the notifier.
    """
    if not iso_ts:
        return ""
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return dt.astimezone(_ET).strftime("%m/%d/%Y %I:%M %p ET")
    except (ValueError, TypeError):
        return iso_ts


def _build_booking_created_card(booking: dict, *, app_base_url: str) -> dict:
    version = booking.get("version", "?")
    booked_by = booking.get("bookedBy") or "Unknown"
    booked_at = _format_et(booking.get("bookedAt"))
    description = (booking.get("description") or "").strip()

    facts = [
        _fact("Booked by", booked_by),
        _fact("Clients", _join_truncated(booking.get("clientEnvironments"))),
        _fact("Components", _join_truncated(booking.get("components"))),
    ]
    if description:
        facts.append(_fact("Description", description))
    if booked_at:
        facts.append(_fact("Booked at", booked_at))

    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": f"🚀 Hotfix booked: {version}",
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
        },
        {"type": "FactSet", "facts": facts},
    ]
    return _base_card(body, app_base_url=app_base_url)


def _build_booking_cancelled_card(
    cancelled: dict,
    affected: list[dict],
    *,
    active_cm_warning: dict | None,
    app_base_url: str,
) -> dict:
    version = cancelled.get("version", "?")
    booked_by = cancelled.get("bookedBy") or "Unknown"
    cancelled_by = cancelled.get("cancelledBy") or ""
    cancelled_at = _format_et(cancelled.get("cancelledAt"))

    # `cancelledBy` is only surfaced when it differs from the original booker
    # (admin / CM-reporter cancel). In the common self-cancel case we omit it
    # to keep the card compact. Case-insensitive + whitespace-trimmed so
    # trivial rendering differences don't spuriously add the row.
    facts = [
        _fact("Originally booked by", booked_by),
    ]
    if cancelled_by and cancelled_by.strip().lower() != booked_by.strip().lower():
        facts.append(_fact("Cancelled by", cancelled_by))
    if cancelled_at:
        facts.append(_fact("Cancelled at", cancelled_at))

    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": f"❌ Hotfix cancelled: {version}",
            "weight": "Bolder",
            "size": "Medium",
            "color": "Attention",
            "wrap": True,
        },
        {"type": "FactSet", "facts": facts},
    ]

    if affected:
        body.append(
            {
                "type": "TextBlock",
                "text": f"Downstream bookings that need rebasing ({len(affected)}):",
                "weight": "Bolder",
                "spacing": "Medium",
                "wrap": True,
            }
        )
        for child in affected:
            child_version = child.get("version", "?")
            child_booker = child.get("bookedBy") or "Unknown"
            new_parents = child.get("newParentVersions") or []
            new_basis = ", ".join(new_parents) if new_parents else "baseline"
            body.append(
                {
                    "type": "TextBlock",
                    "text": (
                        f"• **{child_version}** ({child_booker}) — "
                        f"rebase on {new_basis}"
                    ),
                    "wrap": True,
                    "spacing": "Small",
                }
            )

    if active_cm_warning:
        cm_key = active_cm_warning.get("cmKey") or "?"
        cm_status = active_cm_warning.get("status") or "?"
        body.append(
            {
                "type": "TextBlock",
                "text": (
                    f"⚠️ Active CM in Jira: **{cm_key}** ({cm_status}). "
                    "You may need to reject it manually."
                ),
                "color": "Warning",
                "wrap": True,
                "spacing": "Medium",
            }
        )

    return _base_card(body, app_base_url=app_base_url)
