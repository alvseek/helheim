"""Real Authlib HTTP boundary to an owned synthetic loopback issuer."""

from __future__ import annotations

import socket
from html.parser import HTMLParser
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from test_hosted_login import ORIGIN, hosted_settings

from helheim.app import create_app
from qa.fixtures.hosted_oidc import Issuer


def settings_for_issuer(tmp_path, issuer):
    return hosted_settings(tmp_path, hosted_issuer=issuer.url)


def trust_only_test_issuer(app, issuer):
    login = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/login")
    gate = next(cell.cell_contents for cell in login.__closure__
                if hasattr(cell.cell_contents, "pending"))
    gate.client.client_kwargs["verify"] = issuer.client_tls
    gate.client.client_kwargs["trust_env"] = False


class CallbackForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.action = ""
        self.fields = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.action = attrs["action"]
        elif tag == "input":
            self.fields[attrs["name"]] = attrs["value"]


def stage_callback(client, issuer, authorization):
    form = CallbackForm()
    form.feed(authorization.text)
    assert form.action and set(form.fields) == {"state", "code"}
    response = client.post(form.action, data=form.fields, headers={"Origin": issuer.url})
    assert response.status_code == 303
    assert "?" not in response.headers["location"]
    return response.headers["location"]


@pytest.mark.parametrize(("identity", "fault", "expected"), [
    ("owner", "", 303), ("other", "", 403), ("owner", "issuer", 403),
    ("owner", "audience", 403), ("owner", "expired", 403),
    ("owner", "nonce", 403), ("owner", "signature", 403),
])
def test_only_valid_owner_gets_a_session_over_the_oidc_boundary(
    tmp_path, identity, fault, expected,
):
    with Issuer(tmp_path) as issuer:
        issuer.fault = fault
        app = create_app(settings_for_issuer(tmp_path, issuer))
        trust_only_test_issuer(app, issuer)
        with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
            start = client.get("/login")
            assert start.status_code == 302
            with httpx.Client(verify=issuer.client_tls, trust_env=False,
                              follow_redirects=False) as browser:
                authorization = browser.get(start.headers["location"] + "&identity=" + identity)
            assert authorization.status_code == 200
            location = stage_callback(client, issuer, authorization)
            callback = client.get(location)
            assert callback.status_code == expected
            assert client.get("/").status_code == (200 if expected == 303 else 303)
            assert client.get(location).status_code == 400


def test_callback_is_browser_bound_and_issuer_tears_down(tmp_path):
    with Issuer(tmp_path) as issuer:
        port = issuer.server.server_port
        temp_dir = Path(issuer.temp.name)
        app = create_app(settings_for_issuer(tmp_path, issuer))
        trust_only_test_issuer(app, issuer)
        with TestClient(app, base_url=ORIGIN, follow_redirects=False) as owner:
            with TestClient(app, base_url=ORIGIN, follow_redirects=False) as stranger:
                start = owner.get("/login")
                with httpx.Client(verify=issuer.client_tls, trust_env=False,
                                  follow_redirects=False) as browser:
                    auth = browser.get(start.headers["location"] + "&identity=owner")
                location = stage_callback(stranger, issuer, auth)
                assert stranger.get(location).status_code == 400
                start = owner.get("/login")
                with httpx.Client(verify=issuer.client_tls, trust_env=False,
                                  follow_redirects=False) as browser:
                    auth = browser.get(start.headers["location"] + "&identity=owner")
                assert owner.get(stage_callback(owner, issuer, auth)).status_code == 303
                assert stranger.get("/").status_code == 303
    with socket.socket() as probe:
        probe.settimeout(1)
        assert probe.connect_ex(("127.0.0.1", port)) != 0
    assert not temp_dir.exists()


def test_test_certificate_is_not_trusted_by_default(tmp_path):
    with Issuer(tmp_path) as issuer:
        with httpx.Client(trust_env=False) as browser:
            with pytest.raises(httpx.ConnectError):
                browser.get(issuer.url + "/.well-known/openid-configuration")
