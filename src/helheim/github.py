"""GitHub data access through the existing gh login; no token is extracted or stored."""

from __future__ import annotations

import base64
import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from procedure_composer import MemorySource

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class GitError(RuntimeError):
    """A bounded actionable failure, without arbitrary process output or credentials."""


class GitConflict(GitError):
    """The selected source or workspace no longer matches the expected state."""


class GitUncertain(GitError):
    """A write may have reached GitHub; reconcile before retrying."""


@dataclass(frozen=True)
class Repository:
    key: str
    owner: str
    name: str
    base_ref: str = "main"
    catalog_root: str = ""

    def __post_init__(self):
        if not all(_NAME.fullmatch(value) for value in (self.key, self.owner, self.name)):
            raise ValueError("repository key, owner and name must be simple GitHub identifiers")
        root = self.catalog_root
        if root and (
            root.startswith("/") or "\\" in root or ":" in root
            or any(part in ("", ".", "..") for part in root.split("/"))
        ):
            raise ValueError("catalog_root must be a confined repository-relative directory")
        if not self.base_ref or self.base_ref.startswith("-"):
            raise ValueError("base_ref must identify a Git revision")

    @property
    def api(self):
        return f"repos/{self.owner}/{self.name}"

    def path(self, relative: str) -> str:
        return f"{self.catalog_root}/{relative}" if self.catalog_root else relative


def oid(value: Any) -> str:
    if not isinstance(value, str) or not _OID.fullmatch(value):
        raise GitError("GitHub returned an invalid object identity")
    return value


class GitHub:
    """Replaceable adapter. runner is injected for tests; production never uses a shell."""

    def __init__(self, repository: Repository, *, runner=subprocess.run):
        self.repository = repository
        self.runner = runner
        self._blobs: dict[str, bytes] = {}
        self._snapshots: dict[str, MemorySource] = {}

    def request(self, endpoint: str, *, method="GET", data=None, missing=False):
        args = ["gh", "api", "--hostname", "github.com", "--include", "--method", method, endpoint]
        payload = None
        if data is not None:
            args.extend(["--input", "-"])
            payload = json.dumps(data, ensure_ascii=False)
        try:
            result = self.runner(
                args, input=payload, text=True, encoding="utf-8",
                capture_output=True, timeout=30, check=False,
            )
        except FileNotFoundError as exc:
            raise GitError(
                "gh is not installed; install it and sign in with gh auth login"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            if method != "GET":
                raise GitUncertain(
                    "GitHub write timed out; reconcile its outcome before retrying"
                ) from exc
            raise GitError("GitHub did not answer; check connectivity and gh auth status") from exc
        output = result.stdout.replace("\r\n", "\n")
        headers, separator, body = output.partition("\n\n")
        status_match = re.match(r"HTTP/\S+\s+(\d{3})", headers)
        if not separator or not status_match:
            if method != "GET":
                raise GitUncertain(
                    "GitHub write response was unavailable; reconcile before retrying"
                )
            raise GitError(
                "GitHub response was unavailable; check gh auth status and repository access"
            )
        status = int(status_match.group(1))
        if missing and status == 404:
            return None
        if status in (409, 412, 422):
            raise GitConflict(
                "GitHub refused the changed or conflicting workspace; reload before saving"
            )
        if status >= 500 and method != "GET":
            raise GitUncertain("GitHub write outcome is unknown; reconcile before retrying")
        if not 200 <= status < 300:
            raise GitError(f"GitHub request refused (HTTP {status}); check repository access")
        try:
            return json.loads(body)
        except (TypeError, json.JSONDecodeError) as exc:
            if method != "GET":
                raise GitUncertain(
                    "GitHub write returned an unreadable result; reconcile before retrying"
                ) from exc
            raise GitError("GitHub returned an unreadable response") from exc

    def resolve(self, reference: str) -> str:
        row = self.request(f"{self.repository.api}/commits/{quote(reference, safe='')}")
        return oid(row.get("sha"))

    def head(self, branch: str) -> str | None:
        row = self.request(
            f"{self.repository.api}/git/ref/heads/{quote(branch, safe='')}", missing=True
        )
        return oid(row.get("object", {}).get("sha")) if row else None

    def sources(self, commit: str) -> MemorySource:
        commit = oid(commit)
        if commit in self._snapshots:
            return self._snapshots[commit]
        row = self.request(f"{self.repository.api}/git/commits/{commit}")
        tree = oid(row.get("tree", {}).get("sha"))
        payload = self.request(f"{self.repository.api}/git/trees/{tree}?recursive=1")
        if payload.get("truncated") or not isinstance(payload.get("tree"), list):
            raise GitError("GitHub tree is incomplete; source editing is unavailable")
        directories = tuple(self.repository.path(f"procedures/{part}")
                            for part in ("assemblies", "sections"))
        ancestors = {"/".join(directory.split("/")[:i])
                     for directory in directories for i in range(1, len(directory.split("/")) + 1)}
        files = {}
        for entry in payload["tree"]:
            path = entry.get("path", "")
            selected = any(path.startswith(directory + "/") for directory in directories)
            if not selected and path not in ancestors:
                continue
            if entry.get("mode") in ("120000", "160000"):
                raise GitError(
                    "catalog contains a symlink or submodule; edit its owning repository"
                )
            if not selected or entry.get("type") == "tree":
                continue
            if entry.get("type") != "blob" or entry.get("mode") not in ("100644", "100755"):
                raise GitError("catalog contains a non-regular source")
            blob_id = oid(entry.get("sha"))
            if blob_id in self._blobs:
                raw = self._blobs[blob_id]
            else:
                blob = self.request(f"{self.repository.api}/git/blobs/{blob_id}")
                if blob.get("encoding") != "base64":
                    raise GitError("GitHub returned an unsupported blob encoding")
                try:
                    raw = base64.b64decode("".join(blob["content"].split()), validate=True)
                except (KeyError, ValueError, TypeError) as exc:
                    raise GitError("GitHub returned an invalid source blob") from exc
                self._blobs[blob_id] = raw
            prefix = self.repository.catalog_root + "/" if self.repository.catalog_root else ""
            files[path.removeprefix(prefix)] = raw
        if not any(path.startswith("procedures/assemblies/") for path in files):
            raise GitError("this revision has no supported procedure assembly catalog")
        snapshot = MemorySource(files)
        self._snapshots[commit] = snapshot
        return snapshot

    def create_branch(self, branch: str, commit: str):
        row = self.request(f"{self.repository.api}/git/refs", method="POST",
                           data={"ref": f"refs/heads/{branch}", "sha": oid(commit)})
        if (not isinstance(row, dict) or row.get("ref") != f"refs/heads/{branch}"
                or row.get("object", {}).get("sha") != commit):
            raise GitUncertain("branch creation was not confirmed; reconcile before retrying")

    def commit(self, branch: str, expected_head: str, path: str, content: bytes, save_id: str):
        prefix = self.repository.path("procedures/sections") + "/"
        if (
            not path.startswith(prefix) or not path.endswith(".md")
            or "\\" in path or ":" in path
            or any(part in ("", ".", "..") for part in path.split("/"))
        ):
            raise GitError("write target is outside the configured source-section directory")
        mutation = """mutation($input:CreateCommitOnBranchInput!){
          createCommitOnBranch(input:$input){commit{oid url}}
        }"""
        payload = self.request("graphql", method="POST", data={
            "query": mutation,
            "variables": {"input": {
                "branch": {"repositoryNameWithOwner":
                           f"{self.repository.owner}/{self.repository.name}", "branchName": branch},
                "expectedHeadOid": oid(expected_head),
                "message": {"headline": f"Edit procedure source ({save_id})"},
                "fileChanges": {"additions": [{
                    "path": path, "contents": base64.b64encode(content).decode("ascii"),
                }]},
            }},
        })
        if not isinstance(payload, dict):
            raise GitUncertain("commit response is unreadable; reconcile before retrying")
        data = payload.get("data")
        result = data.get("createCommitOnBranch") if isinstance(data, dict) else None
        commit = result.get("commit") if isinstance(result, dict) else None
        if isinstance(commit, dict):
            try:
                return oid(commit.get("oid"))
            except GitError as exc:
                raise GitUncertain(
                    "commit identity is unreadable; reconcile before retrying"
                ) from exc
        if payload.get("errors"):
            raise GitConflict(
                "GitHub commit precondition failed; inspect the branch before retrying"
            )
        raise GitUncertain("commit outcome is unknown; reconcile before retrying")

    def commit_info(self, commit: str):
        return self.request(f"{self.repository.api}/git/commits/{oid(commit)}")

    def pull(self, number: int):
        return self.request(f"{self.repository.api}/pulls/{int(number)}")

    def ensure_pull(self, branch: str, workspace_id: str):
        head = quote(f"{self.repository.owner}:{branch}", safe="")
        base = quote(self.repository.base_ref, safe="")
        rows = self.request(f"{self.repository.api}/pulls?state=all&head={head}&base={base}")
        if not isinstance(rows, list):
            raise GitError("GitHub returned an unreadable PR listing")
        if rows:
            if len(rows) != 1 or rows[0].get("state") != "open" or not rows[0].get("draft"):
                raise GitConflict(
                    "workspace PR is closed, merged or no longer draft; start a new workspace"
                )
            return rows[0]
        return self.request(f"{self.repository.api}/pulls", method="POST", data={
            "head": branch, "base": self.repository.base_ref,
            "title": f"Procedure edits {workspace_id}",
            "body": "Saved source edits for review. Saving does not merge or deploy these changes.",
            "draft": True,
        })
