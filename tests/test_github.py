import base64
import json
import subprocess

import pytest

from helheim.github import GitError, GitHub, GitUncertain, Repository

OID = "a" * 40
NEXT = "b" * 40


def response(data, status=200, returncode=0):
    return subprocess.CompletedProcess(
        [], returncode, f"HTTP/2.0 {status} Result\ncontent-type: application/json\n\n"
        + json.dumps(data), "not exposed",
    )


def test_commit_uses_atomic_head_precondition_json_stdin_and_no_shell():
    calls = []

    def runner(args, **options):
        calls.append((args, options))
        return response({"data": {"createCommitOnBranch": {"commit": {"oid": NEXT}}}})

    github = GitHub(Repository("core", "owner", "framework"), runner=runner)
    raw = "é\r\n$(never-a-command)\n".encode()
    assert github.commit("helheim/workspace", OID, "procedures/flow.md",
                         raw, "save-id") == NEXT
    args, options = calls[0]
    assert args[:2] == ["gh", "api"] and "--input" in args
    assert not options.get("shell", False)
    payload = json.loads(options["input"])["variables"]["input"]
    assert payload["expectedHeadOid"] == OID
    assert base64.b64decode(payload["fileChanges"]["additions"][0]["contents"]) == raw
    assert "force" not in json.dumps(payload).lower()


@pytest.mark.parametrize("target", [
    "../escape.md", "/absolute.md", "procedures/flow.toml",
    "procedures/../../escape.md", r"procedures\flow.md", "elsewhere/flow.md",
])
def test_adapter_confines_writable_paths(target):
    github = GitHub(Repository("core", "owner", "repo"),
                    runner=lambda *args, **kwargs: pytest.fail("no request allowed"))
    with pytest.raises(GitError):
        github.commit("branch", OID, target, b"body", "save")


def test_write_server_error_is_uncertain_not_a_claim_of_failure():
    github = GitHub(Repository("core", "owner", "repo"),
                    runner=lambda *args, **kwargs: response({}, 503, 1))
    with pytest.raises(GitUncertain):
        github.create_branch("branch", OID)
    with pytest.raises(GitError):
        github.resolve("main")


def test_process_failure_does_not_expose_credentials_or_arbitrary_output():
    github = GitHub(Repository("core", "owner", "repo"),
                    runner=lambda *args, **kwargs:
                    subprocess.CompletedProcess([], 1, "", "secret-credential"))
    with pytest.raises(GitError) as caught:
        github.resolve("main")
    assert "secret-credential" not in str(caught.value)


@pytest.mark.parametrize("output", ["", "HTTP/2.0 201 Created\n", "invalid\n\n{}"])
@pytest.mark.parametrize("operation", ["branch", "commit"])
def test_missing_write_response_is_uncertain(output, operation):
    github = GitHub(Repository("core", "owner", "repo"),
                    runner=lambda *args, **kwargs:
                    subprocess.CompletedProcess([], 1, output, "secret-credential"))
    with pytest.raises(GitUncertain, match="reconcile") as caught:
        if operation == "branch":
            github.create_branch("branch", OID)
        else:
            github.commit("branch", OID, "procedures/flow.md", b"body", "save")
    assert "secret-credential" not in str(caught.value)


def test_missing_read_response_remains_a_definite_read_failure():
    github = GitHub(Repository("core", "owner", "repo"),
                    runner=lambda *args, **kwargs: subprocess.CompletedProcess([], 1, "", ""))
    with pytest.raises(GitError) as caught:
        github.resolve("main")
    assert not isinstance(caught.value, GitUncertain)


def test_missing_gh_is_not_an_uncertain_write():
    def missing(*args, **kwargs):
        raise FileNotFoundError("gh")

    github = GitHub(Repository("core", "owner", "repo"), runner=missing)
    with pytest.raises(GitError, match="not installed") as caught:
        github.create_branch("branch", OID)
    assert not isinstance(caught.value, GitUncertain)


@pytest.mark.parametrize("mode", ["120000", "160000"])
def test_sources_refuse_git_symlinks_and_submodules(mode):
    replies = iter([
        response({"tree": {"sha": NEXT}}),
        response({"tree": [{"path": "procedures/memory", "mode": mode, "type": "blob"}]}),
    ])
    github = GitHub(Repository("core", "owner", "repo"),
                    runner=lambda *args, **kwargs: next(replies))
    with pytest.raises(GitError, match="symlink or submodule"):
        github.sources(OID)


def test_truncated_tree_is_not_an_empty_catalog():
    replies = iter([response({"tree": {"sha": NEXT}}), response({"truncated": True, "tree": []})])
    github = GitHub(Repository("core", "owner", "repo"),
                    runner=lambda *args, **kwargs: next(replies))
    with pytest.raises(GitError, match="incomplete"):
        github.sources(OID)


def test_regular_remote_bytes_are_data_and_not_imported():
    document = (
        b'+++\nformat_version = 2\nid = "flow"\nkind = "procedure"\n'
        b'title = "Flow"\ndescription = "Do it."\n+++\n'
        b'@start storage id="flow.persist" '
        b'ref="procedures/memory/storage-backends/{backend}.md" target="flow.persist"\n'
        b"@end storage\n"
    )
    source = b"Never execute: import os; os.remove('anything')\r\n"
    replies = iter([
        response({"tree": {"sha": NEXT}}),
        response({"tree": [
            {"path": "procedures", "mode": "040000", "type": "tree", "sha": "e" * 40},
            {"path": "procedures/flow.md", "mode": "100644",
             "type": "blob", "sha": "c" * 40},
            {"path": "procedures/memory/storage-backends/db.md", "mode": "100644",
             "type": "blob", "sha": "d" * 40},
        ]}),
        response({"encoding": "base64", "content": base64.b64encode(document).decode()}),
        response({"encoding": "base64", "content": base64.b64encode(source).decode()}),
    ])
    github = GitHub(Repository("core", "owner", "repo"),
                    runner=lambda *args, **kwargs: next(replies))
    snapshot = github.sources(OID)
    assert snapshot.read("procedures/flow.md") == document
    assert snapshot.read("procedures/memory/storage-backends/db.md") == source
    assert github.sources(OID) is snapshot  # Immutable object identity, never a timed cache.
