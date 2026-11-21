"""Git write outcomes through the real gh subprocess boundary; no real gh or network."""
from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from helheim.app import create_app
from helheim.config import Settings
from helheim.github import GitConflict, GitHub, GitUncertain, Repository
from helheim.workspaces import Workspaces, WorkspaceStore
from qa.fixtures.git_remote import BASE, FILES, STORE, GitHubPeer

PROJECT = Path(__file__).resolve().parents[1]
ORIGIN = "http://127.0.0.1:8765"


def edited(text):
    """A whole db.md document whose read region carries this text."""
    return FILES[STORE].decode("utf-8").replace("DB read", text)


@pytest.fixture
def git(monkeypatch):
    fixture = GitHubPeer(PROJECT)
    fixture.activate(monkeypatch)
    try:
        yield fixture
    finally:
        fixture.close()


def setup(git):
    repository = Repository("core", "owner", "repo")
    store = WorkspaceStore(git.root / "workspace.db")
    workspaces = Workspaces(store, (repository,))
    assert type(workspaces.github(repository)) is GitHub
    return workspaces, workspaces.create("core"), store


def save(workspaces, state, source, draft):
    view = workspaces.preview(state["id"], "flow", "db", source)
    return workspaces.save(
        state["id"], "flow", "db", source, draft,
        view["head"], view["original"].sha256,
    )


def test_adapter_uses_only_the_owned_gh_process(git):
    found = shutil.which("gh")
    assert found is not None and Path(found).resolve() == git.gh_path.resolve()
    workspaces, state, store = setup(git)
    endpoints = [call["endpoint"] for call in git.calls()]
    assert endpoints[0].startswith("repos/owner/repo/commits/")
    assert any("git/trees/" in endpoint for endpoint in endpoints)
    assert git.counters()["branch"] == git.counters()["commit"] == 0
    assert workspaces.store.get(state["id"])["head_oid"] == BASE


@pytest.mark.parametrize("fault", ["lost", "unreadable"])
def test_lost_or_unreadable_branch_creation_keeps_pending_and_never_adopts(git, fault):
    workspaces, state, store = setup(git)
    git.faults(branch=fault)
    draft = edited("unique branch draft")
    with pytest.raises(GitUncertain, match="reconcile"):
        save(workspaces, state, "db", draft)
    pending = store.get(state["id"])
    assert pending["pending_action"] == "create_branch"
    assert pending["pending_save"]["path"] == STORE
    assert pending["pending_save"]["new_hash"] == hashlib.sha256(draft.encode()).hexdigest()
    assert "unique branch draft" not in json.dumps(pending)
    assert git.counters()["branch"] == 1
    assert git.counters()["commit"] == 0
    assert git.state()["branches"][state["branch"]] == BASE

    before = len(git.calls())
    with pytest.raises(GitUncertain, match="prior save"):
        workspaces.save(state["id"], "flow", "db", "rules", edited("refused"), BASE, "unused")
    assert len(git.calls()) == before
    with pytest.raises(GitUncertain, match="never adopt or reset"):
        workspaces.reconcile(state["id"])
    assert store.get(state["id"]) == pending
    assert git.counters()["branch"] == 1


@pytest.mark.parametrize("fault", ["lost", "unreadable"])
def test_lost_or_unreadable_commit_response_reconciles_without_recommit(git, fault):
    workspaces, state, store = setup(git)
    git.faults(commit=fault)
    draft = edited("unique commit draft")
    with pytest.raises(GitUncertain, match="reconcile"):
        save(workspaces, state, "db", draft)
    pending = store.get(state["id"])
    assert pending["pending_action"] == "commit"
    assert pending["pending_save"]["new_hash"] == hashlib.sha256(draft.encode()).hexdigest()
    assert "unique commit draft" not in json.dumps(pending)
    assert pending["branch_created"] is True
    assert pending["head_oid"] == BASE
    assert git.counters()["commit"] == 1
    head = git.state()["branches"][state["branch"]]
    assert head != BASE

    second = workspaces.preview(state["id"], "flow", "db", "rules")
    before = len(git.calls())
    with pytest.raises(GitUncertain, match="prior save"):
        workspaces.save(state["id"], "flow", "db", "rules", edited("refused"),
                        second["head"], second["original"].sha256)
    assert len(git.calls()) == before
    reconciled = workspaces.reconcile(state["id"])
    assert reconciled["pending_action"] is None
    assert reconciled["pending_pr"] is True
    assert reconciled["head_oid"] == head
    assert git.counters()["commit"] == 1
    retried = workspaces.retry_pull(state["id"])
    assert retried["status"] == "saved"
    assert retried["workspace"]["head_oid"] == head
    assert retried["workspace"]["pull_number"] == 1
    assert git.counters()["commit"] == 1
    assert git.state()["pulls"]["1"]["head"]["ref"] == state["branch"]


def test_ambiguous_commit_is_not_adopted_by_reconcile(git):
    workspaces, state, store = setup(git)
    git.faults(commit="lost")
    with pytest.raises(GitUncertain):
        save(workspaces, state, "db", edited("ambiguous"))
    head = git.state()["branches"][state["branch"]]
    git.mutate_commit(head, message="unrelated")
    with pytest.raises(GitUncertain, match="inspect its history"):
        workspaces.reconcile(state["id"])
    assert store.get(state["id"])["pending_action"] == "commit"
    assert git.counters()["commit"] == 1
    assert git.counters()["pull"] == 0


@pytest.mark.parametrize("fault", ["500", "unreadable"])
def test_pull_read_failure_after_commit_is_partial_success(git, fault):
    workspaces, state, store = setup(git)
    first = save(workspaces, state, "db", edited("first saved"))
    assert first["status"] == "saved"
    first_head = first["workspace"]["head_oid"]
    git.faults(pull_read=fault, pull_read_after=1)
    second = save(workspaces, state, "db", edited("second saved"))
    assert second["status"] == "saved_pr_pending"
    assert second["error"]
    assert second["workspace"]["pending_pr"] is True
    assert second["workspace"]["head_oid"] != first_head
    assert second["document"]["sha256"] == hashlib.sha256(
        edited("second saved").encode()
    ).hexdigest()
    assert git.state()["branches"][state["branch"]] == second["head"]
    assert git.counters()["commit"] == 2
    commits = git.counters()["commit"]

    git.faults(pull_read=None, pull_read_after=None)
    retried = workspaces.retry_pull(state["id"])
    assert git.counters()["commit"] == commits, retried
    assert retried["status"] == "saved", retried
    assert retried["workspace"]["head_oid"] == second["head"]
    assert retried["workspace"]["pull_number"] == 1
    assert git.state()["pulls"]["1"]["head"]["ref"] == state["branch"]


@pytest.mark.parametrize("fault", ["500", "unreadable"])
def test_pull_list_failure_after_commit_is_partial_success(git, fault):
    workspaces, state, store = setup(git)
    git.faults(pull_list=fault)
    result = save(workspaces, state, "db", edited("first saved"))
    assert result["status"] == "saved_pr_pending"
    assert result["error"]
    assert result["workspace"]["pending_pr"] is True
    assert result["document"]["sha256"] == hashlib.sha256(
        edited("first saved").encode()
    ).hexdigest()
    assert git.counters()["commit"] == 1
    head = result["head"]
    assert git.state()["branches"][state["branch"]] == head

    git.faults(pull_list=None)
    retried = workspaces.retry_pull(state["id"])
    assert retried["status"] == "saved"
    assert retried["workspace"]["head_oid"] == head
    assert retried["workspace"]["pull_number"] == 1
    assert git.counters()["commit"] == 1


@pytest.mark.parametrize("change", [
    {"state": "closed"}, {"draft": False},
    {"head": {"ref": "other-branch"}}, {"base": {"ref": "other-base"}},
])
def test_existing_pr_refuses_closed_ready_or_wrong_head_base(git, change):
    workspaces, state, store = setup(git)
    assert save(workspaces, state, "db", edited("first saved"))["status"] == "saved"
    git.mutate_pr(**change)
    before = git.counters()
    with pytest.raises(GitConflict, match="no longer the original open draft"):
        workspaces.save(state["id"], "flow", "db", "rules", edited("refused"),
                        BASE, "unused")
    assert git.counters()["commit"] == before["commit"]
    assert git.counters()["branch"] == before["branch"]
    assert git.counters()["pull"] == before["pull"]


@pytest.mark.parametrize("change", [
    {"state": "closed"}, {"draft": False},
    {"head": {"ref": "other-branch"}}, {"base": {"ref": "other-base"}},
])
def test_listed_pr_refuses_closed_ready_or_wrong_head_base(git, change):
    workspaces, state, store = setup(git)
    git.faults(commit="lost")
    with pytest.raises(GitUncertain):
        save(workspaces, state, "db", edited("first saved"))
    head = git.state()["branches"][state["branch"]]
    workspaces.reconcile(state["id"])
    git.put_pull(state["branch"], **change)
    git.faults(pull_list_ignore_filter=True)
    result = workspaces.retry_pull(state["id"])
    assert result["status"] == "saved_pr_pending"
    assert result["workspace"]["pending_pr"] is True
    assert result["workspace"]["pull_number"] is None
    assert result["workspace"]["head_oid"] == head
    assert git.counters()["commit"] == 1
    assert git.counters()["pull"] == 0
    assert result["pr_uncertain"] is ("state" not in change and "draft" not in change)


def test_branch_collision_is_refused_before_adoption(git):
    workspaces, state, store = setup(git)
    git.update(lambda value: value["branches"].__setitem__(state["branch"], BASE))
    with pytest.raises(GitConflict, match="already exists"):
        workspaces.preview(state["id"], "flow", "db")
    assert git.counters()["branch"] == 0
    assert git.counters()["commit"] == 0
    assert git.state()["branches"][state["branch"]] == BASE


def csrf_headers(client):
    page = client.get("/procedures")
    token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
    return {"Origin": ORIGIN, "X-CSRF-Token": token}


def initial(page):
    assert page.status_code == 200, page.text
    raw = re.search(r"data-editor-init='([^']*)'", page.text).group(1)
    return json.loads(html.unescape(raw))


def visible(page, action):
    tag = re.search(
        rf'<button[^>]*data-workspace-action="{action}"[^>]*>', page.text
    )
    assert tag is not None, page.text
    return "hidden" not in tag.group(0)


def source_request(state, draft):
    return {
        "procedure": state["procedure"], "backend": state["backend"],
        "document": state["document_id"], "draft": draft,
        "expected_head": state["head"], "expected_document_hash": state["document_hash"],
    }


def app_for(git):
    home = git.root / "home"
    home.mkdir()
    repository = Repository("core", "owner", "repo")
    return create_app(Settings(
        home=home, procedure_repositories=(repository,), servers=(),
    ))


def test_served_uncertain_branch_creation_refuses_adoption_and_reload(git):
    with TestClient(app_for(git), base_url=ORIGIN) as client:
        headers = csrf_headers(client)
        created = client.post("/procedures/workspaces",
                              json={"repository": "core"}, headers=headers)
        path = created.json()["redirect"]
        page = client.get(path + "?document=db")
        state = initial(page)
        assert not visible(page, "reconcile") and not visible(page, "retry-pr")
        git.faults(branch="lost")
        refused = client.post(path + "/save",
                              json=source_request(state, edited("unsaved branch draft")),
                              headers=headers)
        assert refused.status_code == 409 and refused.json()["uncertain"] is True
        reloaded = client.get(path + "?document=db")
        assert reloaded.status_code == 409 and "already exists" in reloaded.text
        checked = client.post(path + "/reconcile", headers=headers)
        assert checked.status_code == 409 and checked.json()["uncertain"] is True
        identity = path.rsplit("/", 1)[-1]
        assert client.app.state.workspaces.store.get(identity)["pending_action"] == "create_branch"
        assert git.counters()["branch"] == 1 and git.counters()["commit"] == 0


def test_served_page_offers_retry_pr_after_confirmed_commit(git):
    with TestClient(app_for(git), base_url=ORIGIN) as client:
        headers = csrf_headers(client)
        created = client.post("/procedures/workspaces",
                              json={"repository": "core"}, headers=headers)
        path = created.json()["redirect"]
        page = client.get(path + "?document=db")
        state = initial(page)
        git.faults(commit="lost")
        refused = client.post(path + "/save",
                              json=source_request(state, edited("saved commit draft")),
                              headers=headers)
        assert refused.status_code == 409 and refused.json()["uncertain"] is True
        page = client.get(path + "?document=db")
        assert visible(page, "reconcile") and not visible(page, "retry-pr")
        reconciled = client.post(path + "/reconcile", headers=headers)
        assert reconciled.status_code == 200
        assert reconciled.json()["workspace"]["pending_pr"] is True
        head = reconciled.json()["workspace"]["head_oid"]
        page = client.get(path + "?document=db")
        assert initial(page)["pending"] is True
        assert visible(page, "retry-pr") and not visible(page, "reconcile")
        retried = client.post(path + "/retry-pr", headers=headers)
        assert retried.status_code == 200 and retried.json()["status"] == "saved"
        assert retried.json()["workspace"]["head_oid"] == head
        assert retried.json()["workspace"]["pull_number"] == 1
        page = client.get(path + "?document=db")
        assert "https://github.com/owner/repo/pull/1" in page.text
        assert not visible(page, "retry-pr") and not visible(page, "reconcile")
        assert git.counters()["commit"] == 1
