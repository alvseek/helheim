"""Hosted owner login. OAuth transaction data and sessions remain server-side."""

from __future__ import annotations

import secrets
import time
from urllib.parse import parse_qs, urlsplit

from authlib.integrations.starlette_client import OAuth
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from .config import Settings
from .hosted_handoff import HostedHandoff

SESSION_COOKIE = "__Host-helheim-session"
PENDING_COOKIE = "__Host-helheim-login"
MAX_ENTRIES = 256
PENDING_TTL = 300
SESSION_TTL = 8 * 60 * 60


class HostedLogin:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.pending: dict[str, tuple[float, dict]] = {}
        self.pending_starts = 0
        self.sessions: dict[str, float] = {}
        self.handoff = HostedHandoff()
        oauth = OAuth()
        self.client = oauth.register(
            name="hosted",
            client_id=settings.hosted_client_id,
            client_secret=settings.hosted_client_secret,
            server_metadata_url=f"{settings.hosted_issuer}/.well-known/openid-configuration",
            client_kwargs={"scope": "openid", "code_challenge_method": "S256"},
        )

    def _prune(self):
        now = time.monotonic()
        self.pending = {key: value for key, value in self.pending.items() if value[0] > now}
        self.sessions = {key: expiry for key, expiry in self.sessions.items() if expiry > now}

    def authorized(self, request: Request) -> bool:
        self._prune()
        return request.cookies.get(SESSION_COOKIE, "") in self.sessions

    async def begin(self, request: Request):
        self._prune()
        if len(self.pending) + self.pending_starts >= MAX_ENTRIES:
            return JSONResponse({"detail": "Too many pending logins"}, status_code=503)
        # Reserve before the first await; pending plus in-flight starts share one cap.
        self.pending_starts += 1
        try:
            metadata = await self.client.load_server_metadata()
            if metadata.get("issuer") != self.settings.hosted_issuer:
                raise ValueError("issuer mismatch")
            for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                endpoint = urlsplit(metadata.get(key, ""))
                if (not endpoint.hostname or endpoint.username or endpoint.password
                        or endpoint.fragment or endpoint.scheme != "https"):
                    raise ValueError("invalid issuer endpoint")
            transaction: dict = {}
            request.scope["session"] = transaction
            response = await self.client.authorize_redirect(
                request, self.settings.hosted_origin + "/login/callback",
                response_mode="form_post",
            )
            state = parse_qs(urlsplit(response.headers["location"]).query).get("state", [""])[0]
            self.handoff.register("login", state, metadata["authorization_endpoint"])
            handle = secrets.token_urlsafe(32)
            response.set_cookie(PENDING_COOKIE, handle, max_age=PENDING_TTL, secure=True,
                                httponly=True, samesite="lax", path="/")
            self.pending[handle] = (time.monotonic() + PENDING_TTL, transaction)
            return response
        except Exception:
            return JSONResponse({"detail": "Login unavailable"}, status_code=503)
        finally:
            self.pending_starts -= 1

    async def callback(self, request: Request):
        self._prune()
        # Consume before any await, including on error, to prevent replay and concurrent use.
        entry = self.pending.pop(request.cookies.get(PENDING_COOKIE, ""), None)
        if not entry:
            return JSONResponse({"detail": "Invalid login transaction"}, status_code=400)
        self.sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
        request.scope["session"] = entry[1]
        try:
            form = await request.form()
            if form.get("error"):
                raise ValueError("authorization refused")
            token = await self.client.authorize_access_token(
                request, claims_options={"iss": {"essential": True,
                                                  "value": self.settings.hosted_issuer}},
                leeway=0,
            )
            claims = token.get("userinfo")
            if not claims or claims.get("sub") != self.settings.hosted_owner_sub:
                raise ValueError("unauthorized identity")
        except Exception:
            response = JSONResponse({"detail": "Login refused"}, status_code=403)
            response.delete_cookie(PENDING_COOKIE, path="/", secure=True, httponly=True,
                                   samesite="lax")
            response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True,
                                   samesite="lax")
            return response
        self._prune()
        if len(self.sessions) >= MAX_ENTRIES:
            return JSONResponse({"detail": "Too many sessions"}, status_code=503)
        handle = secrets.token_urlsafe(32)
        self.sessions[handle] = time.monotonic() + SESSION_TTL
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(PENDING_COOKIE, path="/", secure=True, httponly=True,
                               samesite="lax")
        response.set_cookie(SESSION_COOKIE, handle, max_age=SESSION_TTL, secure=True,
                            httponly=True, samesite="lax", path="/")
        return response

    def logout(self, request: Request):
        self.sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True,
                               samesite="lax")
        return response
