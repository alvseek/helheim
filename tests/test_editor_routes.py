import html
import json
import re

import pytest
from fastapi.testclient import TestClient
from test_memory_editor import Server as FakeMemory
from test_workspaces import BASE, FakeGitHub

from helheim.app import create_app
from helheim.config import Server, Settings
from helheim.editors import github_url
from helheim.github import Repository
from helheim.memory_editor import MemoryEditor
from helheim.workspaces import Workspaces, WorkspaceStore

ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture
def rig(tmp_path):
    repository = Repository("core", "owner", "repo")
    settings = Settings(
        home=tmp_path, procedure_repositories=(repository,),
        servers=(Server("munnin", "https://example.invalid/mcp", "https://example.invalid/mcp"),),
    )
    app = create_app(settings)
    github = FakeGitHub(repository)
    app.state.workspaces = Workspaces(WorkspaceStore(settings.db_path), (repository,),
                                      adapter=lambda repository: github)
    memory = FakeMemory()
    app.state.memory_editor = MemoryEditor(memory.tools, memory.call)
    with TestClient(app, base_url=ORIGIN) as client:
        page = client.get("/memory")
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
        yield app, client, {"Origin": ORIGIN, "X-CSRF-Token": csrf}, github, memory


def initial(page):
    assert page.status_code == 200, page.text
    raw = re.search(r"data-editor-init='([^']*)'", page.text).group(1)
    return json.loads(html.unescape(raw))


def source_request(state, draft):
    return {
        "procedure": state["procedure"], "backend": state["backend"],
        "source_id": state["source_id"], "draft": draft,
        "expected_head": state["head"], "expected_source_hash": state["source_hash"],
    }


def workspace(client, headers):
    response = client.post("/procedures/workspaces",
                           json={"repository": "core"}, headers=headers)
    assert response.status_code == 200
    return response.json()["redirect"]


def test_source_view_preview_and_save_are_distinct(rig):
    app, client, headers, github, memory = rig
    path = workspace(client, headers)
    page = client.get(path + "?source_id=first")
    state = initial(page)
    assert "Shared source: saving changes both" in page.text
    assert "Preview, not deployed content" in page.text
    assert github.calls == []
    invalid_identity = source_request(state, "Must not be written\n")
    invalid_identity["source_id"] = "../not-a-declared-source"
    refused = client.post(path + "/save", json=invalid_identity, headers=headers)
    assert refused.status_code == 400 and github.calls == []
    preview = client.post(path + "/preview",
                          json=source_request(state, "Unsaved § missing-op\n"), headers=headers)
    assert preview.status_code == 200
    assert preview.json()["rendered"]["db"] == "Unsaved § missing-op\nbeta\n"
    assert preview.json()["validation_errors"]["db"] == ["missing-op"]
    assert github.calls == []
    saved = client.post(
        path + "/save", json=source_request(state, "Saved alpha\n"), headers=headers
    )
    assert saved.status_code == 200 and saved.json()["status"] == "saved"
    assert saved.json()["head"] != BASE
    assert github.calls.count("commit") == 1
    duplicate = client.post(path + "/save",
                            json=source_request(state, "Saved alpha\n"), headers=headers)
    assert duplicate.status_code == 409 and github.calls.count("commit") == 1
    second = initial(client.get(path + "?source_id=second"))
    response = client.post(path + "/save",
                           json=source_request(second, "Saved beta\n"), headers=headers)
    assert response.status_code == 200 and response.json()["workspace"]["pull_number"] == 1
    assert "Saved alpha\n" in response.json()["sections"]["db"][0]["text"]
    assert github.calls.count("branch") == 1 and github.calls.count("commit") == 2
    assert app.state.store.list() == []


def test_memory_saves_are_guarded_and_not_written_to_history(rig):
    app, client, headers, github, memory = rig
    memory.body = "\nprivate <script>window.bad = 1</script>\r\n終\n"
    opened = client.get("/memory/munnin/open?uuid=record")
    state = initial(opened)
    assert "Acting account:" in opened.text and "owner" in opened.text
    assert "<script>window.bad = 1</script>" not in opened.text
    assert "&lt;script&gt;" in opened.text
    before = len(memory.calls)
    path = "/memory-edit/" + state["id"]
    preview = client.post(path + "/preview", json={
        "action": "body", "draft": "unsaved", "expected_revision": state["context"]["revision"],
    }, headers=headers)
    assert preview.status_code == 200 and preview.json()["preview"] == "unsaved"
    assert len(memory.calls) == before  # Preview is local and does not call Munnin.
    body = {"action": "body", "draft": "new private body\r\n",
            "expected_revision": state["context"]["revision"]}
    saved = client.post(path + "/save", json=body, headers=headers)
    assert saved.status_code == 200 and saved.json()["saved"] == "body"
    assert memory.body == "new private body\r\n"
    assert memory.fields["content_summary"] == "Summary"
    assert client.post(path + "/save", json=body, headers=headers).status_code == 400
    assert [name for name, args in memory.calls].count("edit") == 1
    changed = client.post(path + "/save", json={
        "action": "metadata", "draft": {"title": "New title"},
        "expected_revision": saved.json()["edit_context"]["revision"],
    }, headers=headers)
    assert changed.status_code == 200 and changed.json()["saved"] == "metadata"
    assert memory.body == "new private body\r\n"
    assert app.state.store.list() == []
    assert b"new private body" not in app.state.settings.db_path.read_bytes()


WRITES = [
    ("/s/munnin/t/edit", {}),
    ("/procedures/workspaces", {"repository": "core"}),
    ("/procedures/workspace/preview", {
        "procedure": "flow", "backend": "db", "source_id": "first", "draft": "x",
        "expected_head": BASE, "expected_source_hash": "hash",
    }),
    ("/procedures/workspace/save", {
        "procedure": "flow", "backend": "db", "source_id": "first", "draft": "x",
        "expected_head": BASE, "expected_source_hash": "hash",
    }),
    ("/procedures/workspace/retry-pr", {}),
    ("/procedures/workspace/reconcile", {}),
    ("/memory-edit/editor/preview", {"action": "body", "draft": "x", "expected_revision": "r"}),
    ("/memory-edit/editor/save", {"action": "body", "draft": "x", "expected_revision": "r"}),
    ("/memory-edit/editor/close", {}),
]


@pytest.mark.parametrize("path,body", WRITES)
def test_every_sibling_write_refuses_cross_origin_and_missing_csrf(rig, path, body):
    app, client, headers, github, memory = rig
    wrong_origin = {**headers, "Origin": "https://hostile.invalid"}
    assert client.post(path, json=body, headers=wrong_origin).status_code == 403
    assert client.post(path, json=body, headers={"Origin": ORIGIN}).status_code == 403
    assert github.calls == [] and memory.calls == []


def test_write_inventory_covers_every_registered_post_route(rig):
    app, client, headers, github, memory = rig
    expected = {
        "/s/{name}/t/{tool}", "/procedures/workspaces",
        "/procedures/{identity}/preview", "/procedures/{identity}/save",
        "/procedures/{identity}/retry-pr", "/procedures/{identity}/reconcile",
        "/memory-edit/{identity}/preview", "/memory-edit/{identity}/save",
        "/memory-edit/{identity}/close",
    }
    actual = {route.path for route in app.routes if "POST" in getattr(route, "methods", set())}
    assert actual == expected


@pytest.mark.parametrize("host", [
    "hostile.invalid:8765", "127.0.0.1:9000", "user@127.0.0.1:8765",
    "127.0.0.1:8765/path",
])
def test_host_refusal_precedes_any_authority(rig, host):
    app, client, headers, github, memory = rig
    assert client.get("/memory", headers={"Host": host}).status_code == 403
    assert client.post("/procedures/workspaces", json={"repository": "core"},
                       headers={**headers, "Host": host}).status_code == 403
    assert github.calls == [] and memory.calls == []


def test_csrf_is_bound_to_the_browser_and_missing_origin_is_refused(rig):
    app, client, headers, github, memory = rig
    assert client.post("/procedures/workspaces", json={"repository": "core"},
                       headers={"X-CSRF-Token": headers["X-CSRF-Token"]}).status_code == 403
    with TestClient(app, base_url=ORIGIN) as other:
        other.get("/memory")
        assert other.post("/procedures/workspaces", json={"repository": "core"},
                          headers=headers).status_code == 403
    assert github.calls == []


def test_cross_site_private_reads_cannot_drive_new_authenticated_editor_loads(rig):
    app, client, headers, github, memory = rig
    hostile = {"Sec-Fetch-Site": "cross-site"}
    assert client.get("/memory/munnin/open?uuid=record", headers=hostile).status_code == 403
    assert client.get("/procedures", headers=hostile).status_code == 403
    assert github.calls == [] and memory.calls == []
    # A hostile cross-site GET cannot silently replace the browser's editor owner cookie.
    outside = TestClient(app, base_url=ORIGIN)
    response = outside.get("/", headers=hostile)
    assert "helheim-editor" not in response.cookies


def test_unsafe_bind_is_refused_before_creating_local_state(tmp_path):
    home = tmp_path / "not-created"
    with pytest.raises(ValueError, match="loopback bind"):
        create_app(Settings(home=home, host="0.0.0.0"))
    assert not home.exists()


@pytest.mark.parametrize("address", [
    "javascript:alert(1)", "https://github.com.hostile.invalid/x",
    "https://user:password@github.com/x", "https://[invalid",
])
def test_untrusted_links_are_not_clickable(address):
    assert github_url(address) is None
