import asyncio
import copy
import hashlib

import pytest

from helheim.mcp_client import CallOutcome, ClientError, ToolInfo
from helheim.memory_editor import MemoryEditError, MemoryEditor


class Server:
    def __init__(self):
        self.body = "ABC"
        self.revision = "revision-1"
        self.calls = []
        self.capable = True
        self.fail = False
        self.pages = None
        self.fields = {"title": "Title", "content_summary": "Summary", "tags": [], "project": None}

    async def tools(self, name):
        if not self.capable:
            return []
        return [
            ToolInfo("get_memory", "", {"properties": {"include_edit_context": {}}}, {}),
            *[ToolInfo(tool, "", {"properties": {
                "expected_revision": {}, "expected_tenant": {},
            }}, {}) for tool in ("edit_memory_body", "edit_memory_metadata")],
        ]

    def context(self):
        return {"uuid": "record", "tenant": "owner", "revision": self.revision}

    def record(self):
        return {
            "uuid": "record", **self.fields, "edit_context": self.context(),
            "content": self.body, "next_cursor": None,
            "edit_body": {"sha256": hashlib.sha256(self.body.encode()).hexdigest(),
                          "characters": len(self.body),
                          "segments": [{"source_start": 0, "characters": len(self.body)}]},
        }

    async def call(self, server, name, arguments):
        self.calls.append((name, copy.deepcopy(arguments)))
        if self.fail:
            raise ClientError("connection interrupted")
        if name == "get_memory":
            result = self.pages.pop(0) if self.pages is not None else self.record()
        elif name == "list_memories":
            result = [{"uuid": "record", **self.fields}]
        else:
            assert arguments["expected_tenant"] == "owner"
            assert arguments["expected_revision"] == self.revision
            if name == "edit_memory_body":
                assert arguments["old_string"] == self.body
                self.body = arguments["new_string"]
            else:
                for key in self.fields:
                    if key in arguments:
                        self.fields[key] = arguments[key]
                for key in arguments.get("clearing", []):
                    self.fields[key] = None
            self.revision += "-next"
            result = {"uuid": "record", **self.fields, "edit_context": self.context()}
        return CallOutcome(result, {}, "", False, 0)


def editor():
    server = Server()
    return server, MemoryEditor(server.tools, server.call)


async def test_old_server_is_read_only_and_browse_does_not_fetch_bodies():
    server, service = editor()
    rows = await service.browse("munnin")
    assert rows[0]["uuid"] == "record"
    assert [name for name, args in server.calls] == ["list_memories"]
    server.capable = False
    with pytest.raises(MemoryEditError, match="lacks guarded"):
        await service.open("browser", "munnin", "record")
    assert [name for name, args in server.calls] == ["list_memories"]


async def test_nonadjacent_source_positions_reconstruct_the_exact_body():
    server, service = editor()
    first = server.record()
    first["content"] = "AC"
    first["next_cursor"] = "next"
    first["edit_body"]["segments"] = [
        {"source_start": 0, "characters": 1}, {"source_start": 2, "characters": 1},
    ]
    last = server.record()
    last["content"] = "B"
    last["edit_body"]["segments"] = [{"source_start": 1, "characters": 1}]
    server.pages = [first, last]
    opened = await service.open("browser", "munnin", "record")
    assert opened.body == "ABC"
    saved = await service.save(opened.id, "browser", "body", "Exact\r\n終\n", server.revision)
    assert saved["saved"] == "body" and server.body == "Exact\r\n終\n"
    assert "content" not in saved


@pytest.mark.parametrize("corruption", ["revision", "hash", "gap", "missing-context"])
async def test_incomplete_or_mixed_pages_never_enable_save(corruption):
    server, service = editor()
    page = server.record()
    if corruption == "revision":
        page["next_cursor"] = "next"
        changed = copy.deepcopy(page)
        changed["next_cursor"] = None
        changed["edit_context"]["revision"] = "changed"
        server.pages = [page, changed]
    elif corruption == "hash":
        page["edit_body"]["sha256"] = "not-the-body"
        server.pages = [page]
    elif corruption == "gap":
        page["edit_body"]["segments"][0]["source_start"] = 1
        server.pages = [page]
    else:
        del page["edit_context"]
        server.pages = [page]
    with pytest.raises(MemoryEditError):
        await service.open("browser", "munnin", "record")
    assert not service.sessions
    assert all(name == "get_memory" for name, args in server.calls)


async def test_separate_saves_preserve_the_other_baseline_and_refuse_duplicate_revision():
    server, service = editor()
    opened = await service.open("browser", "munnin", "record")
    old_revision = opened.context["revision"]
    await service.save(opened.id, "browser", "metadata",
                       {**opened.metadata, "title": "Changed"}, old_revision)
    assert opened.body == "ABC" and server.body == "ABC"
    assert opened.metadata["title"] == "Changed"
    before = len(server.calls)
    with pytest.raises(MemoryEditError, match="another request"):
        await service.save(opened.id, "browser", "body", "lost", old_revision)
    assert len(server.calls) == before
    await service.save(opened.id, "browser", "body", "new", opened.context["revision"])
    assert opened.metadata["title"] == "Changed" and server.fields["title"] == "Changed"


async def test_other_browser_and_unknown_metadata_fields_are_refused():
    server, service = editor()
    opened = await service.open("browser", "munnin", "record")
    before = len(server.calls)
    with pytest.raises(MemoryEditError, match="another browser"):
        await service.save(opened.id, "someone-else", "body", "lost", server.revision)
    assert len(server.calls) == before
    with pytest.raises(MemoryEditError, match="metadata may contain only"):
        await service.save(opened.id, "browser", "metadata",
                           {"owner": "someone-else"}, server.revision)
    assert server.body == "ABC"


async def test_capability_loss_or_transport_failure_never_advances_a_baseline():
    server, service = editor()
    opened = await service.open("browser", "munnin", "record")
    baseline = copy.deepcopy(opened.context)
    server.capable = False
    with pytest.raises(MemoryEditError, match="no longer advertises"):
        await service.save(opened.id, "browser", "body", "lost", server.revision)
    assert opened.body == "ABC" and opened.context == baseline
    server.capable = True
    server.fail = True
    with pytest.raises(ClientError, match="interrupted"):
        await service.save(opened.id, "browser", "body", "lost", server.revision)
    assert opened.body == "ABC" and opened.context == baseline


async def test_close_releases_only_the_owning_idle_editor():
    server, service = editor()
    opened = await service.open("browser", "munnin", "record")
    timer = opened.expiry
    with pytest.raises(MemoryEditError, match="another browser"):
        service.close(opened.id, "stranger")
    assert opened.id in service.sessions
    async with opened.lock:
        with pytest.raises(MemoryEditError, match="saving"):
            service.close(opened.id, "browser")
    service.close(opened.id, "browser")
    assert opened.id not in service.sessions
    assert timer.cancelled()
    with pytest.raises(MemoryEditError, match="ended"):
        service.get(opened.id, "browser")


async def test_capacity_releases_least_recently_used_without_touching_busy_saves():
    server = Server()
    now = [0.0]
    service = MemoryEditor(server.tools, server.call, max_sessions=2, clock=lambda: now[0])
    first = await service.open("browser", "munnin", "record")
    now[0] += 1
    second = await service.open("browser", "munnin", "record")
    now[0] += 1
    service.get(first.id, "browser")
    second_timer = second.expiry
    third = await service.open("browser", "munnin", "record")
    assert set(service.sessions) == {first.id, third.id}
    assert second_timer.cancelled()
    async with first.lock, third.lock:
        with pytest.raises(MemoryEditError, match="all editor sessions are busy"):
            await service.open("browser", "munnin", "record")
    assert len(service.sessions) == 2


async def test_idle_expiry_and_activity_refresh_are_deterministic():
    server = Server()
    now = [0.0]
    service = MemoryEditor(server.tools, server.call, idle_seconds=10, clock=lambda: now[0])
    opened = await service.open("browser", "munnin", "record")
    first_timer = opened.expiry
    now[0] = 9
    service.get(opened.id, "browser")
    assert first_timer.cancelled()
    now[0] = 11
    service._expire(opened.id)
    assert opened.id in service.sessions
    now[0] = 20
    service._expire(opened.id)
    assert opened.id not in service.sessions
    with pytest.raises(MemoryEditError, match="draft retained"):
        service.get(opened.id, "browser")


async def test_expiry_never_releases_a_save_in_flight_and_lazy_expiry_refuses_writes():
    server = Server()
    now = [0.0]
    service = MemoryEditor(server.tools, server.call, idle_seconds=10, clock=lambda: now[0])
    opened = await service.open("browser", "munnin", "record")
    async with opened.lock:
        now[0] = 20
        service._expire(opened.id)
        assert opened.id in service.sessions
    calls = len(server.calls)
    with pytest.raises(MemoryEditError, match="expired.*draft retained"):
        await service.save(opened.id, "browser", "body", "lost", server.revision)
    assert len(server.calls) == calls
    assert not service.sessions


async def test_abandoned_baseline_is_released_without_another_request():
    server = Server()
    service = MemoryEditor(server.tools, server.call, idle_seconds=0.01)
    released = asyncio.Event()
    original_release = service._release

    def release(identity):
        original_release(identity)
        released.set()

    service._release = release
    opened = await service.open("browser", "munnin", "record")
    await asyncio.wait_for(released.wait(), timeout=1)
    assert opened.id not in service.sessions
