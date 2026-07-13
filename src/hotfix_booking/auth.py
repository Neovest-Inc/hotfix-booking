"""OAuth 2.0 (3LO) login flow for Atlassian.

Standard authorization-code grant, identity-only:

1. GET /login           — 302s to auth.atlassian.com/authorize with a
                          fresh, per-session random `state`.
2. GET /callback        — verifies state, exchanges the code at
                          auth.atlassian.com/oauth/token for an access
                          token, then calls api.atlassian.com/me to get
                          {email, name, account_id}. Stores it in the
                          signed session cookie and drops the access
                          token — we never call Atlassian as the user
                          again.
3. GET /me              — returns the current session's user or 401.
4. POST /logout         — clears the session.

Nothing about this module touches Jira. Jira API access continues to
use the shared JIRA_API_TOKEN. OAuth here is purely for identity.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse

from .config import get_settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hotfix-booking/auth")

_AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
_TOKEN_URL = "https://auth.atlassian.com/oauth/token"
_ME_URL = "https://api.atlassian.com/me"
_HTTP_TIMEOUT = 10.0


@dataclass(frozen=True)
class UserContext:
    """Identity resolved from the session cookie."""
    email: str
    displayName: str
    accountId: str


def require_user(request: Request) -> UserContext:
    """FastAPI dependency — returns the logged-in user or raises 401.

    Attach to any endpoint that must be authenticated:
        @router.get("/thing")
        def thing(_user: UserContext = Depends(require_user)) -> ...:
            ...

    Use the underscore prefix (`_user`) when you don't actually need the
    identity — just the gate.
    """
    user = request.session.get("user")
    if not user or not user.get("email") or not user.get("displayName"):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return UserContext(
        email=user["email"],
        displayName=user["displayName"],
        accountId=user.get("accountId", ""),
    )


def _redirect_uri() -> str:
    """Callback URL must exactly match what's registered in the Atlassian
    developer console — Atlassian does a byte-for-byte comparison."""
    base = get_settings().app_base_url.rstrip("/")
    return f"{base}/api/hotfix-booking/auth/callback"


def _safe_return_to(candidate: str | None) -> str:
    """Only allow same-site relative paths. Prevents open-redirect via
    `?return_to=https://evil.example`. `//foo` is a protocol-relative URL
    and would let an attacker redirect to another host — reject it too."""
    if not candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    return candidate


# ---------------------------------------------------------------------------
# GET /login
# ---------------------------------------------------------------------------
@router.get("/login")
async def login(
    request: Request, return_to: str = Query(default="/")
) -> RedirectResponse:
    settings = get_settings()
    state = secrets.token_urlsafe(32)
    # Stored in the signed session cookie — the browser can't tamper with it.
    # `pop` in /callback makes this one-shot (no replay).
    request.session["oauth_state"] = state
    request.session["oauth_return_to"] = _safe_return_to(return_to)

    params = {
        "audience": "api.atlassian.com",
        "client_id": settings.atlassian_client_id,
        "scope": "read:me",
        "redirect_uri": _redirect_uri(),
        "state": state,
        "response_type": "code",
        "prompt": "consent",
    }
    return RedirectResponse(url=f"{_AUTHORIZE_URL}?{urlencode(params)}", status_code=302)


# ---------------------------------------------------------------------------
# GET /callback
# ---------------------------------------------------------------------------
@router.get("/callback")
async def callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
) -> RedirectResponse:
    settings = get_settings()
    # Pop both up-front so the state is consumed even on error paths — no replay.
    expected_state = request.session.pop("oauth_state", None)
    return_to = request.session.pop("oauth_return_to", "/") or "/"

    # User rejected the Atlassian consent screen (or Atlassian returned an
    # error). Redirect back to the login gate rather than raising a raw 400 —
    # `showLoginGate()` on the front-end reads `?auth_error=...` and displays
    # a friendly message.
    if error:
        log.info("Atlassian OAuth returned error=%s (%s)", error, error_description)
        return RedirectResponse(url=f"/?auth_error={error}", status_code=302)

    if not code or not state or not expected_state or not secrets.compare_digest(
        state, expected_state
    ):
        raise HTTPException(status_code=400, detail="Invalid OAuth callback")

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as http:
            token_resp = await http.post(
                _TOKEN_URL,
                json={
                    "grant_type": "authorization_code",
                    "client_id": settings.atlassian_client_id,
                    "client_secret": settings.atlassian_client_secret,
                    "code": code,
                    "redirect_uri": _redirect_uri(),
                },
                headers={"Content-Type": "application/json"},
            )
            if token_resp.status_code != 200:
                log.warning(
                    "Atlassian token exchange failed: %s %s",
                    token_resp.status_code,
                    token_resp.text[:200],
                )
                raise HTTPException(
                    status_code=502, detail="Atlassian token exchange failed"
                )
            access_token = token_resp.json().get("access_token")
            if not access_token:
                raise HTTPException(
                    status_code=502, detail="Atlassian returned no access_token"
                )

            me_resp = await http.get(
                _ME_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
            )
            if me_resp.status_code != 200:
                log.warning(
                    "Atlassian /me failed: %s %s",
                    me_resp.status_code,
                    me_resp.text[:200],
                )
                raise HTTPException(status_code=502, detail="Atlassian /me failed")
            me = me_resp.json()
    except httpx.HTTPError as e:
        log.warning("Atlassian OAuth network error: %s", e)
        raise HTTPException(
            status_code=502, detail="Atlassian OAuth network error"
        ) from e

    email = me.get("email")
    account_id = me.get("account_id")
    if not email or not account_id:
        raise HTTPException(
            status_code=502, detail="Atlassian profile missing email/account_id"
        )
    display_name = me.get("name") or email

    request.session["user"] = {
        "email": email,
        "displayName": display_name,
        "accountId": account_id,
    }
    return RedirectResponse(url=return_to, status_code=302)


# ---------------------------------------------------------------------------
# GET /me
# ---------------------------------------------------------------------------
@router.get("/me")
async def me(request: Request) -> dict:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


# ---------------------------------------------------------------------------
# POST /logout
# ---------------------------------------------------------------------------
@router.post("/logout")
async def logout(request: Request) -> Response:
    request.session.clear()
    return Response(status_code=204)
