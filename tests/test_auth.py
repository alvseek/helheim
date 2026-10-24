from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from helheim.auth import EXPIRED, MISSING, OK, REFUSED, Auth, AuthError, pkce_challenge
from helheim.config import load_settings

PRM = {
    "resource": "https://munnin.example/mcp",
    "authorization_servers": ["https://auth.example/oidc"],
    "scopes_supported": ["openid", "profile", "email", "offline_access"],
}
OIDC = {
    "authorization_endpoint": "https://auth.example/oidc/auth",
    "token_endpoint": "https://auth.example/oidc/token",
}


class FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class FakeClient:
    """Answers the three requests a login makes, and nothing else."""

    def __init__(self, token_response: FakeResponse | None = None) -> None:
        self.token_response = token_response or FakeResponse(
            200,
            {
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )
        self.posted: list[dict] = []
        self.gets: list[str] = []

    def get(self, url: str, headers: dict | None = None) -> FakeResponse:
        self.gets.append(url)
        if url.endswith("/.well-known/oauth-protected-resource/mcp"):
            return FakeResponse(200, PRM)
        if url.endswith("/.well-known/openid-configuration"):
            return FakeResponse(200, OIDC)
        return FakeResponse(404, {})

    def post(self, url: str, data: dict | None = None, headers: dict | None = None) -> FakeResponse:
        self.posted.append(dict(data or {}))
        return self.token_response


@pytest.fixture
def settings(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HELHEIM_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('[app]\nclient_id = "client-abc"\n', encoding="utf-8")
    return load_settings()


def test_pkce_challenge_matches_the_rfc_vector() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert pkce_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_begin_carries_pkce_state_and_resource(settings) -> None:
    client = FakeClient()
    auth = Auth(settings, client=client)
    url = auth.begin(settings.server("munnin"))
    assert url.startswith("https://auth.example/oidc/auth?")
    assert "code_challenge_method=S256" in url
    assert "resource=https%3A%2F%2Fmunnin.example%2Fmcp" in url
    assert "client_id=client-abc" in url


def test_complete_stores_a_token_that_then_works(settings) -> None:
    client = FakeClient()
    auth = Auth(settings, client=client)
    url = auth.begin(settings.server("munnin"))
    state = url.split("state=")[1].split("&")[0]

    name = auth.complete("the-code", state)
    assert name == "munnin"

    stored = json.loads(settings.tokens_path.read_text(encoding="utf-8"))
    assert stored["munnin"]["refresh_token"] == "refresh-1"
    assert auth.token(settings.server("munnin")) == "access-1"
    assert auth.status(settings.server("munnin")) == OK

    exchange = client.posted[0]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["code_verifier"]
    assert exchange["resource"] == "https://munnin.example/mcp"


def test_a_reused_state_is_refused(settings) -> None:
    auth = Auth(settings, client=FakeClient())
    url = auth.begin(settings.server("munnin"))
    state = url.split("state=")[1].split("&")[0]
    auth.complete("the-code", state)
    with pytest.raises(AuthError):
        auth.complete("the-code", state)


def test_status_is_missing_before_a_login(settings) -> None:
    auth = Auth(settings, client=FakeClient())
    assert auth.status(settings.server("munnin")) == MISSING


def test_an_expired_token_is_refreshed(settings) -> None:
    client = FakeClient()
    auth = Auth(settings, client=client)
    settings.tokens_path.parent.mkdir(parents=True, exist_ok=True)
    settings.tokens_path.write_text(
        json.dumps(
            {
                "munnin": {
                    "access_token": "stale",
                    "refresh_token": "refresh-1",
                    "expires_at": time.time() - 10,
                    "resource": "https://munnin.example/mcp",
                }
            }
        ),
        encoding="utf-8",
    )
    assert auth.token(settings.server("munnin")) == "access-1"
    assert client.posted[0]["grant_type"] == "refresh_token"
    stored = json.loads(settings.tokens_path.read_text(encoding="utf-8"))
    assert stored["munnin"]["access_token"] == "access-1"


def test_invalid_target_reads_as_refused(settings) -> None:
    client = FakeClient(
        FakeResponse(
            400,
            {"error": "invalid_target", "error_description": "resource not registered"},
        )
    )
    auth = Auth(settings, client=client)
    settings.tokens_path.parent.mkdir(parents=True, exist_ok=True)
    settings.tokens_path.write_text(
        json.dumps(
            {
                "munnin": {
                    "access_token": "stale",
                    "refresh_token": "refresh-1",
                    "expires_at": time.time() - 10,
                    "resource": "https://munnin.example/mcp",
                }
            }
        ),
        encoding="utf-8",
    )
    assert auth.status(settings.server("munnin")) == REFUSED
    with pytest.raises(AuthError) as caught:
        auth.token(settings.server("munnin"))
    assert caught.value.status == REFUSED


def test_a_failed_refresh_reads_as_expired(settings) -> None:
    client = FakeClient(FakeResponse(400, {"error": "invalid_grant", "error_description": "gone"}))
    auth = Auth(settings, client=client)
    settings.tokens_path.parent.mkdir(parents=True, exist_ok=True)
    settings.tokens_path.write_text(
        json.dumps(
            {
                "munnin": {
                    "access_token": "stale",
                    "refresh_token": "refresh-1",
                    "expires_at": time.time() - 10,
                    "resource": "https://munnin.example/mcp",
                }
            }
        ),
        encoding="utf-8",
    )
    assert auth.status(settings.server("munnin")) == EXPIRED


def test_begin_without_a_client_id_says_so(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HELHEIM_HOME", str(tmp_path))
    settings = load_settings()
    auth = Auth(settings, client=FakeClient())
    with pytest.raises(AuthError) as caught:
        auth.begin(settings.server("munnin"))
    assert caught.value.status == MISSING
