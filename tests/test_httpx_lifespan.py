"""Tests for the app-lifetime persistent httpx client.

The app opens one shared httpx.AsyncClient in its lifespan and every
Jira request reuses it — saves the per-request TLS handshake (~50-100ms
per call). These tests lock in the lifecycle:

- Client exists on `app.state.httpx_client` while the app is running.
- Client is a real httpx.AsyncClient bound to the configured Jira base URL.
- Client is closed on shutdown (state cleared to None).
"""
from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from hotfix_booking.app import create_app
from hotfix_booking.config import Settings


def test_lifespan_creates_and_stores_httpx_client(settings: Settings) -> None:
    """While the TestClient context is open the shared client is present
    on app.state and configured against the Jira base URL from settings."""
    app = create_app()
    with TestClient(app) as _:
        client = app.state.httpx_client
        assert client is not None
        assert isinstance(client, httpx.AsyncClient)
        # base_url is stored as an httpx URL object; comparing str gives us
        # a stable check regardless of whether httpx normalizes the URL.
        assert str(client.base_url).rstrip("/") == settings.jira_base_url.rstrip("/")


def test_lifespan_closes_client_on_shutdown(settings: Settings) -> None:
    """After the TestClient context exits, the shared client is closed and
    `app.state.httpx_client` is reset to None so nothing accidentally
    reuses a closed client."""
    app = create_app()
    with TestClient(app) as _:
        client = app.state.httpx_client
        assert client is not None and not client.is_closed
    # After shutdown
    assert app.state.httpx_client is None
    assert client.is_closed


def test_routes_reuse_the_lifespan_client(
    settings: Settings, mock_jira, monkeypatch,
) -> None:
    """A protected endpoint should hand the persistent client to JiraClient
    (not open a new one per request). We verify by patching make_httpx_client
    to raise — if any per-request client construction happens, the endpoint
    will explode. If the lifespan client is reused correctly, it succeeds."""
    from tests.conftest import login_as
    import httpx as _httpx

    app = create_app()
    with TestClient(app) as client:
        # Stub Jira so the endpoint has data to return.
        mock_jira.get("/rest/api/3/project/CM/components").mock(
            return_value=_httpx.Response(200, json=[])
        )
        mock_jira.get(
            f"/rest/api/3/field/customfield_13235/context/{settings.client_context_id}/option"
        ).mock(return_value=_httpx.Response(200, json={"values": []}))

        # Poison the factory — if the endpoint tries to open a fresh
        # AsyncClient it'll blow up. Reusing the lifespan client bypasses this.
        from hotfix_booking import jira_client
        monkeypatch.setattr(
            jira_client, "make_httpx_client",
            lambda s: (_ for _ in ()).throw(RuntimeError("per-request client construction!")),
        )

        login_as(client, "u@e.com", "U")
        r = client.get("/api/hotfix-booking/field-options")
        assert r.status_code == 200
