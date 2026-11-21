import copy
import hashlib
import subprocess

import pytest
from procedure_composer import CompositionError, MemorySource

from helheim.github import GitConflict, GitError, GitHub, GitUncertain, Repository
from helheim.workspaces import Workspaces, WorkspaceStore

BASE = "a" * 40
STORE_PATH = "procedures/memory/storage-backends/db.md"
MARKDOWN_PATH = "procedures/memory/storage-backends/markdown.md"
RULES_PATH = "procedures/components/rules.md"
FLOW_PATH = "procedures/flow.md"


def document(identity, kind, body):
    return (
        "+++\n"
        "format_version = 2\n"
        f'id = "{identity}"\n'
        f'kind = "{kind}"\n'
        'title = "Flow"\n'
        'description = "Do it."\n'
        "+++\n"
        f"{body}"
    ).encode()


def backend(name, read, write):
    return (
        f'@start region id="flow.read"\n{read}\n@end region\n\n'
        f'@start region id="flow.write"\n{write}\n@end region\n'
    ).encode()


FLOW_BODY = (
    "## Procedure\n\nRead, then write.\n\n"
    '@start storage id="flow.read" '
    'ref="procedures/memory/storage-backends/{backend}.md" target="flow.read"\n'
    "@end storage\n\n"
    "Then continue.\n\n"
    '@start component id="flow.rules" ref="procedures/components/rules.md"\n'
    "@end component\n\n"
    '@start storage id="flow.write" '
    'ref="procedures/memory/storage-backends/{backend}.md" target="flow.write"\n'
    "@end storage\n"
)

FILES = {
    FLOW_PATH: document("flow", "procedure", FLOW_BODY),
    RULES_PATH: document("rules", "component", "Shared rules.\n"),
    STORE_PATH: backend("db", "DB read", "DB write"),
    MARKDOWN_PATH: backend("markdown", "MD read", "MD write"),
}

LEGACY_FILES = {
    "procedures/assemblies/flow.toml": (
        b'format_version=1\nid="flow"\ntitle="Flow"\ndescription="Do it."\n'
        b'[[sections]]\nid="first"\nsource="procedures/sections/first.md"\n'
    ),
    "procedures/sections/first.md": b"alpha\n",
}


class FakeGitHub:
    def __init__(self, repository, files=None):
        self.repository = repository
        self.commits = {BASE: dict(FILES if files is None else files)}
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


def setup(tmp_path, files=None):
    repository = Repository("core", "owner", "repo")
    github = FakeGitHub(repository, files)
    store = WorkspaceStore(tmp_path / "workspace.db")
    workspaces = Workspaces(store, (repository,), adapter=lambda repository: github)
    state = workspaces.create("core")
    return workspaces, state, github


def open_document(workspaces, state, name, backend="db"):
    return workspaces.preview(state["id"], "flow", backend, name)


def save(workspaces, state, name, draft, backend="db"):
    view = open_document(workspaces, state, name, backend)
    return workspaces.save(state["id"], "flow", backend, name, draft,
                           view["head"], view["original"].sha256)


def revise(raw, old, new):
    text = raw.decode("utf-8")
    assert old in text
    return text.replace(old, new)


def test_open_and_preview_do_not_write_and_two_region_edits_preserve_each_other(tmp_path):
    workspaces, state, github = setup(tmp_path)
    read_draft = revise(FILES[STORE_PATH], "DB read", "DB read edited")
    preview = workspaces.preview(state["id"], "flow", "db", "db", read_draft)
    assert preview["rendered"]["db"].text == (
        "## Procedure\n\nRead, then write.\n\nDB read edited\n\nThen continue.\n\n"
        "Shared rules.\n\nDB write\n"
    )
    assert preview["rendered"]["markdown"].text.count("MD read") == 1
    rows = {row["id"]: row for row in preview["documents"]["db"]}
    assert rows["db"] == {
        "id": "db", "kind": "backend", "path": STORE_PATH,
        "sha256": hashlib.sha256(read_draft.encode()).hexdigest(),
    }
    assert github.calls == []

    one = save(workspaces, state, "db", read_draft)
    # The author reopens the saved document and edits the other region in it.
    write_draft = revise(read_draft.encode(), "DB write", "DB write edited")
    two = save(workspaces, state, "db", write_draft)
    assert one["workspace"]["pull_number"] == two["workspace"]["pull_number"] == 1
    assert github.calls.count("branch") == 1 and github.calls.count("commit") == 2
    head = github.head(state["branch"])
    assert github.sources(head).read(STORE_PATH) == write_draft.encode()
    rendered = workspaces.preview(state["id"], "flow", "db")["rendered"]["db"].text
    # The second whole-document save carried the first region's edit with it.
    assert "DB read edited\n" in rendered and "DB write edited\n" in rendered
    assert two["document"] == {
        "id": "db", "kind": "backend", "path": STORE_PATH,
        "sha256": hashlib.sha256(write_draft.encode()).hexdigest(),
    }


def test_procedure_then_component_saves_reuse_one_pr(tmp_path):
    workspaces, state, github = setup(tmp_path)
    flow_draft = revise(FILES[FLOW_PATH], "Then continue.", "Then continue carefully.")
    one = save(workspaces, state, "flow", flow_draft)
    rules_draft = revise(FILES[RULES_PATH], "Shared rules.", "Shared rules, revised.")
    two = save(workspaces, state, "rules", rules_draft)
    assert one["workspace"]["pull_number"] == two["workspace"]["pull_number"] == 1
    assert github.calls.count("branch") == 1 and github.calls.count("commit") == 2
    assert two["document"]["path"] == RULES_PATH
    head = github.head(state["branch"])
    assert github.sources(head).read(RULES_PATH) == rules_draft.encode()
    assert github.sources(head).read(FLOW_PATH) == flow_draft.encode()
    assert two["dependents"] == ["flow"]


def test_stale_document_and_unknown_identity_never_write(tmp_path):
    workspaces, state, github = setup(tmp_path)
    with pytest.raises(GitConflict):
        workspaces.save(state["id"], "flow", "db", "db", revise(FILES[STORE_PATH], "DB", "X"),
                        BASE, "stale-hash")
    current = open_document(workspaces, state, "db")
    for identity in ("../escape.md", "/etc/passwd", "not-a-document", "first"):
        with pytest.raises(GitError):
            workspaces.save(state["id"], "flow", "db", identity, "lost\n", BASE,
                            current["original"].sha256)
    assert github.calls == []


def test_identity_and_kind_changes_are_refused_before_writing(tmp_path):
    workspaces, state, github = setup(tmp_path)
    renamed = revise(FILES[FLOW_PATH], 'id = "flow"', 'id = "other"')
    with pytest.raises(CompositionError, match="filename must identify"):
        save(workspaces, state, "flow", renamed)
    prose_only = "## Procedure\n\nNo metadata any more.\n"
    with pytest.raises(GitError, match="same document identity and kind"):
        save(workspaces, state, "flow", prose_only)
    assert github.calls == []


def test_missing_region_is_refused_before_writing(tmp_path):
    workspaces, state, github = setup(tmp_path)
    without_write = '@start region id="flow.read"\nDB read\n@end region\n'
    with pytest.raises(CompositionError, match="missing region"):
        save(workspaces, state, "db", without_write)
    assert github.calls == []


def test_semantic_error_can_be_saved_as_a_visibly_invalid_draft(tmp_path):
    workspaces, state, github = setup(tmp_path)
    draft = revise(FILES[STORE_PATH], "DB read", "Use § missing-op")
    view = workspaces.preview(state["id"], "flow", "db", "db", draft)
    assert view["validation_errors"]["db"] == ["missing-op"]
    assert save(workspaces, state, "db", draft)["status"] == "saved"


def test_old_format_catalog_and_old_workspace_are_refused_without_deletion(tmp_path):
    repository = Repository("core", "owner", "repo")
    legacy = FakeGitHub(repository, LEGACY_FILES)
    store = WorkspaceStore(tmp_path / "workspace.db")
    workspaces = Workspaces(store, (repository,), adapter=lambda repository: legacy)
    with pytest.raises(CompositionError, match="format_version 1"):
        workspaces.create("core")
    assert store.list() == [] and legacy.calls == []

    workspaces, state, github = setup(tmp_path)
    old = workspaces.store.update(state, document_format=1)
    with pytest.raises(GitError, match="predates whole-document"):
        workspaces.preview(old["id"], "flow", "db")
    assert workspaces.store.get(old["id"])["document_format"] == 1
    assert github.calls == []


def test_whole_file_is_the_only_write_target(tmp_path):
    workspaces, state, github = setup(tmp_path)
    with pytest.raises(GitError, match="no document changes"):
        save(workspaces, state, "db", FILES[STORE_PATH].decode("utf-8"))
    assert github.calls == []
    draft = revise(FILES[STORE_PATH], "DB read", "DB read once")
    result = save(workspaces, state, "db", draft)
    assert result["document"]["path"] == STORE_PATH
    assert result["document"]["sha256"] == hashlib.sha256(draft.encode()).hexdigest()
    assert result["applicability"] == ("db",)


def test_commit_success_pr_failure_retries_only_the_pr(tmp_path):
    workspaces, state, github = setup(tmp_path)
    github.fail_pr = True
    draft = revise(FILES[STORE_PATH], "DB read", "Already saved")
    result = save(workspaces, state, "db", draft)
    assert result["status"] == "saved_pr_pending"
    commit = result["workspace"]["head_oid"]
    with pytest.raises(GitError, match="retry its PR"):
        save(workspaces, state, "rules", "not saved\n")
    github.fail_pr = False
    retried = workspaces.retry_pull(state["id"])
    assert retried["status"] == "saved" and retried["workspace"]["head_oid"] == commit
    assert github.calls.count("commit") == 1


@pytest.mark.parametrize("failure", ["network", "closed", "ready", "head", "base"])
def test_postcommit_pr_check_failure_returns_confirmed_save(tmp_path, monkeypatch, failure):
    workspaces, state, github = setup(tmp_path)
    save(workspaces, state, "db", revise(FILES[STORE_PATH], "DB read", "First saved"))
    original_pull = github.pull
    reads = 0

    def pull(number):
        nonlocal reads
        reads += 1
        if reads >= 2:
            if failure == "network":
                raise GitError("GitHub did not answer")
            github.pr.update({
                "closed": {"state": "closed"},
                "ready": {"draft": False},
                "head": {"head": {"ref": "another-branch"}},
                "base": {"base": {"ref": "another-base"}},
            }[failure])
        return original_pull(number)

    monkeypatch.setattr(github, "pull", pull)
    draft = revise(FILES[STORE_PATH], "DB write", "Second saved")
    result = save(workspaces, state, "db", draft)
    head = github.head(state["branch"])
    assert result["status"] == "saved_pr_pending"
    assert result["head"] == result["workspace"]["head_oid"] == head
    assert result["document"]["sha256"] == hashlib.sha256(draft.encode()).hexdigest()
    assert "Second saved\n" in result["rendered"]["db"]
    assert result["validation_errors"] == {"markdown": [], "db": []}
    assert result["error"] and result["pr_uncertain"] is False
    assert result["workspace"]["pending_pr"]
    assert result["workspace"]["pending_action"] is None
    assert github.sources(head).read(STORE_PATH) == draft.encode()
    before = list(github.calls)
    retried = workspaces.retry_pull(state["id"])
    assert retried["status"] == "saved_pr_pending" and retried["error"]
    assert github.calls == before  # No commit or PR reuse after the failed check.
    with pytest.raises(GitError, match="retry its PR"):
        save(workspaces, state, "rules", "not saved\n")
    if failure == "network":
        monkeypatch.setattr(github, "pull", original_pull)
        assert workspaces.retry_pull(state["id"])["status"] == "saved"
        assert github.head(state["branch"]) == head
    assert github.calls.count("commit") == 2


@pytest.mark.parametrize("change", [{"state": "closed"}, {"draft": False}])
def test_closed_or_ready_pr_is_not_silently_reused(tmp_path, change):
    workspaces, state, github = setup(tmp_path)
    save(workspaces, state, "db", revise(FILES[STORE_PATH], "DB read", "Saved"))
    github.pr.update(change)
    before = list(github.calls)
    with pytest.raises(GitConflict):
        save(workspaces, state, "rules", "refused\n")
    assert github.calls == before


def test_uncertain_commit_is_reconciled_without_recommitting_or_storing_drafts(tmp_path):
    workspaces, state, github = setup(tmp_path)
    github.uncertain_commit = True
    draft = revise(FILES[STORE_PATH], "DB read", "Unique sensitive draft")
    with pytest.raises(GitUncertain):
        save(workspaces, state, "db", draft)
    pending = workspaces.store.get(state["id"])
    assert pending["pending_action"] == "commit"
    assert pending["pending_save"]["path"] == STORE_PATH
    assert "Unique sensitive draft" not in str(pending)
    github.uncertain_commit = False
    reconciled = workspaces.reconcile(state["id"])
    assert reconciled["pending_pr"] and reconciled["pending_action"] is None
    assert workspaces.retry_pull(state["id"])["status"] == "saved"
    assert github.calls.count("commit") == 1


@pytest.mark.parametrize("operation", ["create_branch", "commit"])
def test_lost_response_retains_intent_after_remote_write(tmp_path, monkeypatch, operation):
    workspaces, state, github = setup(tmp_path)
    second = open_document(workspaces, state, "db")
    original = getattr(github, operation)
    adapter = GitHub(github.repository, runner=lambda *args, **kwargs:
                     subprocess.CompletedProcess([], 1, "", "connection reset"))

    def lose_response(*args):
        original(*args)
        adapter.request("graphql", method="POST", data={})

    monkeypatch.setattr(github, operation, lose_response)
    draft = revise(FILES[STORE_PATH], "DB read", "Saved with lost response")
    with pytest.raises(GitUncertain, match="reconcile"):
        save(workspaces, state, "db", draft)
    pending = workspaces.store.get(state["id"])
    assert pending["pending_action"] == operation
    assert pending["pending_save"]["new_hash"] == hashlib.sha256(draft.encode()).hexdigest()
    assert "Saved with lost response" not in str(pending)
    before = list(github.calls)
    with pytest.raises(GitUncertain, match="prior save"):
        workspaces.save(state["id"], "flow", "db", "rules", "refused\n",
                        second["head"], second["original"].sha256)
    assert github.calls == before
    if operation == "commit":
        reconciled = workspaces.reconcile(state["id"])
        assert reconciled["pending_pr"] and reconciled["pending_action"] is None
        assert workspaces.retry_pull(state["id"])["status"] == "saved"
        assert github.calls.count("commit") == 1
    else:
        with pytest.raises(GitUncertain, match="never adopt or reset"):
            workspaces.reconcile(state["id"])
        assert workspaces.store.get(state["id"]) == pending
        assert github.head(state["branch"]) == BASE
        assert github.calls == ["branch"]


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
