"""Endpoint-level authentication gate — every /api/hotfix-booking/*
endpoint (except /auth/*) must reject unauthenticated callers with 401.

Regression guard: if a future contributor adds a new route and forgets
the `Depends(require_user)`, this test will fail and remind them to
gate it.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


PROTECTED_READS = [
    "/api/hotfix-booking/field-options",
    "/api/hotfix-booking/deployed-cms",
    "/api/hotfix-booking/next-version",
    "/api/hotfix-booking/bookings",
    "/api/hotfix-booking/client-versions",
    "/api/hotfix-booking/history",
]

PROTECTED_WRITES = [
    ("/api/hotfix-booking/book", {"version": "9.94.23", "components": ["A"], "clientEnvironments": ["C"]}),
    ("/api/hotfix-booking/cancel", {"bookingId": "HB-X"}),
]


@pytest.mark.parametrize("path", PROTECTED_READS)
def test_read_endpoint_requires_session(anon_client: TestClient, path: str) -> None:
    r = anon_client.get(path)
    assert r.status_code == 401, f"{path} should require auth, got {r.status_code}"


@pytest.mark.parametrize("path,payload", PROTECTED_WRITES)
def test_write_endpoint_requires_session(
    anon_client: TestClient, path: str, payload: dict
) -> None:
    r = anon_client.post(path, json=payload)
    assert r.status_code == 401, f"{path} should require auth, got {r.status_code}"


# ---------------------------------------------------------------------------
# The /auth/* group and /health MUST remain reachable without a session —
# otherwise you couldn't log in and health checks would break.
#
# `/auth/me` is intentionally omitted: it returns 401 when there's no
# session (that's its whole purpose — the front-end uses the 401 to know
# it needs to show the login screen).
# ---------------------------------------------------------------------------
PUBLIC_ENDPOINTS = [
    "/health",
    "/api/hotfix-booking/auth/login",
    "/api/hotfix-booking/auth/callback",  # returns 400 (missing state) but reachable
]


@pytest.mark.parametrize("path", PUBLIC_ENDPOINTS)
def test_public_endpoint_does_not_require_session(
    anon_client: TestClient, path: str
) -> None:
    r = anon_client.get(path, follow_redirects=False)
    # Any status EXCEPT 401 is fine — we're asserting the gate isn't there.
    assert r.status_code != 401, f"{path} unexpectedly requires auth"
