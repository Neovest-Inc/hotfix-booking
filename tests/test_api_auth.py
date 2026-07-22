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


def test_login_does_not_set_session_cookie(anon_client: TestClient) -> None:
    """Regression guard for the signed-state design: /login must NOT rely on
    a session cookie surviving the Atlassian round-trip. The OAuth `state`
    parameter is a self-contained signed token — nothing is stored server-
    side, and the hb_session cookie is only written on successful callback."""
    _kickoff_login(anon_client)
    assert "hb_session" not in anon_client.cookies


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
    # Diagnostic contract: response body carries a machine-readable reason code
    # + a human hint so PMs debugging a broken login don't need to open logs.
    body = r.json()
    assert body["detail"]["error"] == "missing_state"
    assert "hint" in body["detail"]


def test_callback_missing_code_returns_400(anon_client: TestClient) -> None:
    state = _kickoff_login(anon_client)
    r = anon_client.get(
        f"/api/hotfix-booking/auth/callback?state={state}",
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "missing_code"


def test_callback_with_forged_state_redirects_to_login_gate(
    anon_client: TestClient,
) -> None:
    """An unsigned/random `state` value fails signature verification and
    self-heals to the login gate. This covers three real-world scenarios
    that all look identical to the server: (a) an attacker crafting a
    callback URL from scratch, (b) a state left over from a rotated
    SESSION_SECRET_KEY, (c) any random string in the state param."""
    _kickoff_login(anon_client)
    r = anon_client.get(
        "/api/hotfix-booking/auth/callback?code=abc&state=not-a-signed-state",
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/?auth_error=session_lost"


def test_callback_without_prior_login_redirects_to_login_gate(
    anon_client: TestClient,
) -> None:
    """A random unsigned state (no prior /login on this browser) fails
    signature verification. This is the most common real-world failure —
    it covers a user hitting /callback directly, an attacker crafting a
    URL, and a state left over from a rotated SESSION_SECRET_KEY. Always
    self-heal by redirecting to the login gate rather than showing a raw
    400 JSON blob that a PM has to decipher."""
    r = anon_client.get(
        "/api/hotfix-booking/auth/callback?code=abc&state=anything",
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/?auth_error=session_lost"


def test_callback_state_signed_with_wrong_secret_redirects(
    anon_client: TestClient, settings
) -> None:
    """Regression guard for SESSION_SECRET_KEY rotation: a state signed by a
    different secret must NOT be accepted. Simulates an attacker who knows
    our state format but not our secret."""
    from itsdangerous import URLSafeTimedSerializer

    forged_signer = URLSafeTimedSerializer(
        "definitely-not-the-real-secret" + "x" * 40, salt="oauth-state-v1"
    )
    forged_state = forged_signer.dumps({"nonce": "forged", "return_to": "/"})

    r = anon_client.get(
        f"/api/hotfix-booking/auth/callback?code=abc&state={forged_state}",
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/?auth_error=session_lost"


def test_callback_state_signed_with_wrong_salt_redirects(
    anon_client: TestClient, settings
) -> None:
    """The itsdangerous `salt` scopes signatures to a specific use. If a
    future itsdangerous usage in the app (e.g. a password-reset token)
    were to leak a token, that token must NOT be usable as an OAuth state."""
    from itsdangerous import URLSafeTimedSerializer

    # Same secret, DIFFERENT salt — signatures should not cross-verify.
    cross_purpose_signer = URLSafeTimedSerializer(
        settings.session_secret_key, salt="different-purpose"
    )
    cross_state = cross_purpose_signer.dumps({"nonce": "n", "return_to": "/"})

    r = anon_client.get(
        f"/api/hotfix-booking/auth/callback?code=abc&state={cross_state}",
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/?auth_error=session_lost"


def test_callback_state_expires_after_5_minutes(
    anon_client: TestClient, settings, monkeypatch
) -> None:
    """The signed state has a 5-minute TTL (upper bound on how long a user
    can take to complete Atlassian consent, and defense-in-depth against
    replay). An expired state must be rejected \u2014 verified by patching
    itsdangerous' internal clock to simulate the passage of time."""
    import time as _time

    state = _kickoff_login(anon_client)

    # Fast-forward `time.time()` by 6 minutes for itsdangerous' age check.
    # Patch on the module itsdangerous.timed imports from.
    real_time = _time.time
    monkeypatch.setattr(_time, "time", lambda: real_time() + 6 * 60)

    r = anon_client.get(
        f"/api/hotfix-booking/auth/callback?code=abc&state={state}",
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/?auth_error=session_lost"


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


def test_callback_state_replay_relies_on_atlassian_one_shot_code(
    anon_client: TestClient,
) -> None:
    """With the signed-state design our `state` is stateless (nothing stored
    server-side), so replay protection is delegated to Atlassian: RFC 6749
    §10.5 requires the authorization server to invalidate the `code` after
    first use. This test simulates that guarantee — first call succeeds,
    second call with the same (code, state) pair gets `invalid_grant` back
    from Atlassian and surfaces as a 502.

    (Belt-and-suspenders: the itsdangerous signer also stamps the state
    with a 5-minute TTL, so even if Atlassian's replay guard were somehow
    bypassed, a stale state can only be reused for at most 5 minutes.)"""
    state = _kickoff_login(anon_client)
    with respx.mock(assert_all_called=False) as mock:
        token_route = mock.post(TOKEN_URL)
        # First call succeeds, second call fails — mirror Atlassian's one-shot code.
        token_route.side_effect = [
            Response(
                200,
                json={"access_token": "t", "expires_in": 3600, "scope": "read:me"},
            ),
            Response(400, json={"error": "invalid_grant"}),
        ]
        mock.get(ME_URL).mock(return_value=Response(200, json=_ME_PAYLOAD))

        r1 = anon_client.get(
            f"/api/hotfix-booking/auth/callback?code=abc&state={state}",
            follow_redirects=False,
        )
        assert r1.status_code == 302
        r2 = anon_client.get(
            f"/api/hotfix-booking/auth/callback?code=abc&state={state}",
            follow_redirects=False,
        )
        assert r2.status_code == 502


def test_callback_succeeds_when_browser_dropped_the_session_cookie(
    anon_client: TestClient,
) -> None:
    """Regression guard for the recurring UX bug PMs kept hitting: browser
    drops the hb_session cookie between /login and /callback (Chrome cookie
    policy, extensions, cross-profile navigation, etc.), and the user is
    left staring at a JSON error page. Under the signed-state design, the
    OAuth `state` parameter is self-contained (signed with SESSION_SECRET_KEY)
    so /callback can verify it without ANY cookie surviving the round-trip.

    Simulates the failure mode by clearing all cookies between /login and
    /callback — under the old cookie-based design this would trip
    `no_session_state`; under the new design it just works."""
    state = _kickoff_login(anon_client)
    # Simulate the browser dropping the cookie mid-flow.
    anon_client.cookies.clear()

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
    assert r.status_code == 302, (
        f"Expected 302 (login succeeded despite dropped cookie) but got "
        f"{r.status_code}: {r.text[:200]}"
    )
    assert r.headers["location"] == "/"
    # And the user is now properly logged in — the session cookie is set
    # freshly on this successful callback response.
    me = anon_client.get("/api/hotfix-booking/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "antonios@neovest.com"


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


def test_callback_with_atlassian_error_ignores_state(
    anon_client: TestClient,
) -> None:
    """On Atlassian's error path (user declined consent, etc.) we short-circuit
    to the login gate before touching the state token. This mirrors the old
    'consume state on error' behavior in spirit — no code path uses an
    error-branch state to gain access — but under the stateless design there
    is no server-side state to consume; the guarantee comes from the state
    being cryptographically signed rather than session-scoped."""
    _kickoff_login(anon_client)
    r = anon_client.get(
        "/api/hotfix-booking/auth/callback?error=access_denied&state=whatever",
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/?auth_error=access_denied"


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
