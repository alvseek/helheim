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
            ToolInfo("get", "", {"properties": {"include_edit_context": {}}}, {}),
            *[ToolInfo(tool, "", {"properties": {
                "expected_revision": {}, "expected_tenant": {},
            }}, {}) for tool in ("edit", "edit_metadata")],
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
        if name == "get":
            result = self.pages.pop(0) if self.pages is not None else self.record()
        elif name == "query":
            result = [{"uuid": "record", **self.fields}]
        else:
            assert arguments["expected_tenant"] == "owner"
            assert arguments["expected_revision"] == self.revision
            if name == "edit":
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
    assert rows[0]["uuid"] == "record" and [name for name, args in server.calls] == ["query"]
    server.capable = False
    with pytest.raises(MemoryEditError, match="lacks guarded"):
        await service.open("browser", "munnin", "record")
    assert [name for name, args in server.calls] == ["query"]


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
    assert all(name == "get" for name, args in server.calls)


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
