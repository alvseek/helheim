"""Procedure and memory pages. New saves do not pass through inspector history."""

from __future__ import annotations

import difflib
import json
from dataclasses import asdict
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from procedure_composer import CompositionError
from procedure_composer import __version__ as composer_version
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from . import mcp_client
from .auth import AuthError
from .config import Settings
from .github import GitConflict, GitError, GitHub, GitUncertain
from .memory_editor import MemoryEditError, MemoryEditor
from .workspaces import Workspaces, WorkspaceStore


class EditorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewWorkspace(EditorInput):
    repository: str


class SourceDraft(EditorInput):
    procedure: str
    backend: Literal["markdown", "db"]
    document: str
    draft: str
    expected_head: str
    expected_document_hash: str


class MemoryDraft(EditorInput):
    action: Literal["body", "metadata"]
    draft: Any
    expected_revision: str


def difference(before: str, after: str):
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile="saved", tofile="draft",
    ))


def github_url(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if (parsed.scheme == "https" and parsed.hostname == "github.com"
            and not parsed.username and not parsed.password):
        return value
    return None


def register_editors(app: FastAPI, settings: Settings, templates):
    if settings.mode == "hosted":
        def adapter(repository):
            return GitHub(repository, credential_source=lambda: settings.hosted_github_token)
    else:
        adapter = GitHub
    app.state.workspaces = Workspaces(WorkspaceStore(settings.db_path),
                                      settings.procedure_repositories, adapter=adapter)

    def server(name):
        found = settings.server(name)
        if found is None:
            raise HTTPException(404, "no such configured memory server")
        return found

    async def tools(name):
        remote = server(name)
        return await mcp_client.list_tools(remote, app.state.auth.token(remote))

    async def call(name, tool, arguments):
        remote = server(name)
        return await mcp_client.call_tool(remote, app.state.auth.token(remote), tool, arguments)

    app.state.memory_editor = MemoryEditor(tools, call)
    templates.env.filters["github_url"] = github_url

    async def failure(request: Request, exc):
        status = 409 if isinstance(exc, (GitConflict, GitUncertain)) else 400
        if isinstance(exc, (AuthError, mcp_client.ClientError)):
            status = 502
        detail = getattr(exc, "detail", str(exc))[:1000]
        if request.method == "GET":
            return templates.TemplateResponse(
                request, "error.html",
                {"title": "Editor unavailable", "detail": detail}, status_code=status,
            )
        return JSONResponse(
            {"error": detail, "uncertain": isinstance(exc, GitUncertain)}, status_code=status
        )

    for error in (GitError, CompositionError, MemoryEditError, AuthError, mcp_client.ClientError):
        app.add_exception_handler(error, failure)

    @app.get("/procedures")
    def procedures(request: Request):
        return templates.TemplateResponse(request, "procedures.html", {
            "repositories": settings.procedure_repositories,
            "workspaces": app.state.workspaces.store.list(),
        })

    @app.post("/procedures/workspaces")
    async def new_workspace(body: NewWorkspace):
        state = await run_in_threadpool(app.state.workspaces.create, body.repository)
        return {"redirect": f"/procedures/{state['id']}"}

    @app.get("/procedures/{identity}")
    async def procedure(
        identity: str, request: Request, procedure: str | None = None,
        backend: Literal["markdown", "db"] = "db",
        revision: Literal["branch", "base"] = "branch",
        document: str | None = None, span: str | None = None, document_path: str | None = None,
    ):
        view = await run_in_threadpool(
            app.state.workspaces.preview, identity, procedure, backend, document or None,
            revision=revision, allow_missing_document=True, expected_document_path=document_path,
        )
        original = view["original"]
        init = None
        if original and revision == "branch":
            init = {
                "kind": "source", "id": identity, "procedure": view["procedure"],
                "backend": backend, "document_id": original.id,
                "document_kind": original.kind, "document_path": original.path,
                "span": span,
                "head": view["head"], "document_hash": original.sha256,
                "original": view["original_text"],
                "pending": bool(view["workspace"]["pending_action"]
                                or view["workspace"]["pending_pr"]),
            }
        return templates.TemplateResponse(request, "procedure.html", {
            **view, "backend": backend, "span": span,
            "document_unavailable": bool(document) and original is None,
            "composer_version": composer_version, "init": init,
        })

    @app.post("/procedures/{identity}/preview")
    async def preview_source(identity: str, body: SourceDraft):
        view = await run_in_threadpool(
            app.state.workspaces.preview, identity, body.procedure, body.backend,
            body.document, body.draft,
        )
        original = view["original"]
        if (view["head"] != body.expected_head
                or original.sha256 != body.expected_document_hash):
            raise GitConflict(
                "document changed; draft retained, open the latest source in a new tab"
            )
        return {
            "rendered": {backend: result.text for backend, result in view["rendered"].items()},
            "validation_errors": view["validation_errors"],
            "diff": difference(view["original_text"], body.draft),
            "spans": {backend: [asdict(span) for span in result.spans]
                      for backend, result in view["rendered"].items()},
            "documents": view["documents"],
            "dependents": list(view["dependents"]),
            "applicability": list(view["applicability"]),
        }

    @app.post("/procedures/{identity}/save")
    async def save_source(identity: str, body: SourceDraft):
        return await run_in_threadpool(
            app.state.workspaces.save, identity, body.procedure, body.backend, body.document,
            body.draft, body.expected_head, body.expected_document_hash,
        )

    @app.post("/procedures/{identity}/retry-pr")
    async def retry_pull(identity: str):
        return await run_in_threadpool(app.state.workspaces.retry_pull, identity)

    @app.post("/procedures/{identity}/reconcile")
    async def reconcile(identity: str):
        state = await run_in_threadpool(app.state.workspaces.reconcile, identity)
        return {"workspace": state, "notice": "Outcome checked. Open the latest source in a new "
                "tab before another save; your draft stays here."}

    @app.get("/memory")
    def memory_servers(request: Request):
        return templates.TemplateResponse(
            request, "memory_servers.html", {"servers": settings.servers}
        )

    @app.get("/memory/{name}")
    async def memory_list(
        name: str, request: Request, agent_id: str | None = None,
        record_type: str | None = None, project: str | None = None,
    ):
        server(name)
        filters = {key: value for key, value in {
            "agent_id": agent_id, "record_type": record_type, "project": project,
        }.items() if value}
        rows = await app.state.memory_editor.browse(name, **filters)
        return templates.TemplateResponse(request, "memory_list.html", {
            "server": name, "rows": rows, "filters": filters,
        })

    @app.get("/memory/{name}/open")
    async def open_memory(name: str, uuid: str, request: Request):
        opened = await app.state.memory_editor.open(request.state.editor_owner, name, uuid)
        init = {
            "kind": "memory", "id": opened.id, "context": opened.context,
            "body": opened.body, "metadata": opened.metadata,
        }
        return templates.TemplateResponse(request, "memory_editor.html", {
            "editing": opened, "init": init, "metadata_text": json.dumps(
                opened.metadata, indent=2, ensure_ascii=False,
            ),
        })

    @app.post("/memory-edit/{identity}/preview")
    async def preview_memory(identity: str, body: MemoryDraft, request: Request):
        opened = app.state.memory_editor.get(identity, request.state.editor_owner)
        if opened.context["revision"] != body.expected_revision:
            raise MemoryEditError(
                "editor changed; draft retained, open the latest record in a new tab"
            )
        if body.action == "body":
            if not isinstance(body.draft, str):
                raise MemoryEditError("body must be text")
            before, after = opened.body, body.draft
        else:
            before = json.dumps(opened.metadata, indent=2, ensure_ascii=False)
            after = json.dumps(body.draft, indent=2, ensure_ascii=False)
        return {"preview": after, "diff": difference(before, after)}

    @app.post("/memory-edit/{identity}/save")
    async def save_memory(identity: str, body: MemoryDraft, request: Request):
        return await app.state.memory_editor.save(
            identity, request.state.editor_owner, body.action, body.draft, body.expected_revision,
        )

    @app.post("/memory-edit/{identity}/close")
    async def close_memory(identity: str, request: Request):
        editor = app.state.memory_editor
        editor.close(identity, request.state.editor_owner)
        return {"closed": True}
