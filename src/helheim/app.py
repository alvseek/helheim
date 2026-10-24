"""The web shell.

Routes hold no protocol knowledge: they ask ``auth``, ``mcp_client`` and
``store`` for facts and hand them to a template.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import mcp_client
from .auth import Auth, AuthError
from .config import Server, Settings, load_settings
from .forms import arguments_from_form, fields_from_schema
from .store import CallRecord, Store

PACKAGE_DIR = Path(__file__).parent


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="Helheim")
    app.state.settings = settings
    app.state.auth = Auth(settings)
    app.state.store = Store(settings.db_path, settings.max_store_bytes)
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    app.state.templates = templates

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
        return {
            "ok": True,
            "servers": len(settings.servers),
            "client_configured": bool(settings.client_id),
            "stored_bytes": app.state.store.total_bytes(),
        }

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
                "client_configured": bool(settings.client_id),
            },
        )

    @app.get("/auth/{name}/start")
    def start(name: str, request: Request):
        server = need_server(name)
        try:
            return RedirectResponse(app.state.auth.begin(server))
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
        return templates.TemplateResponse(
            request,
            "form.html",
            {
                "server": server,
                "tool": found,
                "fields": fields_from_schema(found.input_schema),
                "values": {},
                "errors": [],
            },
        )

    @app.post("/s/{name}/t/{tool}")
    async def run_tool(name: str, tool: str, request: Request):
        server = need_server(name)
        form = await request.form()
        submitted = {key: str(value) for key, value in form.items()}
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
        return templates.TemplateResponse(request, "call.html", {"call": found})

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

    return app


app = create_app()
