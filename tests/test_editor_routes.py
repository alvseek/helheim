import html
import json
import re

import pytest
from fastapi.testclient import TestClient
from test_memory_editor import Server as FakeMemory
from test_workspaces import BASE, FILES, FLOW_BODY, FLOW_PATH, STORE_PATH, FakeGitHub, document

from helheim.app import create_app
from helheim.config import Server, Settings
from helheim.editors import github_url
from helheim.github import GitHub, Repository
from helheim.memory_editor import MemoryEditor
from helheim.workspaces import Workspaces, WorkspaceStore

ORIGIN = "http://127.0.0.1:8765"
STORE_PATH_BYTES = FILES["procedures/memory/storage-backends/db.md"]
FLOW_PATH_BYTES = FILES["procedures/flow.md"]


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
        "document": state["document_id"], "draft": draft,
        "region": state["region"],
        "expected_head": state["head"],
        "expected_document_hash": state["document_hash"],
    }


def textarea(page):
    found = re.search(r'<textarea id="source-draft"[^>]*>(.*?)</textarea>', page.text, re.S)
    assert found is not None, page.text
    return html.unescape(found.group(1))


def workspace(client, headers):
    response = client.post("/procedures/workspaces",
                           json={"repository": "core"}, headers=headers)
    assert response.status_code == 200
    return response.json()["redirect"]


@pytest.mark.parametrize(("status", "uncertain"), [(422, False), (503, True)])
def test_save_preserves_safe_diagnostics_through_adapter_and_route(rig, status, uncertain):
    import subprocess

    _, client, headers, github, _ = rig
    path = workspace(client, headers)
    state = initial(client.get(path, params={"document": "db", "region": "flow.read"}))
    explanation = "first line\n" + "detail " * 200 + "\nfinal line"
    draft = "private submitted source"

    def runner(args, **options):
        body = {"message": explanation, "errors": [{"field": "head", "message": "invalid"}],
                "request": draft}
        return subprocess.CompletedProcess(
            args, 1, f"HTTP/2.0 {status} Error\nAuthorization: secret\n\n" + json.dumps(body),
            "secret stderr",
        )

    real = GitHub(github.repository, runner=runner)
    github.commit = real.commit
    result = client.post(path + "/save", json=source_request(state, draft), headers=headers)
    data = result.json()
    assert data["uncertain"] is uncertain
    assert data["diagnostic"]["upstream_status"] == status
    assert data["diagnostic"]["stage"] == "commit"
    assert explanation in data["diagnostic"]["details"]
    assert "head: invalid" in data["diagnostic"]["details"]
    assert draft not in result.text and "secret" not in result.text


def test_delete_workspace_is_local_version_checked_and_guards_recovery(rig):
    app, client, headers, github, _ = rig
    first = workspace(client, headers)
    second = workspace(client, headers)
    identity = first.rsplit("/", 1)[1]
    store = app.state.workspaces.store
    state = store.get(identity)
    assert 'data-workspace-action="delete"' in client.get("/procedures").text
    state = store.update(state, pull_number=7, pull_url="https://github.com/owner/repo/pull/7")
    github.calls.clear()
    stale = client.post(first + "/delete", json={"version": 0}, headers=headers)
    assert stale.status_code == 409
    for changes in ({"pending_action": "commit"}, {"pending_action": None, "pending_pr": True}):
        state = store.update(state, **changes)
        blocked = client.post(first + "/delete", json={"version": state["version"]},
                              headers=headers)
        assert blocked.status_code == 409
        assert store.get(identity)["id"] == identity
    state = store.update(state, pending_pr=False)
    deleted = client.post(first + "/delete", json={"version": state["version"]}, headers=headers)
    assert deleted.json() == {"deleted": True}
    assert [row["id"] for row in store.list()] == [second.rsplit("/", 1)[1]]
    assert github.calls == []


def test_pr_failure_keeps_confirmed_commit_and_adapter_diagnostics(rig):
    import subprocess

    _, client, headers, github, _ = rig
    path = workspace(client, headers)
    state = initial(client.get(path, params={"document": "db", "region": "flow.read"}))
    message = "PR refused\n" + "long reason " * 120 + "\nlast reason"
    def runner(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 1, "HTTP/2.0 403 Error\n\n" + json.dumps({"message": message}), ""
        )
    real = GitHub(github.repository, runner=runner)
    github.ensure_pull = lambda branch, identity: real.request(
        "repos/owner/repo/pulls", method="POST", data={}
    )
    response = client.post(path + "/save", json=source_request(state, "new text"), headers=headers)
    result = response.json()
    assert result["status"] == "saved_pr_pending"
    assert result["workspace"]["pending_pr"] is True
    assert result["diagnostic"]["upstream_status"] == 403
    assert result["diagnostic"]["details"] == message
    assert github.calls.count("commit") == 1


def test_source_view_preview_and_save_are_distinct(rig):
    app, client, headers, github, memory = rig
    path = workspace(client, headers)
    github.commits[BASE][STORE_PATH] += (
        b'@start region id="other.private"\r\nUnrelated section\r\n@end region\r\n'
    )
    baseline = github.commits[BASE][STORE_PATH]
    composed = client.get(path)
    assert composed.status_code == 200
    assert "Owning document" not in composed.text
    assert "Composed instructions · db" in composed.text
    assert f'<form method="get" action="{path}"' in composed.text
    assert "data-procedure-selectors" in composed.text
    for text in ("Read, then write.", "Shared rules.", "DB read", "DB write", "Edit document"):
        assert text in composed.text
    assert github.calls == []

    page = client.get(path + "?document=db")
    assert "db sections used by flow" in page.text and "Unrelated section" not in page.text
    assert 'select name="backend"' not in page.text
    assert 'name="backend"' in composed.text
    assert composed.text.index('name="document"') < composed.text.index('name="backend"')
    edit_href = re.search(r'href="([^"]+region=flow.read[^"]+)">Edit section', composed.text)[1]
    editing = client.get(path + html.unescape(edit_href))
    state = initial(editing)
    assert "Composed instructions · db" in editing.text and "DB write" in editing.text
    assert textarea(editing) == "DB read" and "Unrelated section" not in editing.text
    assert "Save section to draft PR" in editing.text
    assert "Preview both backends" not in editing.text
    assert "data-source-preview" not in editing.text
    md = client.get(path, params={"document": "procedures/memory/storage-backends/markdown.md"})
    assert "MD read" in md.text and "MD write" in md.text and "DB read" not in md.text
    for text in ("Composed instructions ·", "Read, then write.", "Shared rules.", "Edit document"):
        assert text not in page.text
    assert "Preview, not deployed content" in page.text
    assert github.calls == []

    base = client.get(path, params={"document": "db", "revision": "base"})
    assert base.status_code == 200 and "The base snapshot is read-only" in base.text
    assert "DB read" in base.text and "DB write" in base.text
    assert "Composed instructions ·" not in base.text
    assert "Read, then write." not in base.text and "source-draft" not in base.text

    shared = client.get(path + "?document=flow")
    assert "Shared document: saving changes both" in shared.text
    assert textarea(shared).encode("utf-8") == FLOW_PATH_BYTES

    invalid_identity = source_request(state, "Must not be written\n")
    invalid_identity["document"] = "../not-a-declared-document"
    refused = client.post(path + "/save", json=invalid_identity, headers=headers)
    assert refused.status_code == 400 and github.calls == []
    revision_draft = "Unsaved § missing-op"
    preview = client.post(path + "/preview",
                          json=source_request(state, revision_draft), headers=headers)
    assert preview.status_code == 200
    assert "Unsaved § missing-op\n" in preview.json()["rendered"]["db"]
    assert "Read, then write." in preview.json()["rendered"]["db"]
    assert "MD read" in preview.json()["rendered"]["markdown"]
    assert preview.json()["validation_errors"]["db"] == ["missing-op"]
    assert preview.json()["dependents"] == ["flow"]
    assert preview.json()["applicability"] == ["db"]
    assert github.calls == []
    for bad in (dict(region="other.private"), dict(draft="@end region\n")):
        body = source_request(state, "unallowed") | bad
        assert client.post(path + "/save", json=body, headers=headers).status_code == 400
    assert github.calls == []
    saved_draft = "Saved read"
    saved = client.post(
        path + "/save", json=source_request(state, saved_draft), headers=headers
    )
    assert saved.status_code == 200 and saved.json()["status"] == "saved"
    assert saved.json()["workspace"]["pull_number"] == 1
    for params in ({}, {"document": "flow"}, {"document": "db", "region": "flow.read"},
                   {"revision": "base"}):
        view = client.get(path, params=params)
        assert 'data-pr-link href="https://github.com/owner/repo/pull/1"' in view.text
        assert ">PR #1</a>" in view.text
    assert saved.json()["head"] != BASE
    assert github.commits[saved.json()["head"]][STORE_PATH] == baseline.replace(
        b"DB read", b"Saved read"
    )
    assert saved.json()["document"]["path"] == "procedures/memory/storage-backends/db.md"
    assert github.calls.count("commit") == 1
    duplicate = client.post(path + "/save",
                            json=source_request(state, saved_draft), headers=headers)
    assert duplicate.status_code == 409 and github.calls.count("commit") == 1
    rules_page = client.get(path + "?document=rules")
    component = initial(rules_page)
    assert component["document_path"] == "procedures/components/rules.md"
    assert component["document_kind"] == "component"
    assert textarea(rules_page).encode("utf-8") == FILES["procedures/components/rules.md"]
    rules_draft = FILES["procedures/components/rules.md"].decode("utf-8").replace(
        "Shared rules.", "Saved rules."
    )
    response = client.post(path + "/save",
                           json=source_request(component, rules_draft), headers=headers)
    assert response.status_code == 200 and response.json()["workspace"]["pull_number"] == 1
    assert response.json()["document"]["path"] == "procedures/components/rules.md"
    assert github.calls.count("branch") == 1 and github.calls.count("commit") == 2
    assert app.state.store.list() == []


@pytest.mark.parametrize(
    ("document", "notice"), [("", False), ("db", True)],
)
def test_view_falls_back_to_composed_instructions(rig, document, notice):
    _, client, headers, github, _ = rig
    path = workspace(client, headers)
    state = initial(client.get(path, params={"document": "db", "region": "flow.read"}))

    page = client.get(path, params={"backend": "markdown", "document": document})
    assert page.status_code == 200, page.text
    assert "Composed instructions · markdown" in page.text
    assert "Owning document" not in page.text
    assert ("selected document is not available" in page.text) is notice

    body = source_request(state, "Must not be written\n")
    body.update(backend="markdown", document=document)
    for action in ("preview", "save"):
        assert client.post(f"{path}/{action}", json=body, headers=headers).status_code == 400
    assert github.calls == []


def test_view_does_not_switch_to_a_different_source_with_the_same_id(rig):
    _, client, headers, github, _ = rig
    component_path = "procedures/components/db.md"
    github.commits[BASE][component_path] = document("db", "component", "Markdown-only rules.\n")
    github.commits[BASE][FLOW_PATH] = document("flow", "procedure", FLOW_BODY + (
        f'@start component id="flow.md-rules" ref="{component_path}" applies_to="markdown"\n'
        "@end component\n"
    ))
    path = workspace(client, headers)
    db_view = client.get(path, params={"backend": "db", "document": "db", "region": "flow.read"})
    assert initial(db_view)["document_path"] == STORE_PATH
    assert f'value="{STORE_PATH}"' in db_view.text
    md_view = client.get(path, params={"backend": "markdown", "document": "db"})
    assert initial(md_view)["document_path"] == component_path

    switched = client.get(path, params={
        "backend": "markdown", "document": "db", "document_path": STORE_PATH,
    })
    assert switched.status_code == 200
    assert "selected document is not available" in switched.text
    assert "Composed instructions · markdown" in switched.text
    assert "Owning document" not in switched.text
    assert github.calls == []


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
        "procedure": "flow", "backend": "db", "document": "db", "draft": "x",
        "expected_head": BASE, "expected_document_hash": "hash",
    }),
    ("/procedures/workspace/save", {
        "procedure": "flow", "backend": "db", "document": "db", "draft": "x",
        "expected_head": BASE, "expected_document_hash": "hash",
    }),
    ("/procedures/workspace/retry-pr", {}),
    ("/procedures/workspace/reconcile", {}),
    ("/procedures/workspace/delete", {"version": 0}),
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
        "/procedures/{identity}/delete",
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


def test_close_editor_releases_baseline_without_a_remote_write(rig):
    app, client, headers, github, memory = rig
    page = client.get("/memory/munnin/open?uuid=record")
    state = initial(page)
    assert "data-memory-close" in page.text
    assert "30 minutes" in page.text
    path = "/memory-edit/" + state["id"]
    calls = len(memory.calls)
    assert client.post(path + "/close", json={}, headers=headers).json() == {"closed": True}
    assert state["id"] not in app.state.memory_editor.sessions
    assert len(memory.calls) == calls
    refused = client.post(path + "/save", json={
        "action": "body", "draft": "not saved",
        "expected_revision": state["context"]["revision"],
    }, headers=headers)
    assert refused.status_code == 400
    assert "draft retained" in refused.json()["error"]
    assert len(memory.calls) == calls


@pytest.mark.parametrize("path", ["/", "/memory", "/procedures"])
def test_response_policy_preserves_native_same_origin_form_submissions(rig, path):
    _, client, _, _, _ = rig
    response = client.get(path)
    assert response.status_code == 200
    # no-referrer makes Chromium native form POSTs carry Origin: null. Keep
    # cross-origin referrers suppressed without breaking the strict Origin guard.
    assert response.headers["Referrer-Policy"] == "same-origin"


@pytest.mark.parametrize("path,body", WRITES)
def test_null_origin_remains_refused_on_every_product_post(rig, path, body):
    _, client, headers, github, memory = rig
    response = client.post(
        path, json=body,
        headers={**headers, "Origin": "null", "Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 403
    assert "same local Origin" in response.json()["detail"]
    assert github.calls == [] and memory.calls == []
