"""Hosted MCP consent: separate, owner-bound and resource-bound."""

from __future__ import annotations

import json
import math
import os
import secrets
import stat
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
from authlib.integrations.starlette_client import OAuth
from fastapi import Request
from starlette.concurrency import run_in_threadpool

from .auth import EXPIRED, REFUSED, UNAVAILABLE, Auth, AuthError, Endpoints, Token
from .config import Server, Settings
from .hosted_login import MAX_ENTRIES, PENDING_TTL, SESSION_COOKIE, HostedLogin


def _https_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        return bool(parsed.scheme == "https" and parsed.hostname and not (
            parsed.username or parsed.password or parsed.fragment
        ))
    except (TypeError, ValueError):
        return False


class HostedMCPAuth(Auth):
    def __init__(self, settings: Settings, owner_gate: HostedLogin,
                 client: httpx.Client | None = None) -> None:
        super().__init__(settings, client)
        self._lock = threading.RLock()
        self.owner_gate = owner_gate
        self._hosted_pending: dict[str, tuple[float, str, Server, dict]] = {}
        self._starts = 0
        oauth = OAuth()
        self.oauth = oauth.register(
            name="hosted_mcp",
            client_id=settings.hosted_client_id,
            client_secret=settings.hosted_client_secret,
            server_metadata_url=f"{settings.hosted_issuer}/.well-known/openid-configuration",
            client_kwargs={"scope": "openid offline_access", "code_challenge_method": "S256"},
        )

    def discover(self, server: Server) -> Endpoints:
        cached = self._endpoints.get(server.name)
        if cached is not None:
            if cached.resource != server.resource:
                raise AuthError(REFUSED, "server resource changed; connect again")
            return cached
        if not _https_url(server.url) or not _https_url(server.resource):
            raise AuthError(REFUSED, "hosted servers require HTTPS")
        try:
            prm = self._protected_resource(server)
            issuers = prm.get("authorization_servers") if isinstance(prm, dict) else None
            if (not isinstance(issuers, list) or prm.get("resource") != server.resource
                    or self.settings.hosted_issuer not in issuers):
                raise AuthError(REFUSED, "server resource or issuer does not match configuration")
            response = self._client.get(
                self.settings.hosted_issuer + "/.well-known/openid-configuration",
                headers={"accept": "application/json"},
            )
            response.raise_for_status()
            meta = response.json()
            if (meta.get("issuer") != self.settings.hosted_issuer
                    or not all(_https_url(meta.get(key)) for key in
                               ("authorization_endpoint", "token_endpoint", "jwks_uri"))):
                raise AuthError(REFUSED, "issuer metadata does not match configuration")
        except AuthError:
            raise
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise AuthError(UNAVAILABLE, "server authorization discovery failed") from exc
        endpoints = Endpoints(
            resource=server.resource,
            authorization_endpoint=meta["authorization_endpoint"],
            token_endpoint=meta["token_endpoint"],
            scopes="openid offline_access",
        )
        self._endpoints[server.name] = endpoints
        return endpoints

    def _prune_pending(self) -> None:
        now = time.monotonic()
        self._hosted_pending = {
            state: item for state, item in self._hosted_pending.items() if item[0] > now
        }

    async def begin_hosted(self, server: Server, request: Request):
        self._prune_pending()
        session = request.cookies.get(SESSION_COOKIE, "")
        if not self.owner_gate.authorized(request):
            raise AuthError(REFUSED, "owner session required")
        if len(self._hosted_pending) + self._starts >= MAX_ENTRIES:
            raise AuthError(UNAVAILABLE, "too many pending connections")
        self._starts += 1
        try:
            endpoints = await run_in_threadpool(self.discover, server)
            meta = await self.oauth.load_server_metadata()
            if (meta.get("issuer") != self.settings.hosted_issuer
                    or meta.get("authorization_endpoint") != endpoints.authorization_endpoint
                    or meta.get("token_endpoint") != endpoints.token_endpoint
                    or not _https_url(meta.get("jwks_uri"))):
                raise AuthError(REFUSED, "issuer metadata changed")
            transaction: dict = {}
            request.scope["session"] = transaction
            response = await self.oauth.authorize_redirect(
                request, self.settings.hosted_origin + "/auth/mcp/callback",
                resource=server.resource, response_mode="form_post",
            )
            state = parse_qs(urlsplit(response.headers["location"]).query).get("state", [""])[0]
            if not state or not self.owner_gate.authorized(request):
                raise AuthError(REFUSED, "owner session expired")
            self.owner_gate.handoff.register("mcp", state, meta["authorization_endpoint"])
            self._hosted_pending[state] = (
                time.monotonic() + PENDING_TTL, session, server, transaction
            )
            return response
        except AuthError:
            raise
        except Exception as exc:
            raise AuthError(UNAVAILABLE, "connection could not be started") from exc
        finally:
            self._starts -= 1

    async def complete_hosted(self, request: Request) -> str:
        self._prune_pending()
        form = await request.form()
        state = form.get("state", "")
        item = self._hosted_pending.get(state)
        if (not item or not self.owner_gate.authorized(request)
                or not secrets.compare_digest(item[1], request.cookies.get(SESSION_COOKIE, ""))):
            raise AuthError(REFUSED, "connection transaction or owner session is invalid")
        del self._hosted_pending[state]
        _, _, server, transaction = item
        if self.settings.server(server.name) != server:
            raise AuthError(REFUSED, "server configuration changed; connect again")
        request.scope["session"] = transaction
        try:
            if form.get("error"):
                raise AuthError(REFUSED, "authorization refused")
            token = await self.oauth.authorize_access_token(
                request, resource=server.resource,
                claims_options={"iss": {"essential": True, "value": self.settings.hosted_issuer}},
                leeway=0,
            )
            claims = token.get("userinfo")
            if not claims or claims.get("sub") != self.settings.hosted_owner_sub:
                raise AuthError(REFUSED, "consenting account is not the owner")
            if not self.owner_gate.authorized(request):
                raise AuthError(REFUSED, "owner session expired")
            value = self._token_from(token, server.resource)
            # Keep the final owner check and commit together on the event loop.
            self._store(server.name, value)
        except AuthError:
            raise
        except Exception as exc:
            raise AuthError(UNAVAILABLE, "connection was not completed") from exc
        return server.name

    def _token_from(self, payload: dict, resource: str) -> Token:
        try:
            lifetime = float(payload["expires_in"])
            if (not math.isfinite(lifetime) or lifetime <= 0
                    or not isinstance(payload.get("access_token"), str)
                    or not payload["access_token"]
                    or str(payload.get("token_type", "")).lower() != "bearer"):
                raise ValueError
            refresh = payload.get("refresh_token") or ""
            if not isinstance(refresh, str):
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthError(REFUSED, "issuer returned an invalid token response") from exc
        return Token(payload["access_token"], refresh, time.time() + max(0, lifetime - 60),
                     resource, "Bearer")

    def _load(self, name: str) -> Token | None:
        token = super()._load(name)
        server = self.settings.server(name)
        if (token is None or server is None or token.resource != server.resource
                or not token.access_token or token.token_type.lower() != "bearer"):
            return None
        return token

    def _refresh(self, server: Server, token: Token) -> Token:
        if not token.refresh_token:
            raise AuthError(EXPIRED, f"{server.name} needs to be connected again")
        endpoints = self.discover(server)
        try:
            response = self._client.post(
                endpoints.token_endpoint,
                data={"grant_type": "refresh_token", "refresh_token": token.refresh_token,
                      "resource": server.resource},
                auth=(self.settings.hosted_client_id, self.settings.hosted_client_secret),
                headers={"accept": "application/json"},
            )
            if response.status_code != 200:
                raise AuthError(EXPIRED, f"{server.name} needs to be connected again")
            refreshed = self._token_from(response.json(), server.resource)
            if not refreshed.refresh_token:
                refreshed.refresh_token = token.refresh_token
            self._store(server.name, refreshed)
            return refreshed
        except AuthError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise AuthError(EXPIRED, f"{server.name} needs to be connected again") from exc

    def _read_file(self) -> dict:
        path = self.settings.home / "hosted-mcp-tokens.json"
        try:
            if not path.exists():
                return {}
            if os.name == "posix":
                directory = path.parent.stat()
                token_file = path.lstat()
                if (directory.st_uid != os.geteuid() or directory.st_mode & 0o077
                        or token_file.st_uid != os.geteuid() or token_file.st_mode & 0o077
                        or not stat.S_ISREG(token_file.st_mode)):
                    raise ValueError
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError
            return raw
        except (OSError, ValueError) as exc:
            raise AuthError(UNAVAILABLE, "hosted token storage is unavailable") from exc

    def _store(self, name: str, token: Token) -> None:
        with self._lock:
            raw = self._read_file()
            raw[name] = token.to_json()
            path = self.settings.home / "hosted-mcp-tokens.json"
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if os.name == "posix":
                directory = path.parent.stat()
                if directory.st_uid != os.geteuid() or directory.st_mode & 0o077:
                    raise AuthError(UNAVAILABLE, "hosted state directory is not private")
            temp_path = None
            try:
                fd, temp_path = tempfile.mkstemp(prefix=".helheim-tokens-", dir=path.parent)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    os.chmod(temp_path, 0o600)
                    json.dump(raw, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, path)
            finally:
                if temp_path and os.path.exists(temp_path):
                    os.unlink(temp_path)
