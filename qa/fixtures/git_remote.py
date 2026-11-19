# fixture: a synthetic GitHub remote behind the real gh subprocess boundary
# produces: an owned gh executable plus retained branch/commit/PR state for the actual adapter
# fidelity: rung 3 hand-rolled seed; no real gh, token, network or GitHub repository is touched
# fidelity-checked: 2026-11-19, GitHub.request drove this process for every API call
# teardown: nonce-owned temp root is removed; no process or remote state remains
from __future__ import annotations

import base64
import hashlib
import os
import shlex
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

from qa import bench

BASE = "a" * 40
TREE = "1" * 40
FILES = {
    "procedures/assemblies/flow.toml": (
        b'format_version=1\nid="flow"\ntitle="Flow"\ndescription="Do it."\n'
        b'[[sections]]\nid="first"\nsource="procedures/sections/first.md"\n'
        b'[[sections]]\nid="second"\nsource="procedures/sections/second.md"\n'
    ),
    "procedures/sections/first.md": b"alpha\n",
    "procedures/sections/second.md": b"beta\n",
}


def initial_state():
    blobs = {
        hashlib.sha256(raw).hexdigest(): base64.b64encode(raw).decode("ascii")
        for raw in FILES.values()
    }
    tree = {path: hashlib.sha256(raw).hexdigest() for path, raw in FILES.items()}
    return {
        "base": BASE,
        "base_ref": "main",
        "owner": "owner",
        "repo": "repo",
        "branches": {},
        "commits": {BASE: {"tree": TREE, "parents": [], "message": "base snapshot"}},
        "trees": {TREE: tree},
        "blobs": blobs,
        "pulls": {},
        "next_oid": 1,
        "next_pull": 1,
        "calls": [],
        "faults": {},
        "counters": {"branch": 0, "commit": 0, "pull": 0, "pull_read": 0, "pull_list": 0},
    }


class GitHubPeer:
    """Own one executable shim and its state; activate() confines the adapter to it."""

    def __init__(self, project):
        self.project = Path(project).resolve()
        self.root = Path(tempfile.gettempdir()) / "opencode" / (
            "helheim-qa-git-" + uuid.uuid4().hex
        )
        self.owner = None
        try:
            self.owner = bench.reset(self.project, self.root)
            self.shim = self.root / "gh-bin"
            self.shim.mkdir()
            self.state_path = self.root / "gh-state.json"
            self.gh_path = self.shim / ("gh.exe" if os.name == "nt" else "gh")
            shutil.copy2(Path(__file__).with_name("gh_shim.py"), self.shim / "api")
            if os.name == "nt":
                shutil.copy2(sys.executable, self.gh_path)
                config = Path(sys.executable).parent.parent / "pyvenv.cfg"
                if config.exists():
                    shutil.copy2(config, self.shim / "pyvenv.cfg")
            else:
                script = (
                    "#!/bin/sh\n"
                    "shift\n"
                    f"exec {shlex.quote(sys.executable)} "
                    f"{shlex.quote(str(self.shim / 'api'))} \"$@\"\n"
                )
                self.gh_path.write_text(script, encoding="utf-8")
                os.chmod(self.gh_path, 0o755)
                os.chmod(self.shim / "api", 0o755)
            bench.write(self.state_path, initial_state())
        except BaseException:
            try:
                self.close()
            except BaseException:
                pass
            raise

    def activate(self, monkeypatch):
        # Only the owned shim directory is on PATH. It executes its owned ``api``
        # script (Windows resolves that name from the working directory), so a
        # missing shim can never fall through to a real gh installation or its
        # credentials.
        monkeypatch.chdir(self.shim)
        monkeypatch.setenv("PATH", str(self.shim))
        monkeypatch.setenv("HELHEIM_QA_GH_STATE", str(self.state_path))
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR", "GH_HOST",
                     "PYTHONHOME", "PYTHONPATH"):
            monkeypatch.delenv(name, raising=False)
        found = shutil.which("gh")
        assert found is not None and Path(found).resolve() == self.gh_path.resolve()

    def state(self):
        return bench.read(self.state_path)

    def update(self, change):
        state = self.state()
        change(state)
        bench.write(self.state_path, state)
        return state

    def faults(self, **changes):
        def change(state):
            for key, value in changes.items():
                if value is None:
                    state["faults"].pop(key, None)
                else:
                    state["faults"][key] = value
        self.update(change)

    def mutate_pr(self, **changes):
        def change(state):
            state["pulls"]["1"].update(changes)
        self.update(change)

    def put_pull(self, branch, **changes):
        def change(state):
            state["pulls"]["1"] = {
                "number": 1,
                "html_url": f"https://github.com/{state['owner']}/{state['repo']}/pull/1",
                "state": "open", "draft": True,
                "head": {"ref": branch}, "base": {"ref": state["base_ref"]},
                **changes,
            }
        self.update(change)

    def mutate_commit(self, oid, **changes):
        def change(state):
            state["commits"][oid].update(changes)
        self.update(change)

    def calls(self):
        return self.state()["calls"]

    def counters(self):
        return self.state()["counters"]

    def close(self):
        os.chdir(self.project)
        if not self.root.exists():
            return
        assert bench.owned(self.root, self.project)["run"] == self.owner["run"]
        shutil.rmtree(self.root)
