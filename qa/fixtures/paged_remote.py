# fixture: an upstream paged memory read
# produces: real MCP transport carrying captured synthetic Munnin responses
# fidelity: rung 1 reuse-snapshot; named faults are explicit test perturbations
# fidelity-checked: 2026-11-19, capture_paged.py compared actual HTTP/MCP responses
# teardown: nonce-authenticated stop; caller removes its exact owned temporary root
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import socket
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse

from qa.bench import owned, state_path, write

MODES = (
    "valid", "revision", "metadata", "tenant", "wrong-uuid", "missing-context",
    "missing-edit-body", "bad-hash", "page-hash", "bad-length", "gap", "overlap",
    "repeat-cursor", "interrupt", "pause-revision", "pause-metadata", "pause-body",
    "pause-tenant",
)


class World:
    def __init__(self, seed):
        self.seed = seed
        self.set_mode("valid")

    def set_mode(self, mode):
        if mode not in MODES:
            raise ValueError("Unknown controlled fault")
        self.mode = mode
        self.calls = []
        self.writes = 0
        self.waiting = asyncio.Event()
        self.resume = asyncio.Event()

    async def get(self, cursor):
        self.calls.append({"tool": "get", "cursor": cursor})
        index = 0 if cursor is None else 1
        if cursor is not None and cursor != self.seed["pages"][0]["next_cursor"]:
            raise ValueError("Unexpected cursor")
        row = copy.deepcopy(self.seed["pages"][index])
        mode = self.mode
        if index and mode.startswith("pause-"):
            self.waiting.set()
            await self.resume.wait()
            mode = mode.removeprefix("pause-")
        if mode == "missing-edit-body":
            row.pop("edit_body")
        elif mode == "bad-hash":
            row["edit_body"]["sha256"] = "0" * 64
        elif mode == "bad-length":
            row["edit_body"]["characters"] += 1
        if index:
            if mode == "revision":
                row["edit_context"]["revision"] += "-changed"
            elif mode == "body":
                # Same length/provenance, different bytes: only the exact-body hash catches it.
                row["content"] = row["content"].replace("Middle", "M!ddle")
            elif mode == "metadata":
                row["title"] += " changed"
            elif mode == "tenant":
                row["edit_context"]["tenant"] = "changed-fixture-account"
            elif mode == "wrong-uuid":
                row["uuid"] = "f" * 32
                row["edit_context"]["uuid"] = row["uuid"]
            elif mode == "missing-context":
                row.pop("edit_context")
            elif mode == "page-hash":
                row["edit_body"]["sha256"] = "0" * 64
            elif mode == "gap":
                row["edit_body"]["segments"][0]["source_start"] += 1
            elif mode == "overlap":
                row["edit_body"]["segments"][0]["source_start"] -= 1
            elif mode == "repeat-cursor":
                row["next_cursor"] = self.seed["pages"][0]["next_cursor"]
        return row


def app_for(seed, owner):
    world = World(seed)
    mcp = MCPServer("Owned captured-memory fixture", log_level="WARNING")

    @mcp.tool()
    async def query(agent_id: str | None = None, record_type: str | None = None,
                    project: str | None = None, include_archived: bool = False) -> Any:
        world.calls.append({"tool": "query"})
        # MCPServer flattens a bare list into separate text blocks; the wrapper
        # keeps the real metadata-listing shape for Helheim's browse contract.
        return {"result": copy.deepcopy(seed["query"])}

    @mcp.tool()
    async def get(uuid: str, cursor: str | None = None, limit: int | None = None,
                  include_edit_context: bool = False) -> Any:
        if uuid != seed["pages"][0]["uuid"]:
            return None
        row = await world.get(cursor)
        if not include_edit_context:
            row.pop("edit_context", None)
            row.pop("edit_body", None)
        return row

    @mcp.tool()
    async def edit(uuid: str, old_string: str, new_string: str, replace_all: bool = False,
                   expected_revision: str | None = None,
                   expected_tenant: str | None = None) -> Any:
        world.writes += 1
        raise ValueError("Read-only capture fixture: unexpected write")

    @mcp.tool()
    async def edit_metadata(uuid: str, title: str | None = None,
                            expected_revision: str | None = None,
                            expected_tenant: str | None = None) -> Any:
        world.writes += 1
        raise ValueError("Read-only capture fixture: unexpected write")

    @mcp.custom_route("/__qa__/health", methods=["GET"])
    async def health(request: Request):
        return JSONResponse({"kind": "helheim-qa-memory", "run": owner["run"],
                             "nonce_hash": hashlib.sha256(owner["nonce"].encode()).hexdigest()})

    @mcp.custom_route("/__qa__/mode", methods=["POST"])
    async def mode(request: Request):
        world.set_mode((await request.json())["mode"])
        return JSONResponse({"mode": world.mode})

    @mcp.custom_route("/__qa__/state", methods=["GET"])
    async def status(request: Request):
        return JSONResponse({"mode": world.mode, "calls": world.calls, "writes": world.writes})

    @mcp.custom_route("/__qa__/waiting", methods=["GET"])
    async def waiting(request: Request):
        await asyncio.wait_for(world.waiting.wait(), timeout=15)
        return JSONResponse({"second_page_paused": True})

    @mcp.custom_route("/__qa__/resume", methods=["POST"])
    async def resume(request: Request):
        world.resume.set()
        return JSONResponse({"resumed": True})

    @mcp.custom_route("/__qa__/stop", methods=["POST"])
    async def shutdown(request: Request):
        request.scope["qa_shutdown"]()
        return JSONResponse({"stopping": True})

    inner = mcp.streamable_http_app(json_response=True, stateless_http=True, host="127.0.0.1")

    class Gate:
        shutdown = None

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await inner(scope, receive, send)
            headers = dict(scope["headers"])
            if (scope["path"] != "/__qa__/health"
                    and headers.get(b"authorization") != ("Bearer " + owner["nonce"]).encode()):
                return await JSONResponse({"error": "wrong QA identity"}, status_code=401)(
                    scope, receive, send)
            scope["qa_shutdown"] = self.shutdown
            if world.mode == "interrupt" and scope["method"] == "POST" and scope["path"] == "/mcp":
                messages, body = [], b""
                while True:
                    message = await receive()
                    messages.append(message)
                    body += message.get("body", b"")
                    if message["type"] != "http.request" or not message.get("more_body"):
                        break
                payload = json.loads(body)
                args = payload.get("params", {}).get("arguments", {})
                if (payload.get("method") == "tools/call"
                        and payload.get("params", {}).get("name") == "get" and args.get("cursor")):
                    world.calls.append({"tool": "get", "cursor": args["cursor"],
                                        "fault": "HTTP 503"})
                    return await JSONResponse({"error": "controlled transport interruption"},
                                              status_code=503)(scope, receive, send)
                async def replay():
                    return messages.pop(0) if messages else await receive()
                return await inner(scope, replay, send)
            return await inner(scope, receive, send)

    return Gate()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    project, root = state_path(args.project, args.state)
    owner = owned(root, project)
    seed = json.loads((project / "qa/seeds/paged-read.json").read_text())
    assert (hashlib.sha256(seed["body"].encode()).hexdigest()
            == seed["pages"][0]["edit_body"]["sha256"])
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    app = app_for(seed, owner)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                          log_level="warning", access_log=False))
    app.shutdown = lambda: setattr(server, "should_exit", True)
    write(root / "runtime/memory-peer.json", {"url": f"http://127.0.0.1:{port}",
                                             "kind": "helheim-qa-memory"})
    server.run(sockets=[sock])


if __name__ == "__main__":
    main()
