"""Hosted MCP consent against one owned HTTPS issuer; all MCP peers are doubled."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from test_hosted_login import ORIGIN, hosted_settings
from test_hosted_mcp_auth import SERVERS
from test_qa_hosted_oidc import stage_callback

from helheim.app import create_app
from helheim.auth import EXPIRED, MISSING, OK
from helheim.hosted_login import SESSION_COOKIE
from qa.fixtures.hosted_oidc import Issuer


class ResourceDiscoveryDouble:
    """Doubles only protected-resource discovery; refuses all non-issuer network."""

    def __init__(self, issuer):
        self.issuer = issuer
        self.resource_override = None
        self.issuer_override = None
        self.client = httpx.Client(
            verify=issuer.client_tls, trust_env=False, follow_redirects=False
        )

    def get(self, url, **kwargs):
        for server in SERVERS:
            if url.startswith(server.url.rsplit("/mcp", 1)[0] + "/.well-known/"):
                return httpx.Response(
                    200, request=httpx.Request("GET", url),
                    json={"resource": self.resource_override or server.resource,
                          "authorization_servers": [self.issuer_override or self.issuer.url]},
                )
        assert url.startswith(self.issuer.url + "/")
        return self.client.get(url, **kwargs)

    def post(self, url, **kwargs):
        assert url == self.issuer.url + "/token"
        return self.client.post(url, **kwargs)

    def close(self):
        self.client.close()


@pytest.fixture
def rig(tmp_path):
    with Issuer(tmp_path, mcp=True) as issuer:
        temp_dir, port = Path(issuer.temp.name), issuer.server.server_port
        app = create_app(hosted_settings(
            tmp_path / "app", hosted_issuer=issuer.url, servers=SERVERS
        ))
        auth = app.state.auth
        transport = ResourceDiscoveryDouble(issuer)
        auth._client = transport
        auth.oauth.client_kwargs.update({"verify": issuer.client_tls, "trust_env": False})
        auth.owner_gate.sessions["owner-a"] = time.monotonic() + 600
        auth.owner_gate.sessions["owner-b"] = time.monotonic() + 600
        try:
            with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
                client.cookies.set(SESSION_COOKIE, "owner-a", domain="helheim.example", path="/")
                yield client, auth, issuer, transport
        finally:
            transport.close()
    assert not temp_dir.exists()
    assert not issuer.thread.is_alive()
    with socket.socket() as probe:
        probe.settimeout(1)
        assert probe.connect_ex(("127.0.0.1", port)) != 0


def initiate(client, issuer, identity="owner", server="one"):
    start = client.get(f"/auth/{server}/start")
    assert start.status_code == 302
    url = start.headers["location"]
    query = parse_qs(urlsplit(url).query)
    assert query["redirect_uri"] == [ORIGIN + "/auth/mcp/callback"]
    assert query["resource"] == [next(item.resource for item in SERVERS if item.name == server)]
    assert query["code_challenge_method"] == ["S256"]
    assert query["nonce"] and query["state"] and query["code_challenge"]
    with httpx.Client(verify=issuer.client_tls, trust_env=False, follow_redirects=False) as browser:
        result = browser.get(url + "&identity=" + identity)
    assert result.status_code == 200
    return stage_callback(client, issuer, result), query["state"][0]


def token_snapshot(auth):
    path = auth.settings.home / "hosted-mcp-tokens.json"
    return path.read_bytes() if path.exists() else None


def test_owner_consent_stores_only_requested_resource_and_replay_is_rejected(rig):
    client, auth, issuer, _ = rig
    location, _ = initiate(client, issuer)
    assert client.get(location).status_code == 303
    assert auth.status(SERVERS[0]) == OK
    assert auth.status(SERVERS[1]) == MISSING
    rows = json.loads(token_snapshot(auth))
    assert set(rows) == {"one"} and rows["one"]["resource"] == SERVERS[0].resource
    assert not auth.settings.tokens_path.exists()
    assert {"stage": "discovery"} in issuer.observations
    assert {"stage": "jwks"} in issuer.observations
    assert {"stage": "authorize", "resource": SERVERS[0].resource,
            "callback": "/auth/mcp/callback", "pkce": True, "nonce": True} in issuer.observations
    assert {"stage": "exchange", "resource": SERVERS[0].resource,
            "pkce_verified": True} in issuer.observations
    original = token_snapshot(auth)
    assert client.get(location).status_code == 400
    assert token_snapshot(auth) == original


@pytest.mark.parametrize("fault", ["other", "issuer", "audience", "expired", "nonce", "signature"])
def test_rejected_identity_or_signed_claim_never_replaces_token(rig, fault):
    client, auth, issuer, _ = rig
    location, _ = initiate(client, issuer)
    assert client.get(location).status_code == 303
    original = token_snapshot(auth)
    issuer.fault = "" if fault == "other" else fault
    rejected, _ = initiate(client, issuer, identity="other" if fault == "other" else "owner")
    assert client.get(rejected).status_code == 400
    assert token_snapshot(auth) == original


def test_other_owner_session_and_expired_transaction_cannot_replace_token(rig):
    client, auth, issuer, _ = rig
    location, _ = initiate(client, issuer)
    assert client.get(location).status_code == 303
    original = token_snapshot(auth)
    other, state = initiate(client, issuer)
    before = len([item for item in issuer.observations if item["stage"] == "exchange"])
    client.cookies.set(SESSION_COOKIE, "owner-b", domain="helheim.example", path="/")
    assert client.get(other).status_code == 400
    assert token_snapshot(auth) == original
    assert len([item for item in issuer.observations if item["stage"] == "exchange"]) == before
    client.cookies.set(SESSION_COOKIE, "owner-a", domain="helheim.example", path="/")
    entry = auth._hosted_pending[state]
    auth._hosted_pending[state] = (0, *entry[1:])
    assert client.get(other).status_code == 400
    assert token_snapshot(auth) == original
    assert len([item for item in issuer.observations if item["stage"] == "exchange"]) == before


@pytest.mark.parametrize("mismatch", ["resource", "issuer", "discovery-issuer"])
def test_resource_or_issuer_mismatch_cannot_replace_token(rig, mismatch):
    client, auth, issuer, transport = rig
    location, _ = initiate(client, issuer)
    assert client.get(location).status_code == 303
    original = token_snapshot(auth)
    auth._endpoints.clear()
    if mismatch == "resource":
        transport.resource_override = "https://wrong.example/mcp"
    elif mismatch == "issuer":
        transport.issuer_override = "https://wrong.example"
    else:
        issuer.fault = "discovery-issuer"
    assert client.get("/auth/one/start").status_code == 502
    assert token_snapshot(auth) == original


@pytest.mark.parametrize("refresh_mode", ["absent", "refused"])
def test_absent_or_refused_refresh_keeps_dashboard_and_offers_reconnect(rig, refresh_mode):
    client, auth, issuer, _ = rig
    issuer.refresh_mode = refresh_mode
    location, _ = initiate(client, issuer)
    assert client.get(location).status_code == 303
    token = auth._load("one")
    token.expires_at = time.time() - 1
    auth._store("one", token)
    original = token_snapshot(auth)
    page = client.get("/")
    assert page.status_code == 200 and "/auth/one/start" in page.text
    assert auth.status(SERVERS[0]) == EXPIRED
    assert token_snapshot(auth) == original
    assert client.get("/auth/one/start").status_code == 302
    refreshes = [item for item in issuer.observations if item["stage"] == "refresh"]
    if refresh_mode == "absent":
        assert not refreshes
    else:
        assert refreshes and all(item["resource"] == SERVERS[0].resource for item in refreshes)
