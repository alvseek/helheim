"""Security checks for the hosted-only form-post staging boundary."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from test_hosted_login import ORIGIN
from test_qa_hosted_oidc import settings_for_issuer, trust_only_test_issuer

from helheim.app import create_app
from qa.fixtures.hosted_oidc import Issuer


@pytest.fixture
def login(tmp_path):
    with Issuer(tmp_path) as issuer:
        app = create_app(settings_for_issuer(tmp_path, issuer))
        trust_only_test_issuer(app, issuer)
        with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
            yield client, issuer, app


def begin(client):
    redirect = client.get("/login")
    query = parse_qs(urlsplit(redirect.headers["location"]).query)
    assert query["response_mode"] == ["form_post"]
    return query["state"][0]


def post(client, issuer, **fields):
    return client.post("/login/callback", data=fields, headers={"Origin": issuer.url})


def test_callback_origin_shape_replay_and_query_fallback(login):
    client, issuer, app = login
    state = begin(client)
    assert client.post("/login/callback", data={"state": state, "code": "secret"},
                       headers={"Origin": "https://wrong.example"}).status_code == 400
    duplicate = client.post(
        "/login/callback", content=f"state={state}&state={state}&code=x",
        headers={"Origin": issuer.url, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert duplicate.status_code == 400
    assert client.post("/login/callback", data={"state": state, "code": "x", "error": "no"},
                       headers={"Origin": issuer.url}).status_code == 400
    assert client.post("/login/callback?code=wrong-mode", data={"state": state, "code": "x"},
                       headers={"Origin": issuer.url}).status_code == 400
    assert client.get("/login/callback?state=wrong-mode&code=wrong-mode").status_code != 200
    assert not app.state.auth.owner_gate.sessions
    response = post(client, issuer, state=state, error="access_denied")
    assert response.status_code == 303 and response.headers["location"] == "/auth/login/complete"
    assert client.get(response.headers["location"]).status_code == 403
    assert post(client, issuer, state=state, code="x").status_code == 400
    assert client.get("/auth/login/complete").status_code == 400


def test_expired_handoff_and_host_csrf_guards(login):
    client, issuer, app = login
    state = begin(client)
    assert client.post("/login/callback", data={"state": state, "code": "x"},
                       headers={"Origin": issuer.url, "Host": "wrong.example"}).status_code == 403
    response = post(client, issuer, state=state, code="x")
    assert response.status_code == 303
    gate = app.state.auth.owner_gate
    key = next(iter(gate.handoff.handoffs))
    entry = gate.handoff.handoffs[key]
    gate.handoff.handoffs[key] = (0, *entry[1:])
    assert client.get(response.headers["location"]).status_code == 400
    assert client.post("/logout", headers={"Origin": issuer.url}).status_code == 401


def test_handoff_capacity_is_bounded(login, monkeypatch):
    client, issuer, _ = login
    monkeypatch.setattr("helheim.hosted_handoff.MAX_HANDOFFS", 1)
    state = begin(client)
    assert post(client, issuer, state=state, code="x").status_code == 303
    other = begin(client)
    assert post(client, issuer, state=other, code="y").status_code == 400
