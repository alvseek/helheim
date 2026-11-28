"""Hosted editor routing and recovery through the owned gh subprocess."""

import base64
import hashlib
import json
import os
import re
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from test_qa_hosted_github import ORIGIN, TEST_CREDENTIAL, app_for, initial

from helheim.editor_security import COOKIE
from helheim.github import _COMMIT_MUTATION, GitError, Repository
from helheim.hosted_login import SESSION_COOKIE
from qa.fixtures.git_remote import FILES, STORE, TREE, document

PROJECT = Path(__file__).resolve().parents[1]
pytest_plugins = ("test_qa_hosted_github",)
FRAMEWORK = Repository("framework", "fixture-owner", "agent-memory-system")
PROJECT_REPO = Repository("project", "fixture-owner", "agent-memory-project")
WIZARD = Repository("wizard", "fixture-owner", "agent-memory-wizards")
REPOS = (FRAMEWORK, PROJECT_REPO, WIZARD)
WIZARD_COMPONENT = "procedures/components/wizard-rules.md"
WIZARD_FILES = {
    "procedures/wizard.md": document(
        "wizard", "procedure",
        '@start component id="rules" ref="procedures/components/wizard-rules.md"\n'
        "@end component\n",
    ),
    WIZARD_COMPONENT: document("wizard-rules", "component", "Wizard rules.\n"),
}


@contextmanager
def hosted_http(app):
    """Serve the normal hosted app on an owned loopback socket."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("hosted fixture server did not start")
            threading.Event().wait(0.01)
        with httpx.Client(
            base_url=f"http://127.0.0.1:{sock.getsockname()[1]}",
            headers={"Host": "helheim.example", "Cookie": f"{SESSION_COOKIE}=fixture-owner"},
            timeout=60,
        ) as client:
            page = client.get("/procedures")
            assert page.status_code == 200, page.text
            owner = page.cookies[COOKIE]
            client.headers["Cookie"] = f"{SESSION_COOKIE}=fixture-owner; {COOKIE}={owner}"
            token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
            yield client, {"Origin": ORIGIN, "X-CSRF-Token": token}
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()


def repo_state(git, repository):
    return git.state()["repositories"][f"{repository.owner}/{repository.name}"]


def update_repo(git, repository, update):
    git.update(lambda state: update(state["repositories"][f"{repository.owner}/{repository.name}"]))


def restore_prechange(git, app):
    saved = json.loads((PROJECT / "tests/prechange-workspace.json").read_text(encoding="utf-8"))
    row = saved["workspace"]
    assert row["repository"] == {
        "key": FRAMEWORK.key, "owner": FRAMEWORK.owner, "name": FRAMEWORK.name,
        "base_ref": FRAMEWORK.base_ref, "catalog_root": FRAMEWORK.catalog_root,
    }
    with sqlite3.connect(app.state.settings.db_path) as db:
        db.execute("INSERT INTO procedure_workspace VALUES (?, ?, ?)",
                   (row["id"], row["version"], json.dumps(row)))

    def seed(state):
        raw = saved["saved_source"].encode()
        blob = hashlib.sha256(raw).hexdigest()
        tree = dict(state["trees"][TREE])
        tree[STORE] = blob
        state["blobs"][blob] = base64.b64encode(raw).decode()
        state["trees"]["2" * 40] = tree
        state["commits"][row["head_oid"]] = {
            "tree": "2" * 40, "parents": [row["base_oid"]],
            "message": saved["commit_info"]["message"],
        }
        state["branches"][row["branch"]] = row["head_oid"]
        state["pulls"]["1"] = {
            "number": 1, "html_url": row["pull_url"], "state": "open", "draft": True,
            "head": {"ref": row["branch"]}, "base": {"ref": "main"},
        }
        state["next_oid"] = 2
        state["next_pull"] = 2

    update_repo(git, FRAMEWORK, seed)
    return row


def payload(view, draft):
    return {
        "procedure": view["procedure"], "backend": view["backend"],
        "document": view["document_id"], "region": view["region"], "draft": draft,
        "expected_head": view["head"], "expected_document_hash": view["document_hash"],
    }


def view(client, path, procedure, document_id, region=None):
    params = {"procedure": procedure, "backend": "db", "document": document_id}
    if region:
        params["region"] = region
    return initial(client.get(path, params=params))


def assert_destination(git, repository, target, previous):
    state = repo_state(git, repository)
    commits = [call for call in state["calls"] if call["endpoint"] == "graphql"]
    assert len(commits) == previous + 1
    assert commits[-1]["repository"] == f"{repository.owner}/{repository.name}"
    assert commits[-1]["path"] == target
    pulls = list(state["pulls"].values())
    assert pulls and all(row["draft"] and row["state"] == "open" for row in pulls)
    assert all(row["html_url"].startswith(
        f"https://github.com/{repository.owner}/{repository.name}/pull/"
    ) for row in pulls)


def run_editor_flow(git, project_files, wizard_files, project_case, wizard_case):
    git.configure_repositories(((FRAMEWORK, FILES), (PROJECT_REPO, project_files),
                                (WIZARD, wizard_files)))
    app = app_for(git, repositories=REPOS)
    old = restore_prechange(git, app)
    with hosted_http(app) as (client, headers):
        before = {r.key: repo_state(git, r)["counters"].copy() for r in REPOS}
        refused = client.post("/procedures/workspaces", json={"repository": "unconfigured"},
                              headers=headers)
        assert refused.status_code == 400
        assert all(repo_state(git, r)["counters"] == before[r.key] for r in REPOS)
        for repo in REPOS:
            adapter = app.state.workspaces.github(repo)
            for other in (item for item in REPOS if item != repo):
                with pytest.raises(GitError):
                    adapter.request(other.api + "/commits/main")
                data = {"query": _COMMIT_MUTATION, "variables": {"input": {
                    "branch": {"repositoryNameWithOwner": f"{other.owner}/{other.name}",
                               "branchName": "helheim/" + "a" * 32},
                    "expectedHeadOid": "a" * 40, "message": {"headline": "fixture"},
                    "fileChanges": {"additions": [{"path": STORE, "contents": "eA=="}]},
                }}}
                with pytest.raises(GitError):
                    adapter.request("graphql", method="POST", data=data)
        assert all(repo_state(git, r)["calls"] == [] for r in REPOS)

        old_path = "/procedures/" + old["id"]
        opened = view(client, old_path, "flow", "db", "flow.read")
        assert opened["original"] == "Pre-change saved text"
        assert app.state.workspaces.store.get(old["id"]) == old

        cases = [
            (FRAMEWORK, old_path, "flow", "db", "flow.read", STORE),
            (PROJECT_REPO, None, *project_case),
            (WIZARD, None, *wizard_case),
        ]
        for repository, path, procedure, document_id, region, target in cases:
            if path is None:
                response = client.post("/procedures/workspaces",
                                       json={"repository": repository.key}, headers=headers)
                assert response.status_code == 200, response.text
                path = response.json()["redirect"]
            for backend in ("db", "markdown"):
                page = client.get(path, params={"procedure": procedure, "backend": backend})
                assert page.status_code == 200, page.text
                assert f"Composed instructions · {backend}" in page.text
            state = view(client, path, procedure, document_id, region)
            draft = state["original"] + "\nSynthetic hosted change."
            data = payload(state, draft)
            foreign_document = (project_case[1] if repository == WIZARD else wizard_case[1])
            wrong = {**data, "document": foreign_document, "region": None}
            before_wrong = repo_state(git, repository)["counters"].copy()
            assert client.post(path + "/save", json=wrong, headers=headers).status_code == 400
            assert repo_state(git, repository)["counters"]["commit"] == before_wrong["commit"]
            preview = client.post(path + "/preview", json=data, headers=headers)
            assert preview.status_code == 200, preview.text
            assert "Synthetic hosted change." in preview.json()["rendered"]["db"]
            stale = {**data, "expected_document_hash": "0" * 64}
            before = repo_state(git, repository)["counters"].copy()
            others = {r.key: repo_state(git, r) for r in REPOS if r != repository}
            assert client.post(path + "/save", json=stale, headers=headers).status_code == 409
            assert repo_state(git, repository)["counters"]["commit"] == before["commit"]
            saved = client.post(path + "/save", json=data, headers=headers)
            assert saved.status_code == 200, saved.text
            assert saved.json()["status"] == "saved"
            assert_destination(git, repository, target, before["commit"])
            remote = repo_state(git, repository)
            commit = remote["commits"][saved.json()["head"]]
            new_tree = remote["trees"][commit["tree"]]
            old_tree = remote["trees"][remote["commits"][state["head"]]["tree"]]
            assert {name for name in new_tree if new_tree[name] != old_tree.get(name)} == {target}
            assert b"Synthetic hosted change." in base64.b64decode(
                remote["blobs"][new_tree[target]]
            )
            assert all(repo_state(git, r) == others[r.key] for r in REPOS if r != repository)
            assert saved.json()["workspace"]["pull_url"].startswith(
                f"https://github.com/{repository.owner}/{repository.name}/pull/"
            )
            assert client.post(path + "/save", json=data, headers=headers).status_code == 409

            # A response lost after a real fixture commit must reconcile without replay.
            state = view(client, path, procedure, document_id, region)
            data = payload(state, state["original"] + "\nSecond hosted change.")
            update_repo(git, repository, lambda s: s["faults"].update(commit="lost"))
            response = client.post(path + "/save", json=data, headers=headers)
            assert response.status_code == 409 and response.json()["uncertain"] is True
            commit_count = repo_state(git, repository)["counters"]["commit"]
            assert client.post(path + "/save", json=data, headers=headers).status_code == 409
            reconciled = client.post(path + "/reconcile", headers=headers)
            assert reconciled.status_code == 200, reconciled.text
            assert reconciled.json()["workspace"]["pending_pr"] is True
            update_repo(git, repository, lambda s: s["faults"].pop("commit"))
            retried = client.post(path + "/retry-pr", headers=headers)
            assert retried.status_code == 200 and retried.json()["status"] == "saved"
            assert repo_state(git, repository)["counters"]["commit"] == commit_count

        for repository in REPOS:
            state = repo_state(git, repository)
            assert all(all(call["credential_probe"].values()) for call in state["calls"])
            assert TEST_CREDENTIAL not in json.dumps(state)


def test_hosted_three_repositories_and_prechange_workspace(git):
    run_editor_flow(
        git, FILES, WIZARD_FILES,
        ("flow", "db", "flow.read", STORE),
        ("wizard", "wizard-rules", None, WIZARD_COMPONENT),
    )


def test_hosted_with_pinned_hermod_sources(git):
    project = os.environ.get("HELHEIM_TEST_PROJECT_SOURCE")
    wizard = os.environ.get("HELHEIM_TEST_WIZARD_SOURCE")
    if not project or not wizard:
        pytest.skip("set HELHEIM_TEST_PROJECT_SOURCE and HELHEIM_TEST_WIZARD_SOURCE")

    def files(root):
        root = Path(root)
        return {path.relative_to(root).as_posix(): path.read_bytes()
                for path in (root / "procedures").rglob("*") if path.is_file()}

    run_editor_flow(
        git, files(project), files(wizard),
        ("awaken-coder", "db", "awaken-coder",
         "procedures/memory/storage-backends/db.md"),
        ("high-wizard", "planning-rounds", None, "procedures/components/planning-rounds.md"),
    )
