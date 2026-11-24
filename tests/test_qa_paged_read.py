"""The captured paged read over the owned fixture's real loopback MCP transport."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from pathlib import Path

import pytest

from helheim import mcp_client
from helheim.config import Server
from helheim.mcp_client import ClientError
from helheim.memory_editor import MemoryEditError, MemoryEditor
from qa.fixtures.processes import Peer

PROJECT = Path(__file__).resolve().parents[1]
UUID = "11111111111141118111111111111111"

SIMPLE_FAULTS = {
    "revision": "changed while loading",
    "metadata": "changed while loading",
    "tenant": "changed while loading",
    "wrong-uuid": "changed while loading",
    "missing-context": "complete edit context",
    "missing-edit-body": "exact-body reconstruction",
    "bad-hash": "complete body hash did not match",
    "page-hash": "body changed while loading",
    "bad-length": "complete body hash did not match",
    "gap": "overlap or leave a gap",
    "overlap": "overlap or leave a gap",
    "repeat-cursor": "pagination is incomplete",
}

PAUSED_FAULTS = {
    "pause-revision": "changed while loading",
    "pause-metadata": "changed while loading",
    "pause-tenant": "changed while loading",
    "pause-body": "complete body hash did not match",
}


def captured():
    return json.loads((PROJECT / "qa/seeds/paged-read.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def peer():
    fixture = Peer(PROJECT, "paged_remote").start()
    try:
        yield fixture
    finally:
        fixture.close()


def transport(peer):
    server = Server("paged", peer.url + "/mcp", peer.url + "/mcp")
    token = peer.owner["nonce"]

    async def tools(_name):
        return await mcp_client.list_tools(server, token)

    async def call(_name, tool, arguments):
        return await mcp_client.call_tool(server, token, tool, arguments)

    return MemoryEditor(tools, call)


def observed(peer):
    return peer.request("/__qa__/state")


def payload(outcome):
    value = outcome.payload
    if isinstance(value, dict) and set(value) == {"result"}:
        return value["result"]
    return value


async def test_fixture_serves_the_captured_pages_over_real_mcp(peer):
    peer.request("/__qa__/mode", {"mode": "valid"})
    data = captured()
    server = Server("paged", peer.url + "/mcp", peer.url + "/mcp")
    token = peer.owner["nonce"]
    first = payload(await mcp_client.call_tool(
        server, token, "get", {"uuid": UUID, "include_edit_context": True},
    ))
    assert first == data["pages"][0]
    second = payload(await mcp_client.call_tool(server, token, "get", {
        "uuid": UUID,
        "cursor": data["pages"][0]["next_cursor"],
        "include_edit_context": True,
    }))
    assert second == data["pages"][1]
    assert peer.url.startswith("http://127.0.0.1:")


async def test_captured_pages_reconstruct_the_exact_original_body_in_source_order(peer):
    peer.request("/__qa__/mode", {"mode": "valid"})
    data = captured()
    editor = transport(peer)
    opened = await editor.open("browser", "paged", UUID)
    assert opened.body == data["body"]
    assert len(opened.body) == data["pages"][0]["edit_body"]["characters"]
    assert (hashlib.sha256(opened.body.encode("utf-8")).hexdigest()
            == data["pages"][0]["edit_body"]["sha256"])
    pieces = []
    for page in data["pages"]:
        offset = 0
        for segment in page["edit_body"]["segments"]:
            end = offset + segment["characters"]
            pieces.append((segment["source_start"], page["content"][offset:end]))
            offset = end
        assert offset == len(page["content"])
    assert "".join(text for _, text in sorted(pieces)) == opened.body
    assert data["pages"][0]["content"] + data["pages"][1]["content"] != opened.body
    assert opened.context == data["pages"][0]["edit_context"]
    assert opened.metadata == {key: data["pages"][0][key] for key in opened.metadata}
    state = observed(peer)
    assert state["mode"] == "valid" and state["writes"] == 0
    assert state["calls"] == [
        {"tool": "get", "cursor": None},
        {"tool": "get", "cursor": data["pages"][0]["next_cursor"]},
    ]


@pytest.mark.parametrize(("mode", "message"), SIMPLE_FAULTS.items())
async def test_corrupted_page_is_refused_before_a_writable_editor_or_write(peer, mode, message):
    peer.request("/__qa__/mode", {"mode": mode})
    editor = transport(peer)
    with pytest.raises(MemoryEditError, match=message):
        await editor.open("browser", "paged", UUID)
    assert editor.sessions == {}
    state = observed(peer)
    assert state["mode"] == mode and state["writes"] == 0
    assert {call["tool"] for call in state["calls"]} <= {"get", "query"}


async def test_interrupted_transport_is_refused_before_a_writable_editor_or_write(peer):
    peer.request("/__qa__/mode", {"mode": "interrupt"})
    editor = transport(peer)
    with pytest.raises(ClientError, match="paged:"):
        await editor.open("browser", "paged", UUID)
    assert editor.sessions == {}
    state = observed(peer)
    assert state["mode"] == "interrupt" and state["writes"] == 0
    assert {"tool": "get", "cursor": None} in state["calls"]
    assert any(call.get("fault") == "HTTP 503" for call in state["calls"])


@pytest.mark.parametrize(("mode", "message"), PAUSED_FAULTS.items())
async def test_change_between_pages_is_refused_after_resume(peer, mode, message):
    peer.request("/__qa__/mode", {"mode": mode})
    editor = transport(peer)
    opening = asyncio.create_task(editor.open("browser", "paged", UUID))
    try:
        await asyncio.wait_for(
            asyncio.to_thread(peer.request, "/__qa__/waiting"), timeout=20,
        )
        peer.request("/__qa__/resume", {})
        with pytest.raises(MemoryEditError, match=message):
            await asyncio.wait_for(opening, timeout=20)
    finally:
        if not opening.done():
            opening.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await opening
    assert editor.sessions == {}
    state = observed(peer)
    assert state["mode"] == mode and state["writes"] == 0
    assert [call["tool"] for call in state["calls"]] == ["get", "get"]


async def test_healthy_recovery_obtains_a_fresh_complete_baseline(peer):
    data = captured()
    peer.request("/__qa__/mode", {"mode": "revision"})
    editor = transport(peer)
    with pytest.raises(MemoryEditError, match="changed while loading"):
        await editor.open("browser", "paged", UUID)
    assert editor.sessions == {}
    peer.request("/__qa__/mode", {"mode": "valid"})
    opened = await editor.open("browser", "paged", UUID)
    assert opened.body == data["body"]
    assert (hashlib.sha256(opened.body.encode("utf-8")).hexdigest()
            == data["pages"][0]["edit_body"]["sha256"])
    assert list(editor.sessions) == [opened.id]
    state = observed(peer)
    assert state["calls"] == [
        {"tool": "get", "cursor": None},
        {"tool": "get", "cursor": data["pages"][0]["next_cursor"]},
    ]
    assert state["writes"] == 0
