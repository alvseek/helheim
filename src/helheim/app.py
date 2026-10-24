"""The web shell.

Routes hold no protocol knowledge: they ask ``auth``, ``mcp_client`` and
``store`` for facts and hand them to a template.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import Settings, load_settings

PACKAGE_DIR = Path(__file__).parent


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="Helheim")
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    app.state.templates = templates

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "ok": True,
            "servers": len(settings.servers),
            "client_configured": bool(settings.client_id),
        }

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        rows = [
            {"server": server, "status": "missing"}
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

    return app


app = create_app()
