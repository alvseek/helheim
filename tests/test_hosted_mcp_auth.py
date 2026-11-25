"""Hosted MCP unit tests use in-process doubles, never a live issuer."""

from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from test_hosted_login import ORIGIN, hosted_settings

from helheim.app import create_app
from helheim.auth import EXPIRED, MISSING, OK, AuthError, Token
from helheim.config import Server
from helheim.hosted_login import SESSION_COOKIE

ISSUER = "https://issuer.example"
SERVERS = (
    Server("one", "https://one.example/mcp", "https://one.example/mcp"),
    Server("two", "https://two.example/mcp", "https://two.example/mcp"),
)
METADATA = {
    "issuer": ISSUER,
    "authorization_endpoint": ISSUER + "/authorize",
    "token_endpoint": ISSUER + "/token",
    "jwks_uri": ISSUER + "/jwks",
}


class FakeTransport:
    def __init__(self):
        self.resource_override = None
        self.issuer_override = None
        self.posts = []
        self.refresh_response = httpx.Response(
            400, request=httpx.Request("POST", ISSUER + "/token")
        )

    def get(self, url, headers=None):
        if url.endswith("/.well-known/openid-configuration"):
            data = METADATA
        else:
            origin = "https://one.example" if "one.example" in url else "https://two.example"
            data = {"resource": self.resource_override or origin + "/mcp",
                    "authorization_servers": [self.issuer_override or ISSUER]}
        return httpx.Response(200, json=data, request=httpx.Request("GET", url))

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.refresh_response


class FakeOAuth:
    def __init__(self):
        self.subject = "owner"
        self.fail = False
        self.n = 0
        self.exchanges = []

    async def load_server_metadata(self):
        return METADATA

    async def authorize_redirect(self, request, redirect_uri, resource, response_mode):
        assert response_mode == "form_post"
        self.n += 1
        state = f"state-{self.n}"
        request.session[state] = {"resource": resource}
        url = ISSUER + "/authorize?" + urlencode({
            "state": state, "resource": resource, "redirect_uri": redirect_uri,
        })
        return RedirectResponse(url, status_code=302)

    async def authorize_access_token(self, request, resource, **kwargs):
        self.exchanges.append(resource)
        state = (await request.form())["state"]
        assert request.session[state]["resource"] == resource
        if self.fail:
            raise ValueError("refused")
        return {"userinfo": {"sub": self.subject}, "access_token": "fixture",
                "token_type": "Bearer", "expires_in": 3600}


@pytest.fixture
def rig(tmp_path):
    app = create_app(hosted_settings(tmp_path, servers=SERVERS))
    auth = app.state.auth
    transport = FakeTransport()
    oauth = FakeOAuth()
    auth._client = transport
    auth.oauth = oauth
    auth.owner_gate.sessions["session-a"] = time.monotonic() + 600
    auth.owner_gate.sessions["session-b"] = time.monotonic() + 600
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        client.cookies.set(SESSION_COOKIE, "session-a", domain="helheim.example", path="/")
        yield client, auth, oauth, transport


def start(client, server="one"):
    response = client.get(f"/auth/{server}/start")
    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["redirect_uri"] == [ORIGIN + "/auth/mcp/callback"]
    return query


def callback(client, state):
    response = client.post("/auth/mcp/callback", data={"state": state, "code": "fixture"},
                           headers={"Origin": ISSUER})
    if response.status_code != 303:
        return response
    return client.get(response.headers["location"])


def test_owner_flow_is_per_resource_and_uses_separate_hosted_storage(rig):
    client, auth, oauth, _ = rig
    query = start(client)
    assert query["resource"] == [SERVERS[0].resource]
    assert callback(client, query["state"][0]).status_code == 303
    assert auth.status(SERVERS[0]) == OK
    assert auth.status(SERVERS[1]) == MISSING
    assert oauth.exchanges == [SERVERS[0].resource]
    assert not auth.settings.tokens_path.exists()
    path = auth.settings.home / "hosted-mcp-tokens.json"
    assert path.exists()
    assert set(json.loads(path.read_text())) == {"one"}
    assert callback(client, query["state"][0]).status_code == 400


def test_authlib_builds_resource_bound_pkce_and_nonce_request(tmp_path):
    app = create_app(hosted_settings(tmp_path, servers=SERVERS))
    auth = app.state.auth
    auth._client = FakeTransport()
    auth.owner_gate.sessions["session-a"] = time.monotonic() + 600

    async def metadata():
        return METADATA

    exchanges = []

    async def fetch_access_token(**kwargs):
        exchanges.append(kwargs)
        return {"id_token": "fixture", "access_token": "fixture",
                "token_type": "Bearer", "expires_in": 600}

    async def parse_id_token(token, nonce, **kwargs):
        assert nonce and kwargs["leeway"] == 0
        return {"sub": "owner"}

    auth.oauth.load_server_metadata = metadata
    auth.oauth.fetch_access_token = fetch_access_token
    auth.oauth.parse_id_token = parse_id_token
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        client.cookies.set(SESSION_COOKIE, "session-a", domain="helheim.example", path="/")
        query = start(client)
        assert query["resource"] == [SERVERS[0].resource]
        assert query["client_id"] == [auth.settings.hosted_client_id]
        assert query["code_challenge_method"] == ["S256"]
        assert query["code_challenge"] and query["nonce"] and query["state"]
        assert set(query["scope"][0].split()) == {"openid", "offline_access"}
        assert callback(client, query["state"][0]).status_code == 303
        assert exchanges[0]["resource"] == SERVERS[0].resource
        assert exchanges[0]["redirect_uri"] == ORIGIN + "/auth/mcp/callback"
        assert exchanges[0]["code_verifier"]


def test_pending_is_bound_to_owner_session_and_expiry(rig):
    client, auth, oauth, _ = rig
    state = start(client)["state"][0]
    client.cookies.set(SESSION_COOKIE, "session-b", domain="helheim.example", path="/")
    assert callback(client, state).status_code == 400
    assert not oauth.exchanges
    client.cookies.set(SESSION_COOKIE, "session-a", domain="helheim.example", path="/")
    entry = auth._hosted_pending[state]
    auth._hosted_pending[state] = (0, *entry[1:])
    assert callback(client, state).status_code == 400
    assert not oauth.exchanges


def test_mcp_handoff_refusal_and_expired_owner_do_not_exchange(rig):
    client, auth, oauth, _ = rig
    state = start(client)["state"][0]
    response = client.post("/auth/mcp/callback", data={"state": state, "error": "denied"},
                           headers={"Origin": ISSUER})
    assert response.status_code == 303
    assert client.get(response.headers["location"]).status_code == 400
    assert not oauth.exchanges
    state = start(client)["state"][0]
    response = client.post("/auth/mcp/callback", data={"state": state, "code": "fixture"},
                           headers={"Origin": ISSUER})
    assert response.status_code == 303
    auth.owner_gate.sessions["session-a"] = 0
    assert client.get(response.headers["location"]).status_code == 303
    assert not oauth.exchanges


@pytest.mark.asyncio
async def test_logout_during_exchange_prevents_token_storage(rig):
    client, auth, _, _ = rig
    state = start(client)["state"][0]
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedOAuth(FakeOAuth):
        async def authorize_access_token(self, request, resource, **kwargs):
            entered.set()
            await release.wait()
            return await super().authorize_access_token(request, resource, **kwargs)

    auth.oauth = PausedOAuth()
    body = urlencode({"state": state, "code": "fixture"}).encode()
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    request = Request({
        "type": "http", "method": "POST", "path": "/auth/mcp/callback",
        "query_string": b"",
        "headers": [(b"cookie", (SESSION_COOKIE + "=session-a").encode()),
                    (b"content-type", b"application/x-www-form-urlencoded")],
    }, receive)
    task = asyncio.create_task(auth.complete_hosted(request))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        auth.owner_gate.sessions.pop("session-a")
        release.set()
        with pytest.raises(AuthError):
            await task
        assert not (auth.settings.home / "hosted-mcp-tokens.json").exists()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("kind", ["other", "declined"])
def test_nonowner_or_declined_consent_never_stores(rig, kind):
    client, auth, oauth, _ = rig
    state = start(client)["state"][0]
    oauth.subject = "other" if kind == "other" else "owner"
    oauth.fail = kind == "declined"
    assert callback(client, state).status_code == 400
    assert not (auth.settings.home / "hosted-mcp-tokens.json").exists()
    assert callback(client, state).status_code == 400


@pytest.mark.parametrize("kind", ["resource", "issuer"])
def test_discovery_mismatch_is_refused_without_consent(rig, kind):
    client, auth, oauth, transport = rig
    if kind == "resource":
        transport.resource_override = "https://wrong.example/mcp"
    else:
        transport.issuer_override = "https://wrong.example"
    assert client.get("/auth/one/start").status_code == 502
    assert not oauth.exchanges and not auth._hosted_pending


def test_expiry_without_refresh_requires_reconnect_and_resource_mismatch_is_missing(rig):
    _, auth, _, _ = rig
    auth._store("one", Token("fixture", "", time.time() - 1, SERVERS[0].resource))
    assert auth.status(SERVERS[0]) == EXPIRED
    auth._store("two", Token("fixture", "", time.time() + 100, SERVERS[0].resource))
    assert auth.status(SERVERS[1]) == MISSING


def test_refresh_refusal_keeps_dashboard_available_and_offers_reconnect(rig):
    client, auth, _, transport = rig
    auth._store("one", Token("fixture", "fixture-refresh", time.time() - 1, SERVERS[0].resource))
    assert auth.status(SERVERS[0]) == EXPIRED
    assert transport.posts[0][1]["data"]["resource"] == SERVERS[0].resource
    page = client.get("/")
    assert page.status_code == 200 and "/auth/one/start" in page.text
    assert start(client)["resource"] == [SERVERS[0].resource]


def test_refresh_uses_web_client_and_preserves_resource(rig):
    _, auth, _, transport = rig
    transport.refresh_response = httpx.Response(
        200, json={"access_token": "refreshed", "token_type": "Bearer", "expires_in": 600},
        request=httpx.Request("POST", ISSUER + "/token"),
    )
    auth._store("one", Token("fixture", "fixture-refresh", time.time() - 1, SERVERS[0].resource))
    assert auth.token(SERVERS[0]) == "refreshed"
    _, request = transport.posts[0]
    assert request["auth"] == (auth.settings.hosted_client_id, auth.settings.hosted_client_secret)
    assert request["data"]["resource"] == SERVERS[0].resource
    assert auth.status(SERVERS[1]) == MISSING


def test_atomic_replace_failure_preserves_previous_file_and_removes_temp(rig, monkeypatch):
    _, auth, _, _ = rig
    path = auth.settings.home / "hosted-mcp-tokens.json"
    auth._store("one", Token("fixture", "", time.time() + 100, SERVERS[0].resource))
    original = path.read_bytes()

    def fail_replace(*args):
        raise OSError("fixture failure")

    monkeypatch.setattr("helheim.hosted_mcp_auth.os.replace", fail_replace)
    with pytest.raises(OSError):
        auth._store("two", Token("other", "", time.time() + 100, SERVERS[1].resource))
    assert path.read_bytes() == original
    assert not list(path.parent.glob(".helheim-tokens-*"))


def test_short_lived_and_invalid_token_responses_fail_closed(rig):
    _, auth, _, _ = rig
    short = auth._token_from({"access_token": "fixture", "token_type": "Bearer",
                              "expires_in": 1}, SERVERS[0].resource)
    assert short.expired
    with pytest.raises(AuthError):
        auth._token_from({"access_token": "", "token_type": "Bearer", "expires_in": 10},
                         SERVERS[0].resource)
