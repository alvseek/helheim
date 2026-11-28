"""Exercise externally supplied, pinned Hermod source trees without remote writes."""

import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_editor_routes import initial, source_request
from test_workspaces import BASE, FakeGitHub

from helheim.app import create_app
from helheim.config import Settings
from helheim.github import Repository
from helheim.workspaces import Workspaces, WorkspaceStore


class RepositoryGitHub(FakeGitHub):
    def ensure_pull(self, branch, workspace_id):
        pull = super().ensure_pull(branch, workspace_id)
        pull["html_url"] = (
            f"https://github.com/{self.repository.owner}/{self.repository.name}/pull/1"
        )
        return pull


@pytest.mark.parametrize(("variable", "key", "procedure", "document", "region"), [
    ("HELHEIM_TEST_PROJECT_SOURCE", "project", "awaken-coder", "db", "awaken-coder"),
    ("HELHEIM_TEST_WIZARD_SOURCE", "wizard", "high-wizard", "planning-rounds", None),
])
def test_real_hermod_source_editor(tmp_path, variable, key, procedure, document, region):
    root = os.environ.get(variable)
    if not root:
        pytest.skip(f"{variable} not supplied")
    root = Path(root)
    files = {p.relative_to(root).as_posix(): p.read_bytes()
             for p in (root / "procedures").rglob("*") if p.is_file()}
    repository = Repository(key, "fixture-owner", key)
    github = RepositoryGitHub(repository, files)
    settings = Settings(home=tmp_path, procedure_repositories=(repository,))
    app = create_app(settings)
    app.state.workspaces = Workspaces(WorkspaceStore(settings.db_path), (repository,),
                                      adapter=lambda _: github)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        page = client.get("/procedures")
        token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
        headers = {"Origin": "http://127.0.0.1:8765", "X-CSRF-Token": token}
        created = client.post("/procedures/workspaces", json={"repository": key}, headers=headers)
        assert created.status_code == 200, created.text
        path = created.json()["redirect"]
        for backend in ("db", "markdown"):
            view = client.get(path, params={"procedure": procedure, "backend": backend})
            assert view.status_code == 200, view.text
            assert f"Composed instructions · {backend}" in view.text
        params = {"procedure": procedure, "backend": "db", "document": document}
        if region:
            params["region"] = region
        state = initial(client.get(path, params=params))
        draft = state["original"] + "\nQA-only edit.\n"
        payload = source_request(state, draft)
        preview = client.post(path + "/preview", json=payload, headers=headers)
        assert preview.status_code == 200, preview.text
        assert "QA-only edit." in preview.json()["rendered"]["db"]
        saved = client.post(path + "/save", json=payload, headers=headers)
        assert saved.status_code == 200, saved.text
        changed = saved.json()["document"]["path"]
        result = github.commits[saved.json()["head"]]
        assert {name for name in files if files[name] != result[name]} == {changed}
        assert b"QA-only edit." in result[changed]
        assert github.pr["draft"] is True
        assert saved.json()["workspace"]["pull_url"] == (
            f"https://github.com/{repository.owner}/{repository.name}/pull/1"
        )
        assert github.calls.count("commit") == 1
        assert BASE in github.commits
