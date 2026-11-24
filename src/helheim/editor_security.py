"""Loopback-only browser authority, including the inspector's sibling write route."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from .config import Settings

COOKIE = "helheim-editor"
CSRF_FIELD_PREFIX = "__helheim_csrf"
_OWNER = re.compile(r"[A-Za-z0-9_-]{43}\Z")


def csrf_field_name(fields):
    names = {field.name for field in fields}
    candidate = CSRF_FIELD_PREFIX
    while candidate in names:
        candidate += "_"
    return candidate


class EditorSecurity:
    def __init__(self, settings: Settings):
        host = settings.host
        if host != "localhost":
            try:
                safe = ipaddress.ip_address(host).is_loopback
            except ValueError:
                safe = False
            if not safe:
                raise ValueError("Helheim editing requires a loopback bind, not a public interface")
        self.port = settings.port
        self.hosts = {"localhost", "127.0.0.1", "::1", host}
        self.secret = secrets.token_bytes(32)

    def prepare(self, request: Request):
        try:
            authority = urlsplit("http://" + request.headers.get("host", ""))
            host = authority.hostname
            port = authority.port or 80
        except ValueError as exc:
            raise HTTPException(403, "invalid local Host") from exc
        if (host not in self.hosts or port != self.port or authority.username
                or authority.password or authority.path or authority.query or authority.fragment):
            raise HTTPException(403, "Host must name this loopback Helheim instance")
        if (request.method in ("GET", "HEAD")
                and request.url.path.startswith(("/memory", "/procedures"))
                and request.headers.get("sec-fetch-site") == "cross-site"):
            raise HTTPException(403, "open private editor views through Helheim's own navigation")
        owner = request.cookies.get(COOKIE, "")
        request.state.new_editor_cookie = not bool(_OWNER.fullmatch(owner))
        if request.state.new_editor_cookie:
            owner = secrets.token_urlsafe(32)
        request.state.editor_owner = owner
        request.state.csrf = hmac.new(
            self.secret, owner.encode("ascii"), hashlib.sha256
        ).hexdigest()

    async def require_write(self, request: Request):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return
        expected_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
        if request.headers.get("origin") != expected_origin:
            raise HTTPException(403, "write requires the same local Origin")
        if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
            raise HTTPException(403, "cross-site writes are refused")
        token = request.headers.get("x-csrf-token")
        candidates = [token] if token is not None else []
        if token is None and request.headers.get("content-type", "").startswith(
            ("application/x-www-form-urlencoded", "multipart/form-data")
        ):
            form = await request.form()
            candidates = [value for name, value in form.multi_items()
                          if name.startswith(CSRF_FIELD_PREFIX) and isinstance(value, str)]
        expected = request.state.csrf.encode("ascii")
        if not any(value and secrets.compare_digest(value.encode("utf-8"), expected)
                   for value in candidates):
            raise HTTPException(403, "missing or stale CSRF token; reload this page")
