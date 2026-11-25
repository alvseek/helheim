"""GitHub data access through gh, with explicit hosted credential isolation."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote

from procedure_composer import MemorySource

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_]+\Z")
_WORKSPACE_BRANCH = re.compile(r"helheim/[0-9a-f]{32}\Z")
_COMMIT_MUTATION = """mutation($input:CreateCommitOnBranchInput!){
          createCommitOnBranch(input:$input){commit{oid url}}
        }"""
_SAFE_ENV = {
    "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
    "TEMP", "TMP", "TMPDIR", "LANG",
}


class GitError(RuntimeError):
    """An actionable failure with optional safe GitHub diagnostics."""

    def __init__(self, message, *, upstream_status=None, details=None, stage=None):
        super().__init__(message)
        self.upstream_status = upstream_status
        self.details = details
        self.stage = stage

    def diagnostic(self):
        return {"upstream_status": self.upstream_status, "details": self.details,
                "stage": self.stage}


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

    def __init__(self, repository: Repository, *, runner=None, credential_source=None):
        self.repository = repository
        self.runner = runner or subprocess.run
        self.credential_source = credential_source
        self._blobs: dict[str, bytes] = {}
        self._snapshots: dict[str, MemorySource] = {}

    def request(self, endpoint: str, *, method="GET", data=None, missing=False):
        if self.credential_source is not None:
            self._validate_hosted_request(endpoint, method, data)
        args = ["gh", "api", "--hostname", "github.com", "--include", "--method", method, endpoint]
        payload = None
        if data is not None:
            args.extend(["--input", "-"])
            payload = json.dumps(data, ensure_ascii=False)
        token = None
        try:
            kwargs = dict(input=payload, text=True, encoding="utf-8",
                          capture_output=True, timeout=30, check=False)
            if self.credential_source is None:
                result = self.runner(args, **kwargs)
            else:
                try:
                    token = self.credential_source()
                except Exception:
                    raise GitError("hosted GitHub credential is unavailable") from None
                if not isinstance(token, str) or not _TOKEN.fullmatch(token):
                    raise GitError("hosted GitHub credential is missing or invalid")
                env = {key: value for key, value in os.environ.items()
                       if key.upper() in _SAFE_ENV or key.upper().startswith("LC_")}
                with tempfile.TemporaryDirectory(prefix="helheim-gh-") as isolated:
                    env.update(GH_TOKEN=token, GH_CONFIG_DIR=isolated,
                               GH_PROMPT_DISABLED="1", HOME=isolated, USERPROFILE=isolated)
                    result = self.runner(args, env=env, **kwargs)
        except FileNotFoundError as exc:
            if self.credential_source is not None:
                raise GitError("gh is unavailable for hosted procedure editing") from exc
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
        details = self._error_details(body, data, token)
        if status in (409, 412, 422):
            raise GitConflict(
                "GitHub refused the request.", upstream_status=status, details=details
            )
        if status >= 500 and method != "GET":
            raise GitUncertain("GitHub write outcome is unknown.", upstream_status=status,
                               details=details)
        if not 200 <= status < 300:
            raise GitError("GitHub refused the request.", upstream_status=status, details=details)
        try:
            parsed = json.loads(body)
        except (TypeError, json.JSONDecodeError) as exc:
            if method != "GET":
                raise GitUncertain(
                    "GitHub write returned an unreadable result; reconcile before retrying"
                ) from exc
            raise GitError("GitHub returned an unreadable response") from exc
        if endpoint == "graphql" and isinstance(parsed, dict) and parsed.get("errors"):
            raise GitConflict("GitHub refused the commit.", upstream_status=status,
                              details=self._error_details(body, data, token))
        return parsed

    @staticmethod
    def _error_details(body, data, token):
        """Expose only GitHub's message and validation fields, not headers or request data."""
        try:
            parsed = json.loads(body)
        except (TypeError, ValueError):
            return None
        if not isinstance(parsed, dict):
            return None
        sensitive = [token] if token else []
        try:
            additions = data["variables"]["input"]["fileChanges"]["additions"]
            for item in additions:
                encoded = item["contents"]
                source = base64.b64decode(encoded).decode("utf-8")
                sensitive.extend((encoded, source))
                sensitive.extend(line for line in source.splitlines() if len(line) >= 4)
        except (KeyError, TypeError, ValueError, UnicodeError):
            pass

        def safe(text):
            if not isinstance(text, str):
                return None
            for value in sensitive:
                if value:
                    text = text.replace(value, "[redacted]")
            text = re.sub(r"(?im)^.*(?:authorization|x-api-key|cookie):.*$", "[redacted]", text)
            return re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b",
                          "[redacted]", text)

        parts = []
        message = safe(parsed.get("message"))
        if message:
            parts.append(message)
        errors = parsed.get("errors")
        if isinstance(errors, list):
            for item in errors:
                if isinstance(item, str):
                    parts.append(safe(item))
                elif isinstance(item, dict):
                    fields = [safe(item.get(key))
                              for key in ("resource", "field", "code", "message")]
                    parts.append(": ".join(value for value in fields if value))
        return "\n".join(value for value in parts if value) or None

    def _validate_hosted_request(self, endpoint, method, data):
        if endpoint == "graphql":
            try:
                change = data["variables"]["input"]
                branch = change["branch"]
                changes = change["fileChanges"]
                additions = changes["additions"]
                path = additions[0]["path"]
                safe = (
                    method == "POST" and data["query"] == _COMMIT_MUTATION
                    and set(change) == {"branch", "expectedHeadOid", "message", "fileChanges"}
                    and set(changes) == {"additions"} and len(additions) == 1
                    and branch["repositoryNameWithOwner"].lower()
                    == f"{self.repository.owner}/{self.repository.name}".lower()
                    and bool(_WORKSPACE_BRANCH.fullmatch(branch["branchName"]))
                    and bool(_OID.fullmatch(change["expectedHeadOid"]))
                    and path.startswith(self.repository.path("procedures") + "/")
                    and path.endswith(".md") and "\\" not in path and ":" not in path
                    and not any(part in ("", ".", "..") for part in path.split("/"))
                )
            except (AttributeError, IndexError, KeyError, TypeError):
                safe = False
            if not safe:
                raise GitError("hosted GitHub request is outside the approved repository")
            return
        if not isinstance(endpoint, str):
            raise GitError("hosted GitHub request is outside the approved repository")
        path = endpoint.split("?", 1)[0]
        while unquote(path) != path:
            path = unquote(path)
        if (not path.startswith(self.repository.api + "/") or "\\" in path or "#" in endpoint
                or any(part in (".", "..") for part in path.split("/"))):
            raise GitError("hosted GitHub request is outside the approved repository")
        if method == "GET":
            return
        if (method == "POST" and isinstance(data, dict)
                and endpoint == self.repository.api + "/git/refs"
                and isinstance(data.get("ref"), str)
                and data["ref"].startswith("refs/heads/")
                and _WORKSPACE_BRANCH.fullmatch(data["ref"].removeprefix("refs/heads/"))
                and isinstance(data.get("sha"), str) and _OID.fullmatch(data["sha"])):
            return
        if (method == "POST" and isinstance(data, dict)
                and endpoint == self.repository.api + "/pulls"
                and isinstance(data.get("head"), str) and _WORKSPACE_BRANCH.fullmatch(data["head"])
                and data.get("base") == self.repository.base_ref and data.get("draft") is True):
            return
        raise GitError("hosted GitHub write is outside the draft workspace")

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
        # Whole owning documents live anywhere beneath procedures/: procedures, components
        # and the consolidated storage-backend files. Nothing outside that tree is a
        # catalogue source, and no link or submodule is ever followed.
        root = self.repository.path("procedures")
        ancestors = {"/".join(root.split("/")[:i]) for i in range(1, root.count("/") + 2)}
        files = {}
        for entry in payload["tree"]:
            path = entry.get("path", "")
            selected = path.startswith(root + "/")
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
        if not any(path.startswith("procedures/") for path in files):
            raise GitError("this revision has no supported procedure document catalog")
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
        prefix = self.repository.path("procedures") + "/"
        if (
            not path.startswith(prefix) or not path.endswith(".md")
            or "\\" in path or ":" in path
            or any(part in ("", ".", "..") for part in path.split("/"))
        ):
            raise GitError("write target is outside the configured procedure document tree")
        payload = self.request("graphql", method="POST", data={
            "query": _COMMIT_MUTATION,
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
