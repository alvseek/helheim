"""Remote procedure workspaces; local persistence holds references, never source drafts."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from procedure_composer import Catalog, MemorySource

from .github import GitConflict, GitError, GitHub, GitUncertain, Repository


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def source_bytes(text: str) -> bytes:
    try:
        return text.encode("utf-8")
    except (AttributeError, UnicodeError) as exc:
        raise GitError("source must be valid UTF-8 text") from exc


class WorkspaceStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS procedure_workspace "
                "(id TEXT PRIMARY KEY, version INTEGER NOT NULL, state TEXT NOT NULL)"
            )

    def create(self, repository: Repository, commit: str):
        identity = uuid.uuid4().hex
        state = {
            "id": identity, "version": 0, "repository": asdict(repository),
            "base_oid": commit, "head_oid": commit, "branch": f"helheim/{identity}",
            "branch_created": False, "pull_number": None, "pull_url": None,
            "pending_action": None, "pending_save": None, "pending_pr": False,
        }
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("INSERT INTO procedure_workspace VALUES (?, 0, ?)",
                               (identity, json.dumps(state)))
        return state

    def get(self, identity: str):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            row = connection.execute(
                "SELECT state FROM procedure_workspace WHERE id=?", (identity,)
            ).fetchone()
        if row is None:
            raise GitError("no such procedure workspace")
        return json.loads(row[0])

    def list(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            rows = connection.execute(
                "SELECT state FROM procedure_workspace ORDER BY rowid DESC"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def update(self, state, **changes):
        updated = {**state, **changes, "version": state["version"] + 1}
        with closing(sqlite3.connect(self.path)) as connection, connection:
            result = connection.execute(
                "UPDATE procedure_workspace SET version=?, state=? WHERE id=? AND version=?",
                (updated["version"], json.dumps(updated), state["id"], state["version"]),
            )
            if result.rowcount != 1:
                raise GitConflict("workspace changed in another request; reload before continuing")
        return updated


class Workspaces:
    def __init__(self, store: WorkspaceStore, repositories: tuple[Repository, ...],
                 *, adapter=GitHub):
        self.store = store
        self.repositories = {repository.key: repository for repository in repositories}
        if len(self.repositories) != len(repositories):
            raise ValueError("procedure repository keys must be unique")
        self.adapter = adapter
        self._adapters = {}

    def github(self, repository):
        if repository not in self._adapters:
            self._adapters[repository] = self.adapter(repository)
        return self._adapters[repository]

    def repository(self, key: str):
        if key not in self.repositories:
            raise GitError("repository is not configured for procedure editing")
        return self.repositories[key]

    def bound(self, identity: str):
        state = self.store.get(identity)
        repository = self.repository(state["repository"]["key"])
        if asdict(repository) != state["repository"]:
            raise GitConflict("repository configuration changed; start a new workspace")
        return state, self.github(repository)

    def create(self, key: str):
        repository = self.repository(key)
        github = self.github(repository)
        commit = github.resolve(repository.base_ref)
        # Refuse a missing/unsupported catalog before creating even local workspace state.
        catalog = Catalog(github.sources(commit))
        catalog.validate()
        if not catalog.procedures:
            raise GitError("the configured catalog contains no procedures")
        return self.store.create(repository, commit)

    def snapshot(self, identity: str, revision="branch"):
        state, github = self.bound(identity)
        if revision == "base":
            source = github.sources(state["base_oid"])
            return state, state["base_oid"], source, Catalog(source)
        if revision != "branch":
            raise GitError("unknown revision selector")
        actual = github.head(state["branch"])
        if state["branch_created"]:
            if actual is None:
                raise GitConflict("workspace branch was deleted; start a new workspace")
            commit = actual
        else:
            if actual is not None:
                raise GitConflict("workspace branch already exists; do not overwrite it")
            commit = state["base_oid"]
        source = github.sources(commit)
        return state, commit, source, Catalog(source)

    @staticmethod
    def target(catalog: Catalog, procedure: str, backend: str, source_id: str):
        if procedure not in catalog.procedures:
            raise GitError("procedure is not in the configured catalog")
        found = [section for section in catalog.compose(procedure, backend).sections
                 if section.id == source_id]
        if not found or len({section.source for section in found}) != 1:
            raise GitError("source identity is not a unique section of this procedure")
        return found[0]

    @staticmethod
    def overlay(source, path, raw):
        files = {name: source.read(name) for directory in
                 ("procedures/assemblies", "procedures/sections")
                 for name in source.paths(directory)}
        files[path] = raw
        return Catalog(MemorySource(files))

    @staticmethod
    def render(catalog, procedure):
        rendered = {selected: catalog.compose(procedure, selected)
                    for selected in ("markdown", "db")}
        errors = {}
        for selected, result in rendered.items():
            referenced = set(re.findall(r"§\s*([a-z][a-z0-9-]*)", result.text))
            defined = set(re.findall(r"(?m)^#{1,6}\s*§\s*([a-z][a-z0-9-]*)", result.text))
            errors[selected] = sorted(referenced - defined)
        return rendered, errors

    def preview(self, identity, procedure, backend, source_id=None, draft=None, revision="branch"):
        state, head, source, catalog = self.snapshot(identity, revision)
        if not catalog.procedures:
            raise GitError("this revision contains no procedures")
        procedure = procedure or catalog.procedures[0]
        original = None
        if source_id is not None:
            original = self.target(catalog, procedure, backend, source_id)
        if draft is not None:
            if original is None:
                raise GitError("preview needs a declared source identity")
            catalog = self.overlay(source, original.source, source_bytes(draft))
        rendered, errors = self.render(catalog, procedure)
        return {
            "workspace": state, "head": head, "catalog": catalog, "original": original,
            "procedure": procedure, "revision": revision,
            "rendered": rendered, "validation_errors": errors,
            "dependents": catalog.dependents(
                source_id, source_path=original.source
            ) if source_id else (),
        }

    def _check_pull(self, state, github):
        if state["pull_number"] is None:
            return
        pull = github.pull(state["pull_number"])
        if (pull.get("state") != "open" or not pull.get("draft")
                or pull.get("head", {}).get("ref") != state["branch"]
                or pull.get("base", {}).get("ref") != state["repository"]["base_ref"]):
            raise GitConflict(
                "workspace PR is no longer the original open draft; start a new workspace"
            )

    def save(self, identity, procedure, backend, source_id, draft, expected_head,
             expected_source_hash):
        state, github = self.bound(identity)
        if state["pending_action"]:
            raise GitUncertain("a prior save needs reconciliation before another write")
        if state["pending_pr"]:
            raise GitError(
                "source is already saved to the branch; retry its PR before another save"
            )
        self._check_pull(state, github)
        version = state["version"]
        state, head, source, catalog = self.snapshot(identity)
        if state["version"] != version:
            raise GitConflict("workspace changed while loading; reload before saving")
        target = self.target(catalog, procedure, backend, source_id)
        if head != expected_head or target.source_hash != expected_source_hash:
            raise GitConflict("branch or source changed; draft retained, reload before saving")
        raw = source_bytes(draft)
        if raw == source.read(target.source):
            raise GitError("no source changes to save")
        rendered, validation_errors = self.render(
            self.overlay(source, target.source, raw), procedure
        )
        pending = {"id": uuid.uuid4().hex, "expected_head": head,
                   "path": target.source, "new_hash": digest(raw)}
        state = self.store.update(
            state, pending_save=pending,
            pending_action="commit" if state["branch_created"] else "create_branch",
        )
        if not state["branch_created"]:
            try:
                github.create_branch(state["branch"], head)
            except GitUncertain:
                raise
            except GitError:
                self.store.update(state, pending_action=None, pending_save=None)
                raise
            state = self.store.update(state, branch_created=True, pending_action="commit")
        try:
            commit = github.commit(
                state["branch"], head, github.repository.path(target.source), raw, pending["id"]
            )
        except GitUncertain:
            raise
        except GitError:
            self.store.update(state, pending_action=None, pending_save=None)
            raise
        state = self.store.update(
            state, head_oid=commit, pending_action=None, pending_save=None, pending_pr=True
        )
        result = self.retry_pull(identity)
        result.update(
            source_hash=digest(raw), head=commit, validation_errors=validation_errors,
            sections={backend: [asdict(section) for section in result.sections]
                      for backend, result in rendered.items()},
        )
        return result

    def retry_pull(self, identity):
        state, github = self.bound(identity)
        if state["pending_action"]:
            raise GitUncertain("workspace has an unfinished request; reconcile first")
        if not state["branch_created"] or not state["pending_pr"]:
            raise GitError("there is no saved commit waiting for a PR")
        self._check_pull(state, github)
        state = self.store.update(state, pending_action="pull")
        try:
            pull = github.ensure_pull(state["branch"], identity)
            if (not isinstance(pull, dict) or type(pull.get("number")) is not int
                    or pull["number"] < 1 or not isinstance(pull.get("html_url"), str)
                    or pull.get("state") != "open" or pull.get("draft") is not True
                    or pull.get("head", {}).get("ref") != state["branch"]
                    or pull.get("base", {}).get("ref") != state["repository"]["base_ref"]):
                raise GitUncertain(
                    "saved commit has no confirmed open draft PR; inspect or retry PR"
                )
        except GitError as exc:
            state = self.store.update(state, pending_action=None)
            return {"status": "saved_pr_pending", "workspace": state, "error": str(exc),
                    "pr_uncertain": isinstance(exc, GitUncertain)}
        state = self.store.update(
            state, pending_action=None, pending_pr=False,
            pull_number=pull["number"], pull_url=pull["html_url"],
        )
        return {"status": "saved", "workspace": state, "error": None}

    def reconcile(self, identity):
        state, github = self.bound(identity)
        action = state["pending_action"]
        if action is None:
            return state
        if action == "pull":
            # Retry always queries by the already-owned branch before creating a PR.
            return self.store.update(state, pending_action=None, pending_pr=True)
        head = github.head(state["branch"])
        if action == "create_branch":
            if head is not None:
                raise GitUncertain("branch exists after an uncertain creation; inspect it or start "
                                   "a new workspace, never adopt or reset it automatically")
            return self.store.update(state, pending_action=None, pending_save=None)
        pending = state["pending_save"]
        if head == pending["expected_head"]:
            # No commit observed. A retry still uses an atomic expected-HEAD condition,
            # so a late prior request cannot create a second successful commit.
            return self.store.update(state, pending_action=None, pending_save=None)
        if head is None:
            raise GitConflict("workspace branch disappeared during an uncertain save")
        commit = github.commit_info(head)
        parents = [row["sha"] for row in commit.get("parents", [])]
        if (pending["id"] not in commit.get("message", "")
                or parents != [pending["expected_head"]]):
            raise GitUncertain(
                "branch changed after an uncertain save; inspect its history before retrying"
            )
        source = github.sources(head)
        if digest(source.read(pending["path"])) != pending["new_hash"]:
            raise GitUncertain(
                "observed commit does not match the pending source; inspect before retrying"
            )
        return self.store.update(
            state, head_oid=head, branch_created=True, pending_action=None,
            pending_save=None, pending_pr=True,
        )
