"""OAuth 2.0 (3LO) login flow for Atlassian.

Standard authorization-code grant, identity-only:

1. GET /login           — 302s to auth.atlassian.com/authorize with a
                          server-signed `state` token (itsdangerous, bound
                          to SESSION_SECRET_KEY, 5-min TTL) that carries the
                          nonce + return_to. Nothing is stored server-side
                          and no cookie is written — the whole flow is
                          cookie-independent so it survives any browser
                          policy or extension that drops cookies on the
                          Atlassian → localhost redirect.
2. GET /callback        — verifies the signed state, exchanges the code at
                          auth.atlassian.com/oauth/token for an access
                          token, then calls api.atlassian.com/me to get
                          {email, name, account_id}. Stores it in the
                          signed session cookie and drops the access
                          token — we never call Atlassian as the user
                          again.
3. GET /me              — returns the current session's user or 401.
4. POST /logout         — clears the session.

Why signed-state instead of session-cookie state? The previous design
stored `oauth_state` in the session cookie and popped it on /callback. In
practice, PM users kept hitting `no_session_state` errors after coming
back to the app days later — the hb_session cookie set by /login didn't
survive the Atlassian round-trip (browser policy quirks, stale duplicate
cookies, cross-profile navigation, etc.). Signed state (as recommended by
OAuth 2.0 RFC 6749 §10.12 for CSRF protection) makes the entire OAuth
flow stateless from the server's perspective. Replay protection comes
from two independent mechanisms: the itsdangerous 5-minute timestamp AND
Atlassian invalidating the `code` after first use (RFC 6749 §10.5).

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
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import get_settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hotfix-booking/auth")

_AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
_TOKEN_URL = "https://auth.atlassian.com/oauth/token"
_ME_URL = "https://api.atlassian.com/me"
_HTTP_TIMEOUT = 10.0

# The OAuth `state` token expires after this many seconds. Users have to
# complete the Atlassian consent flow within the window — 5 minutes is
# generous for the click-Log-in-and-approve interaction. Also acts as an
# upper bound on replay attempts (Atlassian's one-shot code is the primary
# replay guard).
_STATE_MAX_AGE_SECONDS = 300
# `salt` scopes this signer's signatures so a token minted for OAuth state
# can't be reused as a token for any future itsdangerous usage in the app.
_STATE_SALT = "oauth-state-v1"


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


def _state_serializer() -> URLSafeTimedSerializer:
    """Signer for the OAuth `state` parameter.

    Bound to `SESSION_SECRET_KEY`, so states signed by this process can only
    be verified by a process holding the same secret. Rotating the secret
    invalidates any in-flight logins — harmless, the user just retries.

    Called per-request rather than cached at import time so tests that
    override `SESSION_SECRET_KEY` via `reset_settings_for_tests` see the
    new secret. In prod this is a single-digit-microsecond object build.
    """
    return URLSafeTimedSerializer(
        get_settings().session_secret_key, salt=_STATE_SALT
    )


# ---------------------------------------------------------------------------
# GET /login
# ---------------------------------------------------------------------------
@router.get("/login")
async def login(return_to: str = Query(default="/")) -> RedirectResponse:
    settings = get_settings()
    # Pack the nonce + return_to into a signed, time-bounded token that
    # Atlassian will echo back on /callback. Because the token is signed
    # with our own SESSION_SECRET_KEY, no server-side storage — and no
    # session cookie — is needed to verify it later. This is the OAuth
    # 2.0 spec-recommended way to do stateless CSRF protection (RFC 6749
    # §10.12) and it makes the flow immune to browsers dropping the
    # hb_session cookie on the Atlassian → localhost redirect.
    state = _state_serializer().dumps(
        {
            "nonce": secrets.token_urlsafe(16),
            "return_to": _safe_return_to(return_to),
        }
    )

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

    # User rejected the Atlassian consent screen (or Atlassian returned an
    # error). Redirect back to the login gate rather than raising a raw 400 —
    # `showLoginGate()` on the front-end reads `?auth_error=...` and displays
    # a friendly message.
    if error:
        log.info("Atlassian OAuth returned error=%s (%s)", error, error_description)
        return RedirectResponse(url=f"/?auth_error={error}", status_code=302)

    # `missing_code` and `missing_state` are Atlassian / OAuth-app config
    # bugs, not something the end user can recover by retrying — keep them
    # as 400 so misconfigurations surface loudly in monitoring.
    if not code:
        log.warning("OAuth callback rejected: missing_code")
        raise HTTPException(
            status_code=400,
            detail={
                "error": "missing_code",
                "hint": (
                    "Atlassian did not include a 'code' query param on the "
                    "callback. This usually means the OAuth app in the "
                    "Atlassian developer console is misconfigured."
                ),
            },
        )
    if not state:
        log.warning("OAuth callback rejected: missing_state")
        raise HTTPException(
            status_code=400,
            detail={
                "error": "missing_state",
                "hint": (
                    "Atlassian did not echo the 'state' query param back. "
                    "Very unusual — retry once; if it persists, check the "
                    "Atlassian OAuth app settings."
                ),
            },
        )

    # Verify the state token. Failure modes:
    # - `SignatureExpired`: user took >5 min to complete Atlassian consent,
    #   or resurfaced an old tab. Recoverable — restart the flow.
    # - `BadSignature`: state was tampered with, signed by a rotated secret,
    #   or (most likely) is simply a random string an attacker sent to
    #   `/callback` directly. Also recoverable via a fresh /login.
    # In either case we redirect to the login gate rather than raising a
    # scary JSON error — this is the same self-heal UX PMs kept hitting
    # under the old cookie-based design.
    try:
        payload = _state_serializer().loads(
            state, max_age=_STATE_MAX_AGE_SECONDS
        )
    except SignatureExpired:
        log.info("OAuth callback rejected: state token expired")
        return RedirectResponse(url="/?auth_error=session_lost", status_code=302)
    except BadSignature:
        log.warning(
            "OAuth callback rejected: bad state signature "
            "(state was not issued by this server or was tampered with)"
        )
        return RedirectResponse(url="/?auth_error=session_lost", status_code=302)

    # `payload` is trusted (signature verified) — safe to read return_to.
    # Belt-and-suspenders `_safe_return_to` in case a future signer bug
    # lets an unsafe value slip through.
    return_to = _safe_return_to(payload.get("return_to"))

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
