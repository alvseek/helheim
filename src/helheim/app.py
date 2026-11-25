"""The web shell.

Routes hold no protocol knowledge: they ask ``auth``, ``mcp_client`` and
``store`` for facts and hand them to a template.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from requests.exceptions import RequestException
from starlette.concurrency import run_in_threadpool

from . import mcp_client
from .auth import Auth, AuthError
from .config import Server, Settings, load_settings
from .editor_security import COOKIE, CSRF_FIELD_PREFIX, EditorSecurity, csrf_field_name
from .editors import register_editors
from .forms import arguments_from_form, fields_from_schema
from .hosted_login import HostedLogin
from .hosted_mcp_auth import HostedMCPAuth
from .store import CallRecord, Store
from .tokens import TokenCounter

PACKAGE_DIR = Path(__file__).parent

#: Above this size the page shows a summary and the raw envelope instead of a
#: fully expanded tree, because a multi-megabyte DOM is slower than the call it
#: describes.
RENDER_LIMIT_BYTES = 2 * 1024 * 1024


class RejectHostedWebSockets:
    """No WebSocket surface is currently supported in hosted mode."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await self.app(scope, receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    security = EditorSecurity(settings)
    hosted = HostedLogin(settings) if settings.mode == "hosted" else None
    app = FastAPI(
        title="Helheim", dependencies=[Depends(security.require_write)],
        docs_url=None if hosted else "/docs", redoc_url=None if hosted else "/redoc",
        openapi_url=None if hosted else "/openapi.json",
    )
    if hosted:
        app.add_middleware(RejectHostedWebSockets)
    app.state.settings = settings
    app.state.auth = HostedMCPAuth(settings, hosted) if hosted else Auth(settings)
    app.state.store = Store(settings.db_path, settings.max_store_bytes)
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    app.state.templates = templates
    templates.env.globals["csrf_field_name"] = csrf_field_name

    @app.middleware("http")
    async def local_browser(request: Request, call_next):
        try:
            security.prepare(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        if hosted:
            path = request.scope["path"]
            public = ((request.method == "GET" and path in (
                "/login", "/health", "/auth/login/complete"))
                or (request.method == "POST" and path in (
                    "/login/callback", "/auth/mcp/callback")))
            if not public and not hosted.authorized(request):
                if request.method in ("GET", "HEAD"):
                    return RedirectResponse("/login", status_code=303)
                return JSONResponse({"detail": "Sign in required"}, status_code=401)
        response = await call_next(request)
        cross_site = request.headers.get("sec-fetch-site") == "cross-site"
        confirmed_callback = request.url.path == "/callback" and response.status_code == 303
        if (request.state.new_editor_cookie and request.method in ("GET", "HEAD")
                and (not cross_site or confirmed_callback)):
            response.set_cookie(
                COOKIE, request.state.editor_owner, httponly=True, samesite="strict",
                secure=bool(hosted) or request.url.scheme == "https",
            )
        response.headers["X-Frame-Options"] = "DENY"
        # Native form POSTs can carry Origin: null under no-referrer, breaking
        # the strict same-origin write guard. This preserves their local origin
        # while still withholding referrers from external sites.
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        if request.url.path.startswith(("/procedures", "/memory")):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
            )
        return response

    def need_server(name: str) -> Server:
        server = settings.server(name)
        if server is None:
            raise HTTPException(status_code=404, detail=f"no server called {name}")
        return server

    async def load_tools(server: Server) -> list[mcp_client.ToolInfo]:
        token = app.state.auth.token(server)
        return await mcp_client.list_tools(server, token)

    def problem_page(request: Request, title: str, detail: str, status_code: int = 400):
        return templates.TemplateResponse(
            request,
            "error.html",
            {"title": title, "detail": detail},
            status_code=status_code,
        )

    @app.get("/health")
    def health() -> dict[str, object]:
        if hosted:
            return {"ok": True}
        return {
            "ok": True,
            "servers": len(settings.servers),
            "client_configured": bool(settings.client_id),
            "stored_bytes": app.state.store.total_bytes(),
        }

    if hosted:
        @app.get("/login")
        async def login(request: Request):
            return await hosted.begin(request)

        @app.post("/login/callback")
        async def login_callback(request: Request):
            return await hosted.handoff.receive("login", request)

        @app.get("/auth/login/complete")
        async def login_complete(request: Request):
            staged = hosted.handoff.consume("login", request)
            if staged is None:
                return JSONResponse({"detail": "Invalid login transaction"}, status_code=400)
            return await hosted.callback(staged)

        @app.post("/logout")
        def logout(request: Request):
            return hosted.logout(request)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        rows = [
            {"server": server, "status": app.state.auth.status(server)}
            for server in settings.servers
        ]
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "rows": rows,
                "settings": settings,
                "client_configured": bool(
                    settings.hosted_client_id if hosted else settings.client_id
                ),
            },
        )

    @app.get("/auth/{name}/start")
    async def start(name: str, request: Request):
        server = need_server(name)
        try:
            if hosted:
                return await app.state.auth.begin_hosted(server, request)
            return RedirectResponse(await run_in_threadpool(app.state.auth.begin, server))
        except AuthError as exc:
            return problem_page(
                request, f"Could not start a login for {name}", exc.detail, 502
            )

    @app.get("/callback")
    def callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
        error_description: str | None = None,
    ):
        if hosted:
            raise HTTPException(status_code=404, detail="not found")
        if error:
            return problem_page(
                request, "The tenant refused the login", error_description or error
            )
        if not code or not state:
            return problem_page(
                request, "The callback arrived without a code", "nothing to exchange"
            )
        try:
            app.state.auth.complete(code, state)
        except AuthError as exc:
            return problem_page(request, "The login did not complete", exc.detail)
        return RedirectResponse("/", status_code=303)

    if hosted:
        @app.post("/auth/mcp/callback")
        async def hosted_mcp_callback(request: Request):
            return await hosted.handoff.receive("mcp", request)

        @app.get("/auth/mcp/complete")
        async def hosted_mcp_complete(request: Request):
            staged = hosted.handoff.consume("mcp", request)
            if staged is None:
                return JSONResponse({"detail": "Invalid connection transaction"}, status_code=400)
            try:
                await app.state.auth.complete_hosted(staged)
            except AuthError as exc:
                return problem_page(request, "The connection did not complete", exc.detail)
            return RedirectResponse("/", status_code=303)

    @app.get("/s/{name}", response_class=HTMLResponse)
    async def server_page(name: str, request: Request):
        server = need_server(name)
        try:
            tools = await load_tools(server)
        except AuthError as exc:
            status = exc.status
        except mcp_client.ClientError as exc:
            status = exc.status
        else:
            return templates.TemplateResponse(
                request,
                "tools.html",
                {"server": server, "tools": tools, "status": "ok"},
            )
        return templates.TemplateResponse(
            request,
            "tools.html",
            {"server": server, "tools": [], "status": status},
            status_code=200,
        )

    @app.get("/s/{name}/t/{tool}", response_class=HTMLResponse)
    async def tool_form(name: str, tool: str, request: Request):
        server = need_server(name)
        try:
            tools = await load_tools(server)
        except AuthError as exc:
            return problem_page(request, f"{name} is not usable yet", exc.detail, 409)
        except mcp_client.ClientError as exc:
            return problem_page(request, f"{name} did not answer", exc.detail, 502)
        found = next((item for item in tools if item.name == tool), None)
        if found is None:
            raise HTTPException(status_code=404, detail=f"{name} has no tool called {tool}")
        preset = {key: value for key, value in request.query_params.items()}
        return templates.TemplateResponse(
            request,
            "form.html",
            {
                "server": server,
                "tool": found,
                "fields": fields_from_schema(found.input_schema),
                "values": preset,
                "errors": [],
            },
        )

    @app.post("/s/{name}/t/{tool}")
    async def run_tool(name: str, tool: str, request: Request):
        server = need_server(name)
        form = await request.form()
        submitted = {
            key: str(value) for key, value in form.items()
            if not (key.startswith(CSRF_FIELD_PREFIX) and str(value) == request.state.csrf)
        }
        try:
            tools = await load_tools(server)
        except AuthError as exc:
            return problem_page(request, f"{name} is not usable yet", exc.detail, 409)
        except mcp_client.ClientError as exc:
            return problem_page(request, f"{name} did not answer", exc.detail, 502)
        found = next((item for item in tools if item.name == tool), None)
        if found is None:
            raise HTTPException(status_code=404, detail=f"{name} has no tool called {tool}")

        fields = fields_from_schema(found.input_schema)
        arguments, errors = arguments_from_form(fields, submitted)
        if errors:
            return templates.TemplateResponse(
                request,
                "form.html",
                {
                    "server": server,
                    "tool": found,
                    "fields": fields,
                    "values": submitted,
                    "errors": errors,
                },
                status_code=400,
            )

        try:
            token = app.state.auth.token(server)
            outcome = await mcp_client.call_tool(server, token, tool, arguments)
        except AuthError as exc:
            return problem_page(request, f"{name} is not usable yet", exc.detail, 409)
        except mcp_client.ClientError as exc:
            return problem_page(request, f"{tool} did not complete", exc.detail, 502)

        record = CallRecord(
            server=name,
            tool=tool,
            arguments=arguments,
            response_text=outcome.text,
            payload=outcome.payload,
            envelope=outcome.envelope,
            duration_ms=outcome.duration_ms,
            outcome="error" if outcome.is_error else "ok",
            is_error=outcome.is_error,
        )
        call_id = app.state.store.insert(record)
        return RedirectResponse(f"/calls/{call_id}", status_code=303)

    @app.get("/calls/{call_id}", response_class=HTMLResponse)
    def reopen(call_id: int, request: Request):
        found = app.state.store.get(call_id)
        if found is None:
            raise HTTPException(status_code=404, detail="no such call")
        try:
            counter = TokenCounter()
            response_tokens = counter.text(found["response_text"])
        except (OSError, ValueError, RequestException):
            # An unavailable vocabulary must not make stored responses unreadable.
            counter = None
            response_tokens = None
        return templates.TemplateResponse(
            request,
            "call.html",
            {
                "call": found,
                "too_big": found["res_bytes"] > RENDER_LIMIT_BYTES,
                "counter": counter,
                "response_tokens": response_tokens,
            },
        )

    @app.get("/calls/{call_id}/replay", response_class=HTMLResponse)
    async def replay(call_id: int, request: Request):
        found = app.state.store.get(call_id)
        if found is None:
            raise HTTPException(status_code=404, detail="no such call")
        server = need_server(str(found["server"]))
        try:
            tools = await load_tools(server)
        except (AuthError, mcp_client.ClientError) as exc:
            return problem_page(request, f"{server.name} is not usable yet", exc.detail, 409)
        tool = next((item for item in tools if item.name == found["tool"]), None)
        if tool is None:
            raise HTTPException(
                status_code=404, detail=f"{server.name} no longer offers {found['tool']}"
            )
        fields = fields_from_schema(tool.input_schema)
        values = {
            key: (json.dumps(value) if isinstance(value, (dict, list)) else str(value))
            for key, value in found["arguments"].items()
        }
        return templates.TemplateResponse(
            request,
            "form.html",
            {
                "server": server,
                "tool": tool,
                "fields": fields,
                "values": values,
                "errors": [],
                "replay_of": call_id,
            },
        )

    @app.get("/history", response_class=HTMLResponse)
    def history(request: Request):
        return templates.TemplateResponse(
            request,
            "history.html",
            {
                "calls": app.state.store.list(),
                "total_bytes": app.state.store.total_bytes(),
                "max_bytes": settings.max_store_bytes,
            },
        )

    register_editors(app, settings, templates)
    return app
