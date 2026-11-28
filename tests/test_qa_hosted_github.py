"""Hosted GitHub credential path through an owned synthetic gh subprocess."""

from __future__ import annotations

import base64
import copy
import hashlib
import html
import json
import re
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_hosted_login import ORIGIN, hosted_settings

from helheim.app import create_app
from helheim.config import load_settings
from helheim.github import _COMMIT_MUTATION, GitError, Repository
from helheim.hosted_login import SESSION_COOKIE
from qa.fixtures.git_remote import FILES, STORE, GitHubPeer

PROJECT = Path(__file__).resolve().parents[1]
REPO = Repository("core", "alvseek", "agent-memory-system")
DISPOSABLE = Repository("core", "fixture-owner", "fixture-repo")
TEST_CREDENTIAL = "fixture_only_credential"
DISPOSABLE_CREDENTIAL = "fixture_disposable_credential"


@pytest.fixture
def git(monkeypatch):
    peer = GitHubPeer(PROJECT)
    peer.activate(monkeypatch)
    peer.update(lambda state: state.update(
        owner="alvseek", repo="agent-memory-system", hosted_probe=True,
    ))
    # Only synthetic values: the application must not forward any of these.
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
                "HELHEIM_HOSTED_GITHUB_TOKEN", "HELHEIM_HOSTED_CLIENT_SECRET",
                "GH_HOST", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(key, "ambient_fixture")
    real_run = subprocess.run
    peer.config_paths = []

    def run_owned_shim(args, **kwargs):
        assert args[0] == "gh"
        peer.config_paths.append(Path(kwargs["env"]["GH_CONFIG_DIR"]))
        # The test-only shim's state location is added after the product has
        # constructed its environment; it is not a production credential.
        kwargs["env"]["HELHEIM_QA_GH_STATE"] = str(peer.state_path)
        return real_run(args, **kwargs)

    monkeypatch.setattr("helheim.github.subprocess.run", run_owned_shim)
    root = peer.root
    try:
        yield peer
    finally:
        peer.close()
        assert not root.exists()


def app_for(peer, token=TEST_CREDENTIAL, repositories=(REPO,)):
    settings = hosted_settings(peer.root / "hosted", servers=(),
                               procedure_repositories=repositories, hosted_github_token=token)
    app = create_app(settings)
    app.state.auth.owner_gate.sessions["fixture-owner"] = time.monotonic() + 600
    return app


def session(app):
    client = TestClient(app, base_url=ORIGIN, follow_redirects=False)
    client.cookies.set(SESSION_COOKIE, "fixture-owner", domain="helheim.example", path="/")
    return client


def csrf(client):
    page = client.get("/procedures")
    assert page.status_code == 200
    match = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    assert match is not None
    return {"Origin": ORIGIN, "X-CSRF-Token": match.group(1)}


def initial(page):
    assert page.status_code == 200
    match = re.search(r"data-editor-init='([^']*)'", page.text)
    assert match is not None
    return json.loads(html.unescape(match.group(1)))


def assert_confined(peer, repository=REPO, credential=TEST_CREDENTIAL):
    calls = peer.calls()
    assert calls
    assert all(all(call["credential_probe"].values()) for call in calls)
    assert credential not in json.dumps(calls)
    assert credential not in json.dumps(peer.state())
    assert all(not path.exists() for path in peer.config_paths)
    assert all(call["endpoint"] == "graphql"
               or call["endpoint"].startswith(repository.api + "/") for call in calls)


def configured_app_for(peer, monkeypatch, repository, credential):
    home = peer.root / "configured-home"
    home.mkdir()
    (home / "config.toml").write_text(
        '[procedure_repositories.core]\n'
        f'owner = "{repository.owner}"\nname = "{repository.name}"\n'
        '[servers.munnin]\nurl = "http://127.0.0.1:9/mcp"\n'
        '[servers.hermod]\nurl = "http://127.0.0.1:9/mcp"\n'
        '[servers.einherjar]\nurl = "http://127.0.0.1:9/mcp"\n',
        encoding="utf-8",
    )
    for key, value in {
        "HELHEIM_HOME": str(home), "HELHEIM_MODE": "hosted",
        "HELHEIM_HOSTED_ORIGIN": ORIGIN, "HELHEIM_HOSTED_ISSUER": "https://issuer.example",
        "HELHEIM_HOSTED_CLIENT_ID": "test-client", "HELHEIM_HOSTED_CLIENT_SECRET": "test-only",
        "HELHEIM_HOSTED_OWNER_SUB": "owner", "HELHEIM_HOSTED_GITHUB_TOKEN": credential,
    }.items():
        monkeypatch.setenv(key, value)
    settings = load_settings()
    assert settings.procedure_repositories == (repository,)
    app = create_app(settings)
    app.state.auth.owner_gate.sessions["fixture-owner"] = time.monotonic() + 600
    return app


@pytest.mark.parametrize("repository", [REPO, DISPOSABLE], ids=["selected", "disposable"])
def test_hosted_subprocess_saves_one_file_to_branch_and_open_draft_pr(
    git, monkeypatch, repository,
):
    credential = TEST_CREDENTIAL if repository == REPO else DISPOSABLE_CREDENTIAL
    git.update(lambda state: state.update(
        owner=repository.owner, repo=repository.name,
        credential_fingerprint=hashlib.sha256(credential.encode()).hexdigest(),
    ))
    with session(configured_app_for(git, monkeypatch, repository, credential)) as client:
        headers = csrf(client)
        refused = client.post("/procedures/workspaces", json={"repository": "other"},
                              headers=headers)
        assert refused.status_code == 400 and not git.calls()
        adapter = client.app.state.workspaces.github(repository)
        with pytest.raises(GitError):
            adapter.request("repos/different/unapproved/commits/main")
        forged = {"query": _COMMIT_MUTATION, "variables": {"input": {
            "branch": {"repositoryNameWithOwner": "different/unapproved",
                       "branchName": "helheim/" + "a" * 32},
            "expectedHeadOid": "a" * 40, "message": {"headline": "fixture"},
            "fileChanges": {"additions": [{"path": STORE, "contents": "fixture"}]},
        }}}
        with pytest.raises(GitError):
            adapter.request("graphql", method="POST", data=forged)
        assert not git.calls()
        created = client.post("/procedures/workspaces", json={"repository": "core"},
                              headers=headers)
        assert created.status_code == 200
        path = created.json()["redirect"]
        state = initial(client.get(path + "?document=db&region=flow.read"))
        draft = "Synthetic hosted edit"
        saved = client.post(path + "/save", headers=headers, json={
            "procedure": state["procedure"], "backend": state["backend"],
            "document": state["document_id"], "draft": draft,
            "region": state["region"],
            "expected_head": state["head"], "expected_document_hash": state["document_hash"],
        })
        assert saved.status_code == 200 and saved.json()["status"] == "saved"
        assert credential not in saved.text
        assert credential.encode() not in client.app.state.settings.db_path.read_bytes()
        remote = git.state()
        assert remote["counters"]["branch"] == 1
        assert remote["counters"]["commit"] == 1
        assert remote["counters"]["pull"] == 1
        pull = remote["pulls"]["1"]
        assert pull["draft"] is True and pull["state"] == "open"
        branch = pull["head"]["ref"]
        commit = remote["commits"][remote["branches"][branch]]
        tree = remote["trees"][commit["tree"]]
        assert base64.b64decode(remote["blobs"][tree[STORE]]) == FILES[STORE].replace(
            b"DB read", draft.encode()
        )
        commit_call = next(call for call in git.calls() if call["endpoint"] == "graphql")
        assert commit_call["repository"].lower() == (
            f"{repository.owner}/{repository.name}".lower()
        )
        assert commit_call["expected_head"] == remote["base"]
        assert commit_call["path"] == STORE
        assert any(call.get("draft") is True for call in git.calls()
                   if call["method"] == "POST" and call["endpoint"].endswith("/pulls"))
        assert_confined(git, repository, credential)


def test_hosted_stale_head_and_non_draft_pr_refuse_writes(git):
    with session(app_for(git)) as client:
        headers = csrf(client)
        created = client.post("/procedures/workspaces", json={"repository": "core"},
                              headers=headers)
        path = created.json()["redirect"]
        view = initial(client.get(path + "?document=db&region=flow.read"))
        draft = "Guarded hosted edit"
        payload = {"procedure": view["procedure"], "backend": view["backend"],
                   "document": view["document_id"], "draft": draft,
                   "region": view["region"],
                   "expected_head": "f" * 40, "expected_document_hash": view["document_hash"]}
        assert client.post(path + "/save", headers=headers, json=payload).status_code == 409
        assert git.counters()["branch"] == git.counters()["commit"] == 0
        payload["expected_head"] = view["head"]
        assert client.post(path + "/save", headers=headers, json=payload).status_code == 200
        git.mutate_pr(draft=False)
        before = git.counters()
        assert client.post(path + "/save", headers=headers, json=payload).status_code == 409
        assert git.counters()["branch"] == before["branch"]
        assert git.counters()["commit"] == before["commit"]
        assert git.counters()["pull"] == before["pull"]
        assert_confined(git)


@pytest.mark.parametrize("token", ["", "bad token", "bad\nvalue"])
def test_missing_or_invalid_credential_never_starts_a_process(git, token):
    with session(app_for(git, token=token)) as client:
        response = client.post("/procedures/workspaces", json={"repository": "core"},
                               headers=csrf(client))
        assert response.status_code == 400
        assert git.calls() == []


def test_wrong_repo_and_wrong_requests_never_start_a_process(git):
    with pytest.raises(ValueError):
        app_for(git, repositories=(REPO, Repository("duplicate", REPO.owner, REPO.name)))
    with session(app_for(git, repositories=())) as disconnected:
        response = disconnected.post("/procedures/workspaces", json={"repository": "core"},
                                     headers=csrf(disconnected))
        assert response.status_code == 400
    with session(app_for(git)) as client:
        response = client.post("/procedures/workspaces", json={"repository": "other"},
                               headers=csrf(client))
        assert response.status_code == 400
        adapter = client.app.state.workspaces.github(REPO)
        for endpoint, method, payload in (
            ("https://other.example/repos/alvseek/agent-memory-system/commits/main", "GET", None),
            ("repos/other/repo/commits/main", "GET", None),
            (REPO.api + "/%2e%2e/other/repo/commits/main", "GET", None),
            (REPO.api + "/%25252e%25252e/other/repo/commits/main", "GET", None),
            (REPO.api + "/merges", "POST", {}),
            ("graphql", "POST", {"query": "mutation { arbitrary }", "variables": {}}),
            (REPO.api + "/pulls", "POST", {"head": "main", "base": "main", "draft": False}),
        ):
            with pytest.raises(GitError):
                adapter.request(endpoint, method=method, data=payload)
        assert git.calls() == []


def test_hosted_multiple_repositories_keep_adapters_confined(git):
    other = Repository("other", "fixture-owner", "fixture-repo")
    with session(app_for(git, repositories=(REPO, other))) as client:
        headers = csrf(client)
        assert client.post("/procedures/workspaces", json={"repository": "unknown"},
                           headers=headers).status_code == 400
        for selected, forbidden in ((REPO, other), (other, REPO)):
            adapter = client.app.state.workspaces.github(selected)
            with pytest.raises(GitError):
                adapter.request(forbidden.api + "/commits/main")
        assert git.calls() == []


def test_rejected_credential_does_not_retry_or_leak(git):
    git.faults(credential_refused=True)
    with session(app_for(git)) as client:
        response = client.post("/procedures/workspaces", json={"repository": "core"},
                               headers=csrf(client))
        assert response.status_code == 400
        assert TEST_CREDENTIAL not in response.text
        assert len(git.calls()) == 1
        assert_confined(git)


@pytest.mark.parametrize("fault", ["repository", "branch", "path", "deletion"])
def test_graphql_commit_cannot_escape_the_approved_draft_workspace(git, fault):
    with session(app_for(git)) as client:
        adapter = client.app.state.workspaces.github(REPO)
        data = {"query": _COMMIT_MUTATION, "variables": {"input": {
            "branch": {"repositoryNameWithOwner": "alvseek/agent-memory-system",
                       "branchName": "helheim/" + "a" * 32},
            "expectedHeadOid": "a" * 40,
            "message": {"headline": "fixture"},
            "fileChanges": {"additions": [{"path": STORE, "contents": "fixture"}]},
        }}}
        change = copy.deepcopy(data)
        body = change["variables"]["input"]
        if fault == "repository":
            body["branch"]["repositoryNameWithOwner"] = "other/repo"
        elif fault == "branch":
            body["branch"]["branchName"] = "main"
        elif fault == "path":
            body["fileChanges"]["additions"][0]["path"] = "outside.md"
        else:
            body["fileChanges"]["deletions"] = [{"path": STORE}]
        with pytest.raises(GitError):
            adapter.request("graphql", method="POST", data=change)
        assert git.calls() == []


def test_environment_configuration_is_separate_and_not_in_repr(tmp_path, monkeypatch):
    monkeypatch.setenv("HELHEIM_HOME", str(tmp_path))
    monkeypatch.setenv("HELHEIM_MODE", "hosted")
    monkeypatch.setenv("HELHEIM_HOSTED_ORIGIN", ORIGIN)
    monkeypatch.setenv("HELHEIM_HOSTED_ISSUER", "https://issuer.example")
    monkeypatch.setenv("HELHEIM_HOSTED_CLIENT_ID", "test-client")
    monkeypatch.setenv("HELHEIM_HOSTED_CLIENT_SECRET", "test-only")
    monkeypatch.setenv("HELHEIM_HOSTED_OWNER_SUB", "owner")
    monkeypatch.setenv("HELHEIM_HOSTED_GITHUB_TOKEN", TEST_CREDENTIAL)
    settings = load_settings()
    assert settings.hosted_github_token == TEST_CREDENTIAL
    assert TEST_CREDENTIAL not in repr(settings)
