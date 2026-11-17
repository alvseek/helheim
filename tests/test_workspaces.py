import copy

import pytest
from procedure_composer import MemorySource

from helheim.github import GitConflict, GitError, GitUncertain, Repository
from helheim.workspaces import Workspaces, WorkspaceStore

BASE = "a" * 40
FILES = {
    "procedures/assemblies/flow.toml": (
        b'format_version=1\nid="flow"\ntitle="Flow"\ndescription="Do it."\n'
        b'[[sections]]\nid="first"\nsource="procedures/sections/first.md"\n'
        b'[[sections]]\nid="second"\nsource="procedures/sections/second.md"\n'
    ),
    "procedures/sections/first.md": b"alpha\n",
    "procedures/sections/second.md": b"beta\n",
}


class FakeGitHub:
    def __init__(self, repository):
        self.repository = repository
        self.commits = {BASE: dict(FILES)}
        self.branches = {}
        self.calls = []
        self.pr = None
        self.fail_pr = False
        self.uncertain_commit = False
        self.information = {}

    def resolve(self, reference):
        return BASE

    def head(self, branch):
        return self.branches.get(branch)

    def sources(self, commit):
        return MemorySource(self.commits[commit])

    def create_branch(self, branch, commit):
        assert branch not in self.branches
        self.calls.append("branch")
        self.branches[branch] = commit

    def commit(self, branch, expected, path, content, save_id):
        if self.branches[branch] != expected:
            raise GitConflict("head moved")
        self.calls.append("commit")
        identity = f"{len(self.commits):040x}"
        self.commits[identity] = {**self.commits[expected], path: content}
        self.information[identity] = {
            "message": f"Edit procedure source ({save_id})", "parents": [{"sha": expected}],
        }
        self.branches[branch] = identity
        if self.uncertain_commit:
            raise GitUncertain("response lost after commit")
        return identity

    def commit_info(self, commit):
        return self.information[commit]

    def ensure_pull(self, branch, workspace_id):
        self.calls.append("pull")
        if self.fail_pr:
            raise GitError("PR creation failed")
        if self.pr is None:
            self.pr = {"number": 1, "html_url": "https://github.com/owner/repo/pull/1",
                       "state": "open", "draft": True, "head": {"ref": branch},
                       "base": {"ref": "main"}}
        return self.pr

    def pull(self, number):
        return self.pr


def setup(tmp_path):
    repository = Repository("core", "owner", "repo")
    github = FakeGitHub(repository)
    store = WorkspaceStore(tmp_path / "workspace.db")
    workspaces = Workspaces(store, (repository,), adapter=lambda repository: github)
    state = workspaces.create("core")
    return workspaces, state, github


def save(workspaces, state, source_id, draft):
    view = workspaces.preview(state["id"], "flow", "db", source_id)
    return workspaces.save(state["id"], "flow", "db", source_id, draft,
                           view["head"], view["original"].source_hash)


def test_open_and_preview_do_not_write_and_two_saves_share_one_pr(tmp_path):
    workspaces, state, github = setup(tmp_path)
    preview = workspaces.preview(state["id"], "flow", "db", "first", "draft\n")
    assert preview["rendered"]["db"].text == "draft\nbeta\n"
    assert preview["rendered"]["markdown"].text == "draft\nbeta\n"
    assert github.calls == []
    one = save(workspaces, state, "first", "edited alpha\n")
    two = save(workspaces, state, "second", "edited beta\n")
    assert one["workspace"]["pull_number"] == two["workspace"]["pull_number"] == 1
    assert github.calls.count("branch") == 1 and github.calls.count("commit") == 2
    rendered = workspaces.preview(state["id"], "flow", "db")["rendered"]["db"].text
    assert rendered == "edited alpha\nedited beta\n"


def test_stale_source_and_unknown_id_never_write(tmp_path):
    workspaces, state, github = setup(tmp_path)
    with pytest.raises(GitConflict):
        workspaces.save(state["id"], "flow", "db", "first", "lost", BASE, "stale-hash")
    with pytest.raises(GitError):
        current = workspaces.preview(state["id"], "flow", "db", "first")
        workspaces.save(state["id"], "flow", "db", "../escape.md", "lost", BASE,
                        current["original"].source_hash)
    assert github.calls == []


def test_semantic_error_can_be_saved_as_a_visibly_invalid_draft(tmp_path):
    workspaces, state, github = setup(tmp_path)
    view = workspaces.preview(state["id"], "flow", "db", "first", "Use § missing-op\n")
    assert view["validation_errors"]["db"] == ["missing-op"]
    assert save(workspaces, state, "first", "Use § missing-op\n")["status"] == "saved"


def test_commit_success_pr_failure_retries_only_the_pr(tmp_path):
    workspaces, state, github = setup(tmp_path)
    github.fail_pr = True
    result = save(workspaces, state, "first", "already saved\n")
    assert result["status"] == "saved_pr_pending"
    commit = result["workspace"]["head_oid"]
    with pytest.raises(GitError, match="retry its PR"):
        save(workspaces, state, "second", "not saved\n")
    github.fail_pr = False
    retried = workspaces.retry_pull(state["id"])
    assert retried["status"] == "saved" and retried["workspace"]["head_oid"] == commit
    assert github.calls.count("commit") == 1


@pytest.mark.parametrize("change", [{"state": "closed"}, {"draft": False}])
def test_closed_or_ready_pr_is_not_silently_reused(tmp_path, change):
    workspaces, state, github = setup(tmp_path)
    save(workspaces, state, "first", "saved\n")
    github.pr.update(change)
    before = list(github.calls)
    with pytest.raises(GitConflict):
        save(workspaces, state, "second", "refused\n")
    assert github.calls == before


def test_uncertain_commit_is_reconciled_without_recommitting_or_storing_drafts(tmp_path):
    workspaces, state, github = setup(tmp_path)
    github.uncertain_commit = True
    with pytest.raises(GitUncertain):
        save(workspaces, state, "first", "unique sensitive draft\n")
    pending = workspaces.store.get(state["id"])
    assert pending["pending_action"] == "commit"
    assert "unique sensitive draft" not in str(pending)
    github.uncertain_commit = False
    reconciled = workspaces.reconcile(state["id"])
    assert reconciled["pending_pr"] and reconciled["pending_action"] is None
    assert workspaces.retry_pull(state["id"])["status"] == "saved"
    assert github.calls.count("commit") == 1


def test_branch_collision_is_never_adopted_or_reset(tmp_path):
    workspaces, state, github = setup(tmp_path)
    github.branches[state["branch"]] = BASE
    with pytest.raises(GitConflict, match="already exists"):
        workspaces.preview(state["id"], "flow", "db")
    assert github.calls == [] and not workspaces.store.get(state["id"])["branch_created"]


def test_local_workspace_updates_reject_an_intervening_request(tmp_path):
    workspaces, state, github = setup(tmp_path)
    old = copy.deepcopy(state)
    workspaces.store.update(state, pending_action="commit")
    with pytest.raises(GitConflict, match="another request"):
        workspaces.store.update(old, pending_action="pull")


def test_configuration_identity_is_bound_to_the_workspace(tmp_path):
    workspaces, state, github = setup(tmp_path)
    workspaces.repositories["core"] = Repository("core", "someone-else", "repo")
    with pytest.raises(GitConflict, match="configuration changed"):
        workspaces.bound(state["id"])
