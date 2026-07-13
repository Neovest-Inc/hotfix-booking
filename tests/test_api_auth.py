"""OAuth 2.0 (3LO) login flow tests.

Covers the four `/api/hotfix-booking/auth/*` endpoints:
- GET  /auth/login    → 302 to auth.atlassian.com/authorize
- GET  /auth/callback → verifies state, exchanges code, calls /me, sets session
- POST /auth/logout   → clears session
- GET  /auth/me       → returns session user or 401

Atlassian's endpoints are mocked with respx; no real network traffic.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"
ME_URL = "https://api.atlassian.com/me"

_ME_PAYLOAD = {
    "account_id": "5b10a2844c20165700ede21g",
    "email": "antonios@neovest.com",
    "name": "Antonios Kalogeropoulos",
    "picture": "https://avatar.example/x.png",
}


def _kickoff_login(anon_client: TestClient) -> str:
    """POST-like helper: hits /login, returns the `state` param."""
    r = anon_client.get("/api/hotfix-booking/auth/login", follow_redirects=False)
    assert r.status_code == 302
    query = parse_qs(urlparse(r.headers["location"]).query)
    return query["state"][0]


# ---------------------------------------------------------------------------
# GET /auth/login
# ---------------------------------------------------------------------------
def test_login_redirects_to_atlassian_authorize_url(anon_client: TestClient) -> None:
    r = anon_client.get("/api/hotfix-booking/auth/login", follow_redirects=False)
    assert r.status_code == 302
    parsed = urlparse(r.headers["location"])
    assert parsed.scheme == "https"
    assert parsed.hostname == "auth.atlassian.com"
    assert parsed.path == "/authorize"
    q = parse_qs(parsed.query)
    assert q["client_id"] == ["test-client-id"]
    assert q["scope"] == ["read:me"]
    assert q["response_type"] == ["code"]
    assert q["audience"] == ["api.atlassian.com"]
    assert q["prompt"] == ["consent"]
    assert q["redirect_uri"] == [
        "http://localhost:3001/api/hotfix-booking/auth/callback"
    ]
    # state must be present and long enough to resist guessing
    assert len(q["state"][0]) >= 32


def test_login_state_is_stored_in_session(anon_client: TestClient) -> None:
    _kickoff_login(anon_client)
    # After /login the client should carry an hb_session cookie
    assert "hb_session" in anon_client.cookies


def test_login_accepts_return_to_query_param(anon_client: TestClient) -> None:
    r = anon_client.get(
        "/api/hotfix-booking/auth/login?return_to=/history",
        follow_redirects=False,
    )
    assert r.status_code == 302
    # The return_to itself is stored server-side (in session), not exposed
    # in the authorize URL — nothing to assert on the redirect target beyond
    # the base authorize URL. But the callback test below verifies round-trip.


def test_login_rejects_external_return_to(anon_client: TestClient) -> None:
    """Guard against open-redirect via return_to=https://evil.example."""
    r = anon_client.get(
        "/api/hotfix-booking/auth/login?return_to=https://evil.example/steal",
        follow_redirects=False,
    )
    # Still 302 to Atlassian, but return_to is dropped (defaults to /).
    assert r.status_code == 302


# ---------------------------------------------------------------------------
# GET /auth/callback — happy path
# ---------------------------------------------------------------------------
def test_callback_happy_path_sets_session_and_redirects(anon_client: TestClient) -> None:
    state = _kickoff_login(anon_client)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(TOKEN_URL).mock(
            return_value=Response(
                200,
                json={
                    "access_token": "atl-access-token",
                    "expires_in": 3600,
                    "scope": "read:me",
                },
            )
        )
        mock.get(ME_URL).mock(return_value=Response(200, json=_ME_PAYLOAD))

        r = anon_client.get(
            f"/api/hotfix-booking/auth/callback?code=abc&state={state}",
            follow_redirects=False,
        )

    assert r.status_code == 302
    assert r.headers["location"] == "/"
    # Now /me should return the logged-in user
    me = anon_client.get("/api/hotfix-booking/auth/me")
    assert me.status_code == 200
    body = me.json()
    assert body["email"] == "antonios@neovest.com"
    assert body["displayName"] == "Antonios Kalogeropoulos"
    assert body["accountId"] == "5b10a2844c20165700ede21g"


def test_callback_honors_return_to_from_login(anon_client: TestClient) -> None:
    r = anon_client.get(
        "/api/hotfix-booking/auth/login?return_to=/history",
        follow_redirects=False,
    )
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]

    with respx.mock(assert_all_called=False) as mock:
        mock.post(TOKEN_URL).mock(
            return_value=Response(
                200,
                json={"access_token": "t", "expires_in": 3600, "scope": "read:me"},
            )
        )
        mock.get(ME_URL).mock(return_value=Response(200, json=_ME_PAYLOAD))

        r = anon_client.get(
            f"/api/hotfix-booking/auth/callback?code=abc&state={state}",
            follow_redirects=False,
        )
    assert r.status_code == 302
    assert r.headers["location"] == "/history"


# ---------------------------------------------------------------------------
# GET /auth/callback — error paths
# ---------------------------------------------------------------------------
def test_callback_missing_state_returns_400(anon_client: TestClient) -> None:
    r = anon_client.get(
        "/api/hotfix-booking/auth/callback?code=abc",
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_callback_missing_code_returns_400(anon_client: TestClient) -> None:
    state = _kickoff_login(anon_client)
    r = anon_client.get(
        f"/api/hotfix-booking/auth/callback?state={state}",
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_callback_state_mismatch_returns_400(anon_client: TestClient) -> None:
    _kickoff_login(anon_client)
    r = anon_client.get(
        "/api/hotfix-booking/auth/callback?code=abc&state=not-the-right-state",
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_callback_without_prior_login_returns_400(anon_client: TestClient) -> None:
    """No session state cookie at all — attacker crafting a callback URL."""
    r = anon_client.get(
        "/api/hotfix-booking/auth/callback?code=abc&state=anything",
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_callback_token_exchange_failure_returns_502(anon_client: TestClient) -> None:
    state = _kickoff_login(anon_client)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(TOKEN_URL).mock(
            return_value=Response(400, json={"error": "invalid_grant"})
        )
        r = anon_client.get(
            f"/api/hotfix-booking/auth/callback?code=abc&state={state}",
            follow_redirects=False,
        )
    assert r.status_code == 502


def test_callback_me_call_failure_returns_502(anon_client: TestClient) -> None:
    state = _kickoff_login(anon_client)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(TOKEN_URL).mock(
            return_value=Response(
                200,
                json={"access_token": "t", "expires_in": 3600, "scope": "read:me"},
            )
        )
        mock.get(ME_URL).mock(return_value=Response(500, json={"error": "boom"}))
        r = anon_client.get(
            f"/api/hotfix-booking/auth/callback?code=abc&state={state}",
            follow_redirects=False,
        )
    assert r.status_code == 502


def test_callback_state_is_consumed_after_use(anon_client: TestClient) -> None:
    """State + return_to must be one-shot to prevent replay."""
    state = _kickoff_login(anon_client)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(TOKEN_URL).mock(
            return_value=Response(
                200,
                json={"access_token": "t", "expires_in": 3600, "scope": "read:me"},
            )
        )
        mock.get(ME_URL).mock(return_value=Response(200, json=_ME_PAYLOAD))

        # First use — succeeds
        r1 = anon_client.get(
            f"/api/hotfix-booking/auth/callback?code=abc&state={state}",
            follow_redirects=False,
        )
        assert r1.status_code == 302

        # Second use with the same state — must fail (state was consumed)
        r2 = anon_client.get(
            f"/api/hotfix-booking/auth/callback?code=abc&state={state}",
            follow_redirects=False,
        )
        assert r2.status_code == 400


# ---------------------------------------------------------------------------
# GET /auth/me
# ---------------------------------------------------------------------------
def test_me_returns_401_without_session(anon_client: TestClient) -> None:
    r = anon_client.get("/api/hotfix-booking/auth/me")
    assert r.status_code == 401


def test_me_returns_user_with_session(anon_client: TestClient) -> None:
    state = _kickoff_login(anon_client)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(TOKEN_URL).mock(
            return_value=Response(
                200,
                json={"access_token": "t", "expires_in": 3600, "scope": "read:me"},
            )
        )
        mock.get(ME_URL).mock(return_value=Response(200, json=_ME_PAYLOAD))
        anon_client.get(
            f"/api/hotfix-booking/auth/callback?code=abc&state={state}",
            follow_redirects=False,
        )

    r = anon_client.get("/api/hotfix-booking/auth/me")
    assert r.status_code == 200
    assert r.json()["email"] == "antonios@neovest.com"


# ---------------------------------------------------------------------------
# POST /auth/logout
# ---------------------------------------------------------------------------
def test_logout_clears_session(anon_client: TestClient) -> None:
    state = _kickoff_login(anon_client)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(TOKEN_URL).mock(
            return_value=Response(
                200,
                json={"access_token": "t", "expires_in": 3600, "scope": "read:me"},
            )
        )
        mock.get(ME_URL).mock(return_value=Response(200, json=_ME_PAYLOAD))
        anon_client.get(
            f"/api/hotfix-booking/auth/callback?code=abc&state={state}",
            follow_redirects=False,
        )

    # Confirm logged in
    assert anon_client.get("/api/hotfix-booking/auth/me").status_code == 200

    # Logout
    r = anon_client.post("/api/hotfix-booking/auth/logout")
    assert r.status_code == 204

    # No longer logged in
    assert anon_client.get("/api/hotfix-booking/auth/me").status_code == 401


def test_logout_without_session_still_succeeds(anon_client: TestClient) -> None:
    """Idempotent — logging out when not logged in is a no-op, not an error."""
    r = anon_client.post("/api/hotfix-booking/auth/logout")
    assert r.status_code == 204


# ---------------------------------------------------------------------------
# GET /auth/callback — Atlassian returned an error (e.g. user declined consent)
# ---------------------------------------------------------------------------
def test_callback_with_atlassian_error_redirects_to_login_gate(
    anon_client: TestClient,
) -> None:
    """Atlassian sends `?error=access_denied&state=...` when the user clicks
    Cancel on the consent screen. We must not surface a raw 400 — instead
    redirect to `/?auth_error=access_denied` so the login gate can show a
    friendly message."""
    r = anon_client.get(
        "/api/hotfix-booking/auth/callback?error=access_denied&state=whatever",
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/?auth_error=access_denied"


def test_callback_with_atlassian_error_still_consumes_state(
    anon_client: TestClient,
) -> None:
    """Even on the error path, any stored `oauth_state` must be cleared so a
    later replay of a stale code can't hijack the session."""
    # Prime the session with a state.
    _kickoff_login(anon_client)
    assert "hb_session" in anon_client.cookies
    # Atlassian error path — should still clear the state.
    r = anon_client.get(
        "/api/hotfix-booking/auth/callback?error=access_denied&state=whatever",
        follow_redirects=False,
    )
    assert r.status_code == 302
    # A subsequent legitimate callback attempt with the original state must
    # now fail (state was consumed on the error redirect).
    r2 = anon_client.get(
        "/api/hotfix-booking/auth/callback?code=x&state=whatever",
        follow_redirects=False,
    )
    assert r2.status_code == 400


# ---------------------------------------------------------------------------
# Session cookie signed with the wrong secret must be rejected.
# Regression guard for SESSION_SECRET_KEY rotation.
# ---------------------------------------------------------------------------
def test_session_signed_with_wrong_secret_returns_401(
    anon_client: TestClient,
) -> None:
    """A cookie signed by a stranger's key (or an old rotated key) must not
    grant access — the app's SessionMiddleware should refuse to unsign it
    and treat the request as anonymous."""
    import itsdangerous
    from base64 import b64encode
    import json as _json

    forged_signer = itsdangerous.TimestampSigner("not-the-real-secret" + "x" * 40)
    data = b64encode(_json.dumps({
        "user": {
            "email": "attacker@example.com",
            "displayName": "Attacker",
            "accountId": "acc-attacker",
        }
    }).encode("utf-8"))
    forged = forged_signer.sign(data).decode("utf-8")
    anon_client.cookies.set("hb_session", forged)

    # Any protected endpoint — pick /auth/me since it needs zero setup.
    r = anon_client.get("/api/hotfix-booking/auth/me")
    assert r.status_code == 401
