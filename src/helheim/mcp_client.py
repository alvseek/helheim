"""Speak MCP to one server with a bearer token.

The transport is the SDK's; everything above it is ours. One session per call
sequence: ``initialize``, then the request, then close. The bearer is attached by
the HTTP client the transport is given, which is how mcp 2.x accepts headers.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx2
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from . import __version__
from .config import Server

REQUEST_TIMEOUT_SECONDS = 120.0

#: Names that write, used when a server sets no annotations at all.
DESTRUCTIVE_PREFIXES = (
    "soft_delete",
    "delete",
    "edit",
    "insert",
    "update",
    "create",
    "add",
    "push",
    "promote",
    "demote",
)


class ClientError(Exception):
    """A call that did not produce a result, with a sentence a page can show."""

    def __init__(self, detail: str, status: str = "unavailable") -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


def _root_cause(exc: BaseException) -> BaseException:
    """Dig through the exception groups the SDK wraps everything in.

    A transport failure arrives as ``ExceptionGroup: unhandled errors in a
    TaskGroup`` whose message names nothing useful; the answer is one or more
    levels down.
    """
    depth = 0
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions and depth < 10:
        exc = exc.exceptions[0]
        depth += 1
    return exc


def _describe(exc: BaseException) -> tuple[str, str]:
    """The sentence to show, and the status the page should act on."""
    root = _root_cause(exc)
    if isinstance(root, httpx2.HTTPStatusError):
        code = root.response.status_code
        status = "expired" if code in (401, 403) else "unavailable"
        return f"HTTP {code} {root.response.reason_phrase}", status
    error = getattr(root, "error", None)
    rpc_code = getattr(error, "code", None)
    if rpc_code is not None:
        return (
            f"server answered with JSON-RPC error {rpc_code}: "
            f"{getattr(error, 'message', '')}",
            "unavailable",
        )
    return f"{type(root).__name__}: {root}", "unavailable"


@dataclass
class ToolInfo:
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any]

    @property
    def destructive(self) -> bool:
        """True when the tool writes. Annotations win; names are the fallback."""
        annotation = self.annotations.get("destructive")
        if annotation is not None:
            return bool(annotation)
        read_only = self.annotations.get("read_only")
        if read_only:
            return False
        return self.name.startswith(DESTRUCTIVE_PREFIXES)


@dataclass
class CallOutcome:
    payload: Any
    envelope: Any
    text: str
    is_error: bool
    duration_ms: int


def _annotations(tool: types.Tool) -> dict[str, Any]:
    found: dict[str, Any] = {}
    raw = getattr(tool, "annotations", None)
    if raw is None:
        return found
    if raw.destructive_hint is not None:
        found["destructive"] = raw.destructive_hint
    if raw.read_only_hint is not None:
        found["read_only"] = raw.read_only_hint
    return found


def _decode(result: types.CallToolResult) -> tuple[Any, str]:
    """Join the text parts and parse them when they hold JSON.

    These servers answer with a JSON document inside a text part, so a tree over
    the raw envelope would show one escaped string instead of the structure.
    """
    parts: list[str] = []
    for item in result.content or []:
        if getattr(item, "type", "") == "text":
            text = getattr(item, "text", "") or ""
            parts.append(text)
    joined = "\n".join(parts)
    if not joined and result.structured_content is not None:
        return result.structured_content, json.dumps(result.structured_content)
    try:
        return json.loads(joined), joined
    except (json.JSONDecodeError, TypeError):
        return joined, joined


async def _run(server: Server, token: str, work):
    headers = {"Authorization": f"Bearer {token}"}
    client_info = types.Implementation(name="helheim", version=__version__)
    try:
        async with httpx2.AsyncClient(
            headers=headers, timeout=REQUEST_TIMEOUT_SECONDS
        ) as http_client:
            async with streamable_http_client(server.url, http_client=http_client) as (
                read_stream,
                write_stream,
            ):
                async with ClientSession(
                    read_stream, write_stream, client_info=client_info
                ) as session:
                    await session.initialize()
                    return await work(session)
    except httpx2.HTTPError as exc:
        detail, status = _describe(exc)
        raise ClientError(f"{server.name}: {detail}", status) from exc
    except ClientError:
        raise
    except Exception as exc:  # the SDK raises its own types; all mean "this failed"
        detail, status = _describe(exc)
        raise ClientError(f"{server.name}: {detail}", status) from exc


async def list_tools(server: Server, token: str) -> list[ToolInfo]:
    result = await _run(server, token, lambda session: session.list_tools())
    tools: list[ToolInfo] = []
    for tool in result.tools:
        tools.append(
            ToolInfo(
                name=tool.name,
                description=tool.description or tool.title or "",
                input_schema=tool.input_schema or {},
                annotations=_annotations(tool),
            )
        )
    return tools


async def call_tool(
    server: Server, token: str, name: str, arguments: dict[str, Any]
) -> CallOutcome:
    started = time.perf_counter()
    result = await _run(
        server,
        token,
        lambda session: session.call_tool(name, arguments=arguments or {}),
    )
    duration_ms = int((time.perf_counter() - started) * 1000)
    if not isinstance(result, types.CallToolResult):
        raise ClientError(
            f"{name} returned {type(result).__name__} rather than a tool result"
        )
    payload, text = _decode(result)
    return CallOutcome(
        payload=payload,
        envelope=result.model_dump(mode="json"),
        text=text,
        is_error=bool(result.is_error),
        duration_ms=duration_ms,
    )
