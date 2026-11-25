"""Bounded form-post reception; authorization remains with the original browser."""

from __future__ import annotations

import secrets
import time
from urllib.parse import parse_qsl, urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

MAX_HANDOFFS = 256
HANDOFF_TTL = 60
MAX_BODY = 8192
COOKIES = {"login": "__Host-helheim-login-handoff", "mcp": "__Host-helheim-mcp-handoff"}


class HostedHandoff:
    def __init__(self):
        self.expected = {}
        self.handoffs = {}

    def prune(self):
        now = time.monotonic()
        self.expected = {k: v for k, v in self.expected.items() if v[0] > now}
        self.handoffs = {k: v for k, v in self.handoffs.items() if v[0] > now}

    def register(self, kind, state, authorization_endpoint):
        self.prune()
        parsed = urlsplit(authorization_endpoint)
        if (not state or parsed.scheme != "https" or not parsed.hostname
                or parsed.username or parsed.password or len(self.expected) >= MAX_HANDOFFS):
            raise ValueError("invalid authorization transaction")
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if state in self.expected:
            raise ValueError("duplicate authorization state")
        self.expected[state] = (time.monotonic() + 300, kind, origin)

    async def receive(self, kind: str, request: Request):
        self.prune()
        if request.scope.get("query_string") or request.headers.get(
            "content-type", ""
        ).split(";")[0].strip().lower() != "application/x-www-form-urlencoded":
            return JSONResponse({"detail": "Invalid authorization response"}, status_code=400)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                return JSONResponse({"detail": "Invalid authorization response"}, status_code=400)
        try:
            fields = parse_qsl(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True,
                               max_num_fields=12)
            values = dict(fields)
            if (len(values) != len(fields) or not values.get("state")
                    or bool(values.get("code")) == bool(values.get("error"))):
                raise ValueError
        except (UnicodeError, ValueError):
            return JSONResponse({"detail": "Invalid authorization response"}, status_code=400)
        entry = self.expected.get(values["state"])
        if (not entry or entry[1] != kind or request.headers.get("origin") != entry[2]
                or len(self.handoffs) >= MAX_HANDOFFS):
            return JSONResponse({"detail": "Invalid authorization response"}, status_code=400)
        del self.expected[values["state"]]
        handle = secrets.token_urlsafe(32)
        self.handoffs[handle] = (time.monotonic() + HANDOFF_TTL, kind, bytes(body))
        response = RedirectResponse(f"/auth/{kind}/complete", status_code=303)
        response.set_cookie(COOKIES[kind], handle, max_age=HANDOFF_TTL, secure=True,
                            httponly=True, samesite="lax", path="/")
        return response

    def consume(self, kind: str, request: Request):
        self.prune()
        entry = self.handoffs.pop(request.cookies.get(COOKIES[kind], ""), None)
        if not entry or entry[1] != kind or request.scope.get("query_string"):
            return None
        scope = dict(request.scope)
        scope["method"] = "POST"
        scope["query_string"] = b""
        scope["headers"] = [(k, v) for k, v in scope["headers"]
                            if k.lower() not in (b"content-type", b"content-length")]
        scope["headers"].append((b"content-type", b"application/x-www-form-urlencoded"))
        body = entry[2]

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        return Request(scope, receive)
