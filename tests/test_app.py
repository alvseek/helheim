from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from helheim.app import create_app
from helheim.config import load_settings


def make_client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("HELHEIM_HOME", str(tmp_path))
    return TestClient(create_app(load_settings()))


def test_health_reports_the_configured_servers(tmp_path: Path, monkeypatch) -> None:
    client = make_client(tmp_path, monkeypatch)
    body = client.get("/health").json()
    assert body["ok"] is True
    assert body["servers"] == 3
    assert body["client_configured"] is False


def test_index_lists_every_server(tmp_path: Path, monkeypatch) -> None:
    client = make_client(tmp_path, monkeypatch)
    response = client.get("/")
    assert response.status_code == 200
    for name in ("munnin", "hermod", "einherjar"):
        assert name in response.text
    assert "No client is configured" in response.text
