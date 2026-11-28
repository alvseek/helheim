"""Configuration: where the servers are, and where local state lives.

Every value has a default so the app starts with no config file at all. The one
value that cannot be defaulted is ``client_id``: the tenant has no registration
endpoint, so a client must be created by hand and its id written here or in
``config.toml``.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .github import Repository

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
    procedure_repositories: tuple[Repository, ...] = ()
    mode: str = "desktop"
    hosted_origin: str = ""
    hosted_issuer: str = ""
    hosted_client_id: str = ""
    hosted_client_secret: str = field(default="", repr=False)
    hosted_owner_sub: str = ""
    hosted_github_token: str = field(default="", repr=False)

    def __post_init__(self):
        if self.mode not in ("desktop", "hosted"):
            raise ValueError("invalid Helheim mode")
        if self.mode == "hosted":
            if not all((self.hosted_origin, self.hosted_issuer, self.hosted_client_id,
                        self.hosted_client_secret, self.hosted_owner_sub)):
                raise ValueError("hosted mode requires origin, issuer, client and owner")
            origin = urlsplit(self.hosted_origin)
            issuer = urlsplit(self.hosted_issuer)
            try:
                _ = origin.port, issuer.port
            except ValueError as exc:
                raise ValueError("invalid hosted URL port") from exc
            if (origin.scheme != "https" or not origin.hostname or origin.username
                    or origin.password or origin.path or origin.query or origin.fragment):
                raise ValueError("hosted origin must be a canonical HTTPS origin")
            if (issuer.scheme != "https" or not issuer.hostname
                    or issuer.username or issuer.password or issuer.query or issuer.fragment
                    or issuer.path.endswith("/")):
                raise ValueError("issuer must use HTTPS")
            destinations = {(repo.owner.casefold(), repo.name.casefold())
                            for repo in self.procedure_repositories}
            if len(destinations) != len(self.procedure_repositories):
                raise ValueError("hosted procedure repository destinations must be unique")
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

    def procedure_repository(self, key: str) -> Repository | None:
        return next((item for item in self.procedure_repositories if item.key == key), None)


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
        procedure_repositories=tuple(
            Repository(
                key=key, owner=row["owner"], name=row["name"],
                base_ref=row.get("base_ref", "main"),
                catalog_root=row.get("catalog_root", ""),
            )
            for key, row in raw.get("procedure_repositories", {}).items()
        ),
        mode=os.environ.get("HELHEIM_MODE", app.get("mode", "desktop")),
        hosted_origin=os.environ.get("HELHEIM_HOSTED_ORIGIN", ""),
        hosted_issuer=os.environ.get("HELHEIM_HOSTED_ISSUER", ""),
        hosted_client_id=os.environ.get("HELHEIM_HOSTED_CLIENT_ID", ""),
        hosted_client_secret=os.environ.get("HELHEIM_HOSTED_CLIENT_SECRET", ""),
        hosted_owner_sub=os.environ.get("HELHEIM_HOSTED_OWNER_SUB", ""),
        hosted_github_token=os.environ.get("HELHEIM_HOSTED_GITHUB_TOKEN", ""),
    )
