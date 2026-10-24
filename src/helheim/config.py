"""Configuration: where the servers are, and where local state lives.

Every value has a default so the app starts with no config file at all. The one
value that cannot be defaulted is ``client_id``: the tenant has no registration
endpoint, so a client must be created by hand and its id written here or in
``config.toml``.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MAX_STORE_BYTES = 200 * 1024 * 1024

DEFAULT_SERVERS: tuple[tuple[str, str], ...] = (
    ("munnin", "https://munnin.lok.quest/mcp"),
    ("hermod", "https://hermod.lok.quest/mcp"),
    ("einherjar", "https://einherjar.lok.quest/mcp"),
)


@dataclass(frozen=True)
class Server:
    """One MCP server, and the resource identifier a token must be bound to."""

    name: str
    url: str
    resource: str


@dataclass(frozen=True)
class Settings:
    home: Path
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    client_id: str = ""
    max_store_bytes: int = DEFAULT_MAX_STORE_BYTES
    servers: tuple[Server, ...] = ()

    @property
    def tokens_path(self) -> Path:
        return self.home / "tokens.json"

    @property
    def db_path(self) -> Path:
        return self.home / "helheim.db"

    @property
    def redirect_uri(self) -> str:
        return f"http://{self.host}:{self.port}/callback"

    def server(self, name: str) -> Server | None:
        for server in self.servers:
            if server.name == name:
                return server
        return None


def default_home() -> Path:
    override = os.environ.get("HELHEIM_HOME")
    if override:
        return Path(override)
    return Path.home() / ".config" / "helheim"


def load_settings(path: Path | None = None) -> Settings:
    """Read ``config.toml`` if it exists, falling back to defaults."""
    home = default_home()
    config_path = path or (home / "config.toml")
    raw: dict = {}
    if config_path.is_file():
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)

    app = raw.get("app", {})
    server_rows = raw.get("servers", {})

    servers: list[Server] = []
    for name, default_url in DEFAULT_SERVERS:
        row = server_rows.get(name, {})
        url = row.get("url", default_url)
        servers.append(Server(name=name, url=url, resource=row.get("resource", url)))
    for name, row in server_rows.items():
        if name in {server.name for server in servers}:
            continue
        url = row.get("url", "")
        servers.append(Server(name=name, url=url, resource=row.get("resource", url)))

    return Settings(
        home=home,
        host=app.get("host", DEFAULT_HOST),
        port=int(app.get("port", DEFAULT_PORT)),
        client_id=app.get("client_id", ""),
        max_store_bytes=int(app.get("max_store_bytes", DEFAULT_MAX_STORE_BYTES)),
        servers=tuple(servers),
    )
