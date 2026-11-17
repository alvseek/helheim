"""Guarded memory editing outside generic call history; drafts remain in the browser."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any

from .mcp_client import ToolInfo

EDITABLE = ("title", "content_summary", "tags", "project")


class MemoryEditError(ValueError):
    pass


def metadata(record):
    if not set(EDITABLE) <= set(record):
        raise MemoryEditError("record metadata is incomplete; reload before editing")
    return {key: record.get(key) for key in EDITABLE}


def supported(tools: list[ToolInfo]) -> bool:
    shapes = {tool.name: tool.input_schema.get("properties", {}) for tool in tools}
    return (
        "include_edit_context" in shapes.get("get", {})
        and all({"expected_revision", "expected_tenant"} <= set(shapes.get(name, {}))
                for name in ("edit", "edit_metadata"))
    )


def context(record):
    value = record.get("edit_context")
    if (not isinstance(value, dict) or set(value) != {"uuid", "tenant", "revision"}
            or not all(isinstance(item, str) and item for item in value.values())
            or value["uuid"] != record.get("uuid")):
        raise MemoryEditError("server did not return a complete edit context; editing is disabled")
    return dict(value)


def valid_text(value):
    if not isinstance(value, str):
        raise MemoryEditError("text fields must contain strings")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise MemoryEditError("text must be valid UTF-8") from exc


@dataclass
class Editing:
    id: str
    owner: str
    server: str
    context: dict[str, str]
    body: str
    metadata: dict[str, Any]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class MemoryEditor:
    def __init__(self, list_tools, call_tool):
        self.list_tools = list_tools
        self.call_tool = call_tool
        self.sessions: dict[str, Editing] = {}

    async def call(self, server, name, arguments):
        # This path deliberately does not call Store.insert or the inspector route.
        outcome = await self.call_tool(server, name, arguments)
        if outcome.is_error:
            raise MemoryEditError(str(outcome.text)[:1000])
        result = outcome.payload
        # Different MCP SDKs wrap scalar tool returns in {"result": ...}.
        if isinstance(result, dict) and set(result) == {"result"}:
            result = result["result"]
        return result

    async def browse(self, server, **filters):
        rows = await self.call(server, "query", filters)
        if not isinstance(rows, list):
            raise MemoryEditError("server did not return a metadata listing")
        return rows

    async def open(self, owner: str, server: str, identity: str):
        if not supported(await self.list_tools(server)):
            raise MemoryEditError("server lacks guarded edit support; use the inspector read-only")
        record = await self.call(server, "get", {
            "uuid": identity, "include_edit_context": True,
        })
        if not isinstance(record, dict):
            raise MemoryEditError("record is unavailable")
        baseline = context(record)
        parts = []
        received = 0
        cursors = set()
        expected_metadata = metadata(record)
        expected_body = record.get("edit_body", {})
        if (not isinstance(expected_body, dict) or not isinstance(expected_body.get("sha256"), str)
                or type(expected_body.get("characters")) is not int):
            raise MemoryEditError(
                "server lacks exact-body reconstruction metadata; editing is disabled"
            )
        while True:
            if context(record) != baseline or metadata(record) != expected_metadata:
                raise MemoryEditError(
                    "account or revision changed while loading; reload the record"
                )
            if not isinstance(record.get("content"), str):
                raise MemoryEditError("body page is incomplete; editing is disabled")
            provenance = record.get("edit_body", {})
            if (not isinstance(provenance, dict)
                    or provenance.get("sha256") != expected_body["sha256"]
                    or provenance.get("characters") != expected_body["characters"]):
                raise MemoryEditError("body changed while loading; reload the record")
            segments = provenance.get("segments")
            if not isinstance(segments, list) or not segments:
                raise MemoryEditError("body page has no source positions; editing is disabled")
            position = 0
            for segment in segments:
                if (not isinstance(segment, dict)
                        or type(segment.get("source_start")) is not int
                        or type(segment.get("characters")) is not int
                        or segment["source_start"] < 0 or segment["characters"] < 0):
                    raise MemoryEditError("invalid body source positions; editing is disabled")
                end = position + segment["characters"]
                parts.append((segment["source_start"], record["content"][position:end]))
                position = end
            if position != len(record["content"]):
                raise MemoryEditError("body page is incomplete; editing is disabled")
            received += position
            if received > expected_body["characters"]:
                raise MemoryEditError(
                    "body pages exceed their declared length; editing is disabled"
                )
            cursor = record.get("next_cursor")
            if cursor is None:
                break
            if not record["content"]:
                raise MemoryEditError("body pagination made no progress; editing is disabled")
            if not isinstance(cursor, str) or cursor in cursors:
                raise MemoryEditError("body pagination is incomplete; reload the record")
            cursors.add(cursor)
            record = await self.call(server, "get", {
                "uuid": baseline["uuid"], "cursor": cursor, "include_edit_context": True,
            })
            if not isinstance(record, dict):
                raise MemoryEditError("body load was interrupted; no editor was enabled")
        offset = 0
        chunks = []
        for start, text in sorted(parts):
            if start != offset:
                raise MemoryEditError("body pages overlap or leave a gap; editing is disabled")
            chunks.append(text)
            offset += len(text)
        body = "".join(chunks)
        if (offset != expected_body["characters"]
                or hashlib.sha256(body.encode("utf-8")).hexdigest() != expected_body["sha256"]):
            raise MemoryEditError("complete body hash did not match; editing is disabled")
        editing = Editing(uuid.uuid4().hex, owner, server, baseline, body, expected_metadata)
        self.sessions[editing.id] = editing
        return editing

    def get(self, identity: str, owner: str):
        editing = self.sessions.get(identity)
        if editing is None or editing.owner != owner:
            raise MemoryEditError("editor session ended or belongs to another browser; reload")
        return editing

    async def save(self, identity: str, owner: str, action: str, draft, expected_revision: str):
        editing = self.get(identity, owner)
        async with editing.lock:
            if editing.context["revision"] != expected_revision:
                raise MemoryEditError("editor changed in another request; draft retained, reload")
            if not supported(await self.list_tools(editing.server)):
                raise MemoryEditError(
                    "server no longer advertises guarded saves; editing is disabled"
                )
            arguments = {
                "uuid": editing.context["uuid"],
                "expected_revision": editing.context["revision"],
                "expected_tenant": editing.context["tenant"],
            }
            if action == "body":
                valid_text(draft)
                if draft == editing.body:
                    raise MemoryEditError("no body changes to save")
                arguments.update(old_string=editing.body, new_string=draft)
                result = await self.call(editing.server, "edit", arguments)
            elif action == "metadata":
                if not isinstance(draft, dict) or set(draft) - set(EDITABLE):
                    raise MemoryEditError(
                        "metadata may contain only title, content_summary, tags and project"
                    )
                for key, value in draft.items():
                    if value is None:
                        continue
                    if key == "tags":
                        if not isinstance(value, list):
                            raise MemoryEditError("tags must be a list of text values or null")
                        for tag in value:
                            valid_text(tag)
                    else:
                        valid_text(value)
                changed = {key: value for key, value in draft.items()
                           if value != editing.metadata.get(key)}
                if not changed:
                    raise MemoryEditError("no metadata changes to save")
                clearing = [key for key, value in changed.items() if value is None]
                arguments.update({
                    key: value for key, value in changed.items() if value is not None
                })
                if clearing:
                    arguments["clearing"] = clearing
                result = await self.call(editing.server, "edit_metadata", arguments)
            else:
                raise MemoryEditError("unknown save area")
            if not isinstance(result, dict) or "content" in result or "full_content" in result:
                raise MemoryEditError(
                    "server returned an unsupported save result; reload to verify it"
                )
            fresh = context(result)
            if (fresh["uuid"], fresh["tenant"]) != (
                editing.context["uuid"], editing.context["tenant"]
            ):
                raise MemoryEditError("save context changed unexpectedly; reload to verify it")
            # A failed/uncertain call never reaches this point or rewrites a baseline.
            if action == "body":
                editing.body = draft
            editing.context = fresh
            editing.metadata = metadata(result)
            return {"saved": action, "edit_context": fresh, "metadata": editing.metadata}
