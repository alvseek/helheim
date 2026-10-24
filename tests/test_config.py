from __future__ import annotations

from pathlib import Path

from helheim.config import DEFAULT_SERVERS, load_settings


def test_defaults_provide_three_servers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HELHEIM_HOME", str(tmp_path))
    settings = load_settings()
    assert [server.name for server in settings.servers] == [name for name, _ in DEFAULT_SERVERS]
    assert settings.client_id == ""
    assert settings.redirect_uri == "http://127.0.0.1:8765/callback"


def test_config_file_overrides_client_and_server(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HELHEIM_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        "\n".join(
            [
                "[app]",
                'client_id = "abc123"',
                "port = 9000",
                "",
                "[servers.munnin]",
                'url = "http://127.0.0.1:8123/mcp"',
                'resource = "https://munnin.example/mcp"',
            ]
        ),
        encoding="utf-8",
    )
    settings = load_settings()
    assert settings.client_id == "abc123"
    assert settings.redirect_uri == "http://127.0.0.1:9000/callback"
    munnin = settings.server("munnin")
    assert munnin is not None
    assert munnin.url == "http://127.0.0.1:8123/mcp"
    assert munnin.resource == "https://munnin.example/mcp"


def test_unknown_server_in_config_is_kept(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HELHEIM_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        "\n".join(["[servers.local]", 'url = "http://127.0.0.1:9999/mcp"']), encoding="utf-8"
    )
    settings = load_settings()
    assert settings.server("local") is not None
    assert settings.server("nope") is None
