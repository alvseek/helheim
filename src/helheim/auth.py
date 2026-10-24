"""OAuth for one server at a time.

This module never learns what MCP is. It turns a server name into a bearer token
and keeps that token usable: discovery comes from the server's own metadata, the
flow is authorization code with PKCE over a loopback redirect, and once the code
is exchanged the refresh token lives in a local file so a session survives a
restart.

The tenant has no registration endpoint, so ``client_id`` is configured by hand.
That is the one thing this module cannot discover.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from urllib.parse import urlencode, urlparse

import httpx

from .config import Server, Settings

OK = "ok"
MISSING = "missing"
EXPIRED = "expired"
REFUSED = "refused"
UNAVAILABLE = "unavailable"

#: Refresh a little before the token actually dies, so a call never races it.
EXPIRY_MARGIN_SECONDS = 60


class AuthError(Exception):
    """A login or token problem a page can explain in one sentence."""

    def __init__(self, status: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class Token:
    access_token: str
    refresh_token: str
    expires_at: float
    resource: str
    token_type: str = "Bearer"

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: dict) -> Token:
        return cls(
            access_token=raw.get("access_token", ""),
            refresh_token=raw.get("refresh_token", ""),
            expires_at=float(raw.get("expires_at", 0)),
            resource=raw.get("resource", ""),
            token_type=raw.get("token_type", "Bearer"),
        )


@dataclass(frozen=True)
class Endpoints:
    """What one server's metadata says about logging into it."""

    resource: str
    authorization_endpoint: str
    token_endpoint: str
    scopes: str


def pkce_challenge(verifier: str) -> str:
    """RFC 7636 S256: base64url of the SHA-256 digest, unpadded."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class Auth:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self._client = client or httpx.Client(timeout=20.0, follow_redirects=False)
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[str, str]] = {}
        self._endpoints: dict[str, Endpoints] = {}

    # ---------------------------------------------------------------- discovery
    def discover(self, server: Server) -> Endpoints:
        cached = self._endpoints.get(server.name)
        if cached is not None:
            return cached

        prm = self._protected_resource(server)
        servers = prm.get("authorization_servers") or []
        if not servers:
            raise AuthError(UNAVAILABLE, f"{server.name} names no authorization server")
        issuer = servers[0].rstrip("/")

        response = self._client.get(
            f"{issuer}/.well-known/openid-configuration",
            headers={"accept": "application/json"},
        )
        if response.status_code != 200:
            raise AuthError(
                UNAVAILABLE,
                f"{issuer} did not answer its openid-configuration ({response.status_code})",
            )
        meta = response.json()

        wanted = {"openid", "offline_access"}
        offered = [scope for scope in prm.get("scopes_supported", []) if scope in wanted]
        endpoints = Endpoints(
            resource=prm.get("resource", server.resource),
            authorization_endpoint=meta["authorization_endpoint"],
            token_endpoint=meta["token_endpoint"],
            scopes=" ".join(offered) or "openid offline_access",
        )
        self._endpoints[server.name] = endpoints
        return endpoints

    def _protected_resource(self, server: Server) -> dict:
        parsed = urlparse(server.url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path.rstrip("/")
        candidates = [f"{base}/.well-known/oauth-protected-resource{path}"]
        candidates.append(f"{base}/.well-known/oauth-protected-resource")

        for url in candidates:
            try:
                response = self._client.get(url, headers={"accept": "application/json"})
            except httpx.HTTPError as exc:
                raise AuthError(UNAVAILABLE, f"{server.name} could not be reached: {exc}") from exc
            if response.status_code == 200:
                return response.json()
        raise AuthError(
            UNAVAILABLE,
            f"{server.name} published no protected-resource metadata",
        )

    # ------------------------------------------------------------ authorization
    def begin(self, server: Server) -> str:
        """Return the authorization URL to send the browser to."""
        if not self.settings.client_id:
            raise AuthError(MISSING, "no client id is configured, so nothing can be authorized")
        endpoints = self.discover(server)
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(24)
        self._pending[state] = (server.name, verifier)

        params = {
            "response_type": "code",
            "client_id": self.settings.client_id,
            "redirect_uri": self.settings.redirect_uri,
            "scope": endpoints.scopes,
            "state": state,
            "code_challenge": pkce_challenge(verifier),
            "code_challenge_method": "S256",
            "resource": endpoints.resource,
        }
        return f"{endpoints.authorization_endpoint}?{urlencode(params)}"

    def complete(self, code: str, state: str) -> str:
        """Exchange the code and store the token. Returns the server name."""
        pending = self._pending.pop(state, None)
        if pending is None:
            raise AuthError(MISSING, "this sign-in was not started here, or it was already used")
        name, verifier = pending
        server = self.settings.server(name)
        if server is None:
            raise AuthError(UNAVAILABLE, f"{name} is no longer configured")
        endpoints = self.discover(server)

        response = self._client.post(
            endpoints.token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self.settings.client_id,
                "redirect_uri": self.settings.redirect_uri,
                "code_verifier": verifier,
                "resource": endpoints.resource,
            },
            headers={"accept": "application/json"},
        )
        if response.status_code != 200:
            raise self._error(response)
        self._store(name, self._token_from(response.json(), endpoints.resource))
        return name

    # -------------------------------------------------------------------- tokens
    def token(self, server: Server) -> str:
        """A usable access token, refreshed if needed."""
        with self._lock:
            token = self._load(server.name)
            if token is None:
                raise AuthError(MISSING, f"{server.name} has not been connected yet")
            if not token.expired:
                return token.access_token
            return self._refresh(server, token).access_token

    def status(self, server: Server) -> str:
        token = self._load(server.name)
        if token is None:
            return MISSING
        if not token.expired:
            return OK
        try:
            self.token(server)
        except AuthError as exc:
            return exc.status
        except httpx.HTTPError:
            return UNAVAILABLE
        return OK

    def _refresh(self, server: Server, token: Token) -> Token:
        if not token.refresh_token:
            raise AuthError(EXPIRED, f"{server.name} has no refresh token; connect it again")
        endpoints = self.discover(server)
        response = self._client.post(
            endpoints.token_endpoint,
            data={
                "grant_type": "refresh_token",
                "refresh_token": token.refresh_token,
                "client_id": self.settings.client_id,
                "resource": endpoints.resource,
            },
            headers={"accept": "application/json"},
        )
        if response.status_code != 200:
            error = self._error(response)
            if error.status == REFUSED:
                raise error
            raise AuthError(EXPIRED, f"{server.name} needs to be connected again: {error.detail}")
        refreshed = self._token_from(response.json(), endpoints.resource)
        if not refreshed.refresh_token:
            refreshed.refresh_token = token.refresh_token
        self._store(server.name, refreshed)
        return refreshed

    def _token_from(self, payload: dict, resource: str) -> Token:
        expires_in = float(payload.get("expires_in", 3600))
        return Token(
            access_token=payload.get("access_token", ""),
            refresh_token=payload.get("refresh_token", ""),
            expires_at=time.time() + max(expires_in - EXPIRY_MARGIN_SECONDS, 30),
            resource=resource,
            token_type=payload.get("token_type", "Bearer"),
        )

    def _error(self, response: httpx.Response) -> AuthError:
        try:
            body = response.json()
        except json.JSONDecodeError:
            body = {}
        code = body.get("error", "")
        detail = body.get("error_description") or code or f"HTTP {response.status_code}"
        if code == "invalid_target":
            return AuthError(REFUSED, f"the tenant refused this resource: {detail}")
        return AuthError(EXPIRED, detail)

    # ---------------------------------------------------------------- token file
    def _load(self, name: str) -> Token | None:
        raw = self._read_file()
        row = raw.get(name)
        return Token.from_json(row) if row else None

    def _store(self, name: str, token: Token) -> None:
        raw = self._read_file()
        raw[name] = token.to_json()
        path = self.settings.tokens_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def _read_file(self) -> dict:
        path = self.settings.tokens_path
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
