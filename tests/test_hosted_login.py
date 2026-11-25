from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest
from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from helheim.app import create_app
from helheim.config import Settings
from helheim.hosted_login import PENDING_COOKIE, SESSION_COOKIE, HostedLogin

ORIGIN = "https://helheim.example"


def hosted_settings(tmp_path, **overrides):
    values = {
        "home": tmp_path, "mode": "hosted", "host": "0.0.0.0",
        "hosted_origin": ORIGIN, "hosted_issuer": "https://issuer.example",
        "hosted_client_id": "test-client", "hosted_client_secret": "test-only",
        "hosted_owner_sub": "owner",
    }
    values.update(overrides)
    return Settings(**values)


class FakeOIDC:
    subject = "owner"

    async def load_server_metadata(self):
        return {"issuer": "https://issuer.example",
                "authorization_endpoint": "https://issuer.example/authorize",
                "token_endpoint": "https://issuer.example/token",
                "jwks_uri": "https://issuer.example/jwks"}

    async def authorize_redirect(self, request, redirect_uri, response_mode):
        assert redirect_uri == ORIGIN + "/login/callback"
        assert response_mode == "form_post"
        request.session["state"] = "bound"
        return RedirectResponse("https://issuer.example/authorize?state=y", status_code=302)

    async def authorize_access_token(self, request, **kwargs):
        assert request.session.pop("state") == "bound"
        assert kwargs["leeway"] == 0
        return {"userinfo": {"sub": self.subject}}


@pytest.fixture
def rig(tmp_path):
    app = create_app(hosted_settings(tmp_path))
    # Find the closed-over owner gate without changing any app-facing routes.
    login = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/login")
    gate = next(cell.cell_contents for cell in login.__closure__
                if hasattr(cell.cell_contents, "pending"))
    gate.client = FakeOIDC()
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        yield client, gate


@pytest.mark.parametrize("override", [
    {"mode": "invalid"}, {"hosted_origin": "http://helheim.example"},
    {"hosted_origin": "https://helheim.example/path"}, {"hosted_owner_sub": ""},
    {"hosted_issuer": "http://issuer.example"},
    {"hosted_issuer": "http://127.0.0.1:1234"}, {"hosted_client_secret": ""},
])
def test_invalid_hosted_configuration_fails_closed(tmp_path, override):
    with pytest.raises(ValueError):
        hosted_settings(tmp_path, **override)


def login_request():
    return Request({"type": "http", "method": "GET", "path": "/login",
                    "headers": [], "query_string": b""})


def finish_login(client):
    response = client.post("/login/callback", data={"code": "x", "state": "y"},
                           headers={"Origin": "https://issuer.example"})
    if response.status_code != 303:
        return response
    return client.get(response.headers["location"])


@pytest.mark.asyncio
async def test_login_start_cap_includes_pending_and_paused_starts(tmp_path, monkeypatch):
    monkeypatch.setattr("helheim.hosted_login.MAX_ENTRIES", 1)
    gate = HostedLogin(hosted_settings(tmp_path))
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedOIDC(FakeOIDC):
        async def load_server_metadata(self):
            entered.set()
            await release.wait()
            return await super().load_server_metadata()

    gate.client = PausedOIDC()
    first = asyncio.create_task(gate.begin(login_request()))
    await asyncio.wait_for(entered.wait(), timeout=2)
    try:
        assert gate.pending_starts == 1 and len(gate.pending) == 0
        refused = await asyncio.wait_for(gate.begin(login_request()), timeout=0.5)
        assert refused.status_code == 503
        assert gate.pending_starts == 1 and len(gate.pending) == 0
        release.set()
        assert (await first).status_code == 302
        assert gate.pending_starts == 0 and len(gate.pending) == 1
        assert (await gate.begin(login_request())).status_code == 503
        assert len(gate.pending) == 1
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_login_start_reservation_is_released_on_error_or_cancellation(
    tmp_path, monkeypatch, cancel,
):
    monkeypatch.setattr("helheim.hosted_login.MAX_ENTRIES", 1)
    gate = HostedLogin(hosted_settings(tmp_path))
    entered = asyncio.Event()

    class BrokenOIDC(FakeOIDC):
        async def load_server_metadata(self):
            entered.set()
            if cancel:
                await asyncio.Event().wait()
            raise ValueError("test failure")

    gate.client = BrokenOIDC()
    task = asyncio.create_task(gate.begin(login_request()))
    await asyncio.wait_for(entered.wait(), timeout=2)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert (await task).status_code == 503
    assert gate.pending_starts == 0 and not gate.pending
    gate.client = FakeOIDC()
    assert (await gate.begin(login_request())).status_code == 302


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["authorization_endpoint", "token_endpoint", "jwks_uri"])
async def test_plain_http_issuer_endpoints_are_refused(tmp_path, key):
    gate = HostedLogin(hosted_settings(tmp_path))

    class UnsafeOIDC(FakeOIDC):
        async def load_server_metadata(self):
            metadata = await super().load_server_metadata()
            metadata[key] = "http://127.0.0.1:1234/" + key
            return metadata

    gate.client = UnsafeOIDC()
    assert (await gate.begin(login_request())).status_code == 503
    assert gate.pending_starts == 0 and not gate.pending


def test_owner_session_is_opaque_bounded_and_logout_invalidates(rig):
    client, gate = rig
    assert client.get("/").status_code == 303
    start = client.get("/login")
    assert start.status_code == 302
    assert "Secure" in start.headers["set-cookie"] and "HttpOnly" in start.headers["set-cookie"]
    assert finish_login(client).status_code == 303
    assert client.get("/").status_code == 200
    assert client.cookies.get(SESSION_COOKIE) in gate.sessions
    assert client.cookies.get(PENDING_COOKIE) is None
    assert finish_login(client).status_code == 400
    page = client.get("/memory")
    import re
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
    response = client.post("/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})
    assert response.status_code == 303
    assert client.get("/").status_code == 303
    assert not gate.sessions


def test_non_owner_and_cross_browser_are_refused(rig):
    client, gate = rig
    client.get("/login")
    pending = client.cookies.get(PENDING_COOKIE)
    client.cookies.delete(PENDING_COOKIE, domain="helheim.example", path="/")
    assert finish_login(client).status_code == 400
    client.cookies.set(PENDING_COOKIE, pending, domain="helheim.example", path="/")
    gate.client.subject = "other"
    client.get("/login")
    assert finish_login(client).status_code == 403
    assert not gate.sessions


def test_expired_pending_and_session_are_rejected(rig):
    client, gate = rig
    client.get("/login")
    key = client.cookies.get(PENDING_COOKIE)
    gate.pending[key] = (0, gate.pending[key][1])
    assert finish_login(client).status_code == 400
    client.get("/login")
    finish_login(client)
    gate.sessions[client.cookies.get(SESSION_COOKIE)] = 0
    assert client.get("/").status_code == 303


def test_anonymous_gate_covers_static_callback_and_actions(rig):
    client, _ = rig
    for path in ("/", "/history", "/memory", "/procedures", "/static/app.css",
                 "/callback", "/docs", "/openapi.json", "/health/"):
        assert client.get(path).status_code == 303
    for path in ("/memory-edit/unknown/save", "/procedures/workspaces", "/logout"):
        assert client.post(path).status_code == 401
    assert client.get("/health").json() == {"ok": True}
    assert client.get("/", headers={"Host": "wrong.example"}).status_code == 403
    assert client.get("/login", headers={"Host": "wrong.example"}).status_code == 403
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/anything"):
            pass


def test_host_origin_and_csrf_guards_precede_writes(rig):
    client, gate = rig
    client.get("/login")
    assert finish_login(client).status_code == 303
    marker = client.cookies.get(SESSION_COOKIE)
    assert client.post("/logout").status_code == 403
    assert client.post("/logout", headers={"Origin": "https://wrong.example",
                                           "X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post("/logout", headers={"Host": "wrong.example", "Origin": ORIGIN,
                                           "X-CSRF-Token": "wrong"}).status_code == 403
    assert marker in gate.sessions
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/static/app.css").status_code == 200


def test_refused_reauthentication_revokes_existing_session(rig):
    client, gate = rig
    client.get("/login")
    finish_login(client)
    old = client.cookies.get(SESSION_COOKIE)
    assert client.get("/").status_code == 200
    client.get("/login")
    gate.client.subject = "other"
    assert finish_login(client).status_code == 403
    assert old not in gate.sessions
    assert client.get("/").status_code == 303


def test_successful_reauthentication_rotates_session(rig):
    client, gate = rig
    client.get("/login")
    finish_login(client)
    old = client.cookies.get(SESSION_COOKIE)
    client.get("/login")
    response = finish_login(client)
    new = client.cookies.get(SESSION_COOKIE)
    assert response.status_code == 303
    assert old != new and old not in gate.sessions and new in gate.sessions
    headers = response.headers.get_list("set-cookie")
    assert any(SESSION_COOKIE in value and "Secure" in value and "HttpOnly" in value
               and "SameSite=lax" in value and "Domain=" not in value for value in headers)


def test_unapproved_writes_never_reach_the_downstream_adapter(rig):
    import re

    client, _ = rig
    create = Mock(return_value={"id": "fixture"})
    client.app.state.workspaces.create = create
    body = {"repository": "test-only"}
    path = "/procedures/workspaces"
    assert client.post(path, json=body).status_code == 401
    client.get("/login")
    finish_login(client)
    page = client.get("/memory")
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
    assert client.post(path, json=body, headers={"Origin": ORIGIN}).status_code == 403
    assert client.post(path, json=body, headers={"Origin": "https://wrong.example",
                                                "X-CSRF-Token": csrf}).status_code == 403
    assert client.post(path, json=body, headers={"Host": "wrong.example", "Origin": ORIGIN,
                                                "X-CSRF-Token": csrf}).status_code == 403
    create.assert_not_called()
    assert client.post(path, json=body, headers={"Origin": ORIGIN,
                                                "X-CSRF-Token": csrf}).status_code == 200
    create.assert_called_once_with("test-only")
