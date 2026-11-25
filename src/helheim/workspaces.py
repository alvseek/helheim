"""Remote procedure workspaces; local persistence holds references, never source drafts.

Each edit belongs to one Markdown document. Backend section edits replace a named
region in the original source. Preview and Save both use the resulting complete blob.
"""

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
from procedure_composer.document import _events

from .github import GitConflict, GitError, GitHub, GitUncertain, Repository

DOCUMENT_FORMAT = 2
BACKENDS = ("markdown", "db")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def source_bytes(text: str) -> bytes:
    try:
        return text.encode("utf-8")
    except (AttributeError, UnicodeError) as exc:
        raise GitError("source must be valid UTF-8 text") from exc


def document_row(edit) -> dict:
    """A JSON-safe view of one owning document: never its bytes."""
    return {"id": edit.id, "kind": edit.kind, "path": edit.path, "sha256": edit.sha256}


def region_source(catalog, procedure, backend, document, region, draft=None):
    """Splice only the selected region, using the pinned composer's fence-aware lexer."""
    if document.kind != "backend" or not any(
        span.path == document.path and span.target == region
        for span in catalog.compose(procedure, backend).spans
    ):
        raise GitError("region does not belong to this procedure and document")
    text = document.raw.decode("utf-8")
    lines = text.splitlines(keepends=True)
    start = None
    for event in _events(text, document.path):
        if event[0] != "directive":
            continue
        _, action, _, attrs, line = event
        if action == "start" and attrs.get("id") == region:
            start = line
        elif action == "end" and start is not None:
            interior = "".join(lines[start:line - 1])
            # Keep the structural newline before @end outside the textarea.
            ending = re.search(r"(\r\n|\r|\n)\Z", interior)
            original = interior[:ending.start()] if ending else interior
            if draft is None or draft == original:
                return original, document.raw
            if any(e[0] == "directive" for e in _events(draft, document.path)):
                raise GitError("a section edit cannot change region markers")
            marker = lines[start - 1]
            separator = ending[0] if ending else marker[len(marker.rstrip("\r\n")):]
            if draft.endswith("\r") and separator == "\n":
                separator = "\r\n"
            raw = "".join(lines[:start]) + draft + separator + "".join(lines[line - 1:])
            return original, source_bytes(raw)
    raise GitError("source region not found")


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
            "id": identity, "version": 0, "document_format": DOCUMENT_FORMAT,
            "repository": asdict(repository),
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
        if state.get("document_format") != DOCUMENT_FORMAT:
            raise GitError(
                "this workspace predates whole-document procedure editing; start a new "
                "workspace (its local state is kept, never converted)"
            )
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
    def document(catalog: Catalog, procedure: str, backend: str, document_id: str,
                 *, allow_missing: bool = False):
        """The complete owning document behind one rendered span or selector."""
        if procedure not in catalog.procedures:
            raise GitError("procedure is not in the configured catalog")
        matches = [edit for edit in catalog.compose(procedure, backend).documents
                   if edit.id == document_id]
        if not matches and allow_missing:
            return None
        if len(matches) != 1:
            raise GitError(
                "document identity is not part of this procedure for this backend"
            )
        return matches[0]

    @staticmethod
    def overlay(source, path, raw):
        if not path.startswith("procedures/"):
            raise GitError("write target is outside the procedure document tree")
        files = {name: source.read(name) for name in source.paths("procedures")}
        files[path] = raw
        return Catalog(MemorySource(files))

    @classmethod
    def checked_overlay(cls, source, target, raw):
        """Replace one whole file, refuse an identity/kind change, validate everything."""
        catalog = cls.overlay(source, target.path, raw)
        if target.kind == "backend":
            catalog.backend_document(target.path)
        else:
            document = catalog.documents.get(target.id)
            if (document is None or document.path != target.path
                    or document.metadata.kind != target.kind):
                raise GitError(
                    "the draft no longer declares the same document identity and kind; "
                    "save refused"
                )
        # Every document and both backends, not only the clicked region.
        catalog.validate()
        return catalog

    @staticmethod
    def applicability(catalog: Catalog, path: str):
        delivered = []
        for backend in BACKENDS:
            if any(any(edit.path == path for edit in catalog.compose(name, backend).documents)
                   for name in catalog.procedures):
                delivered.append(backend)
        return tuple(delivered)

    @staticmethod
    def render(catalog, procedure):
        rendered = {selected: catalog.compose(procedure, selected)
                    for selected in BACKENDS}
        errors = {}
        for selected, result in rendered.items():
            referenced = set(re.findall(r"§\s*([a-z][a-z0-9-]*)", result.text))
            defined = set(re.findall(r"(?m)^#{1,6}\s*§\s*([a-z][a-z0-9-]*)", result.text))
            errors[selected] = sorted(referenced - defined)
        return rendered, errors

    def preview(self, identity, procedure, backend, document_id=None, draft=None,
                revision="branch", *, allow_missing_document=False, expected_document_path=None,
                region=None):
        state, head, source, catalog = self.snapshot(identity, revision)
        if not catalog.procedures:
            raise GitError("this revision contains no procedures")
        procedure = procedure or catalog.procedures[0]
        if procedure not in catalog.procedures:
            raise GitError("procedure is not in the configured catalog")
        original = None
        rendered, errors = self.render(catalog, procedure)
        choices = {}
        for selected in dict.fromkeys((backend, *BACKENDS)):
            for edit in rendered[selected].documents:
                choices.setdefault(edit.path, (selected, edit))
        if allow_missing_document and document_id in choices:
            backend, edit = choices[document_id]
            document_id = edit.id
        if document_id is not None:
            original = self.document(
                catalog, procedure, backend, document_id,
                allow_missing=allow_missing_document and draft is None,
            )
            # A backend file and an authored document can share an ID in different views.
            if (original and expected_document_path is not None
                    and original.path != expected_document_path):
                original = None
        original_text = original.raw.decode("utf-8") if original else None
        if region is not None:
            if original is None:
                raise GitError("region needs a declared document")
            original_text, raw = region_source(catalog, procedure, backend, original, region, draft)
        if draft is not None:
            if original is None:
                raise GitError("preview needs a declared document identity")
            catalog = self.checked_overlay(source, original, raw if region else source_bytes(draft))
            rendered, errors = self.render(catalog, procedure)
        return {
            "workspace": state, "head": head, "catalog": catalog, "original": original,
            "procedure": procedure, "revision": revision,
            "rendered": rendered, "validation_errors": errors,
            "dependents": catalog.dependents(original.path) if original else (),
            "documents": {selected: [document_row(edit) for edit in result.documents]
                          for selected, result in rendered.items()},
            "applicability": self.applicability(catalog, original.path) if original else (),
            "original_text": original_text, "backend": backend,
            "choices": [edit for _, edit in choices.values()],
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

    def save(self, identity, procedure, backend, document_id, draft, expected_head,
             expected_document_hash, region=None):
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
        target = self.document(catalog, procedure, backend, document_id)
        if head != expected_head or target.sha256 != expected_document_hash:
            raise GitConflict(
                "branch or document changed; draft retained, reload before saving"
            )
        raw = (region_source(catalog, procedure, backend, target, region, draft)[1]
               if region is not None else source_bytes(draft))
        if raw == source.read(target.path):
            raise GitError("no document changes to save")
        overlay = self.checked_overlay(source, target, raw)
        rendered, validation_errors = self.render(overlay, procedure)
        pending = {"id": uuid.uuid4().hex, "expected_head": head,
                   "path": target.path, "document_id": target.id, "new_hash": digest(raw)}
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
                state["branch"], head, github.repository.path(target.path), raw, pending["id"]
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
        saved = digest(raw)
        result.update(
            head=commit, document={"id": target.id, "kind": target.kind,
                                   "path": target.path, "sha256": saved},
            validation_errors=validation_errors,
            dependents=list(overlay.dependents(target.path)),
            applicability=self.applicability(overlay, target.path),
            rendered={selected: rendered[selected].text for selected in BACKENDS},
            spans={selected: [asdict(span) for span in rendered[selected].spans]
                   for selected in BACKENDS},
            documents={selected: [document_row(edit)
                                  for edit in rendered[selected].documents]
                       for selected in BACKENDS},
        )
        return result

    def retry_pull(self, identity):
        state, github = self.bound(identity)
        if state["pending_action"]:
            raise GitUncertain("workspace has an unfinished request; reconcile first")
        if not state["branch_created"] or not state["pending_pr"]:
            raise GitError("there is no saved commit waiting for a PR")
        state = self.store.update(state, pending_action="pull")
        try:
            # The source commit is already confirmed. A failed PR check must
            # preserve that success without creating or reusing a refused PR.
            self._check_pull(state, github)
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
                "observed commit does not match the pending document; inspect before retrying"
            )
        return self.store.update(
            state, head_oid=head, branch_created=True, pending_action=None,
            pending_save=None, pending_pr=True,
        )
