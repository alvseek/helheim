"""Safety regressions for owned QA process lifecycle; no product or real remote is touched."""
import tempfile
import uuid
from pathlib import Path
from urllib.error import URLError

import pytest

from qa import bench
from qa.fixtures import git_remote, processes

PROJECT = Path(__file__).resolve().parents[1]


def _root(prefix):
    return Path(tempfile.gettempdir()) / "opencode" / (prefix + uuid.uuid4().hex)


def test_peer_close_tolerates_never_started_peer():
    peer = processes.Peer(PROJECT, "paged_remote")
    root = peer.root
    assert peer.process is None
    peer.close()
    assert not root.exists()
    peer.close()


def test_peer_start_failure_stops_owned_child_and_removes_root(monkeypatch):
    ticks = iter([0, 100])
    monkeypatch.setattr(processes.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(
        processes.Peer, "request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("not ready")),
    )
    peer = processes.Peer(PROJECT, "paged_remote")
    root = peer.root
    with pytest.raises(RuntimeError, match="did not become ready"):
        peer.start()
    assert peer.process is not None and peer.process.poll() is not None
    assert not root.exists()
    peer.close()


def test_peer_partial_constructor_failure_does_not_leave_owned_root(monkeypatch):
    known = uuid.uuid4()

    class Known:
        hex = known.hex

    monkeypatch.setattr(processes.uuid, "uuid4", lambda: Known())
    root = Path(tempfile.gettempdir()) / "opencode" / ("helheim-qa-" + known.hex)

    def fail(_root):
        raise RuntimeError("inject failed")

    monkeypatch.setattr(processes.bench, "inject", fail)
    with pytest.raises(RuntimeError, match="inject failed"):
        processes.Peer(PROJECT, "paged_remote")
    assert not root.exists()


def test_git_peer_partial_constructor_failure_does_not_leave_owned_root(monkeypatch):
    known = uuid.uuid4()

    class Known:
        hex = known.hex

    monkeypatch.setattr(git_remote.uuid, "uuid4", lambda: Known())
    root = Path(tempfile.gettempdir()) / "opencode" / ("helheim-qa-git-" + known.hex)

    def fail(*_args, **_kwargs):
        raise RuntimeError("copy failed")

    monkeypatch.setattr(git_remote.shutil, "copy2", fail)
    with pytest.raises(RuntimeError, match="copy failed"):
        git_remote.GitHubPeer(PROJECT)
    assert not root.exists()


class FakeProcess:
    def __init__(self):
        self.pid = 424242
        self.terminated = False
        self.killed = False
        self.waited = []

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.terminated = True

    def wait(self, timeout=None):
        self.waited.append(timeout)
        self.terminated = True
        return 0


def test_bench_act_boot_failure_retains_and_stops_the_handle(monkeypatch, tmp_path):
    target = tmp_path / "project"
    target.mkdir()
    state = target / ".qa-state"
    owner = bench.reset(target, state)
    process = FakeProcess()
    killed = []
    ticks = iter([0, 100])
    monkeypatch.setattr(bench.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(bench, "observe", lambda *_args: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(bench.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(bench.subprocess, "run", lambda args, **_kwargs: killed.append(args))
    with pytest.raises(RuntimeError, match="QA boot failed"):
        bench.act(target, state, owner)
    assert process.poll() is not None
    assert process.waited
    assert process.terminated or process.killed or killed
    if bench.os.name == "nt":
        assert any(call and call[0] == "taskkill" and str(process.pid) in call
                   for call in killed)
    assert bench.owned(state, target)["run"] == owner["run"]
