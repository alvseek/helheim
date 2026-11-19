"""The bench must reject unowned/user state before it can reset anything."""
import importlib.util
import io
import socket

import pytest


@pytest.fixture
def bench(pytestconfig):
    spec = importlib.util.spec_from_file_location(
        "helheim_qa_bench", pytestconfig.rootpath / "qa/bench.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_bytes(b'[project]\nname="helheim"\n')
    return root


def test_qa_state_cannot_target_normal_user_or_arbitrary_paths(bench, tmp_path):
    target = project(tmp_path)
    normal = tmp_path / "user-profile"
    normal.mkdir()
    sentinel = normal / "tokens.json"
    sentinel.write_bytes(b"do not touch")
    with pytest.raises(ValueError, match="QA state must"):
        bench.reset(target, normal)
    assert sentinel.read_bytes() == b"do not touch"


def test_qa_reset_refuses_an_unowned_existing_directory(bench, tmp_path):
    target = project(tmp_path)
    state = target / ".qa-state"
    state.mkdir()
    sentinel = state / "keep.txt"
    sentinel.write_bytes(b"unowned")
    with pytest.raises(FileNotFoundError):
        bench.reset(target, state)
    assert sentinel.read_bytes() == b"unowned"


def test_qa_marker_must_match_exact_root_and_project(bench, tmp_path):
    target = project(tmp_path)
    state = target / ".qa-state"
    marker = bench.reset(target, state)
    marker["root"] = str(tmp_path)
    bench.write(state / "owner.json", marker)
    with pytest.raises(ValueError, match="ownership marker"):
        bench.reset(target, state)


def test_qa_inject_is_empty_idempotent_and_reset_scoped(bench, tmp_path):
    target = project(tmp_path)
    state = target / ".qa-state"
    first = bench.reset(target, state)
    bench.inject(state)
    bench.inject(state)
    assert bench.read(state / "runtime/config.json") == {
        "kind": bench.KIND, "servers": [], "repositories": [],
    }
    defaults = bench.tomllib.loads((state / "runtime/home/config.toml").read_text())
    assert all(row["url"] == "http://127.0.0.1:9/mcp"
               for row in defaults["servers"].values())
    sentinel = target / "user-state.txt"
    sentinel.write_bytes(b"preserve")
    second = bench.reset(target, state)
    assert second["run"] != first["run"]
    assert sentinel.read_bytes() == b"preserve"


def test_qa_runtime_cannot_follow_a_directory_link(bench, tmp_path):
    target = project(tmp_path)
    state = target / ".qa-state"
    bench.reset(target, state)
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep").write_bytes(b"preserve")
    try:
        (state / "runtime/link").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("This host does not permit creating a test directory symlink")
    with pytest.raises(ValueError, match="reparse"):
        bench.reset(target, state)
    assert (external / "keep").read_bytes() == b"preserve"


def test_qa_observe_rejects_external_endpoints_before_network(bench, tmp_path, monkeypatch):
    target = project(tmp_path)
    state = target / ".qa-state"
    owner = bench.reset(target, state)
    bench.write(state / "runtime/service.json", {"url": "https://example.invalid"})
    monkeypatch.setattr(bench, "request", lambda *_: pytest.fail("Unsafe network request"))
    with pytest.raises(ValueError, match="loopback"):
        bench.observe(state, owner)


@pytest.mark.parametrize("timeout_case", ["none", "transient", "deadline"])
def test_qa_stop_checks_tcp_closure_not_a_disappearing_http_response(
    bench, tmp_path, monkeypatch, timeout_case,
):
    target = project(tmp_path)
    state = target / ".qa-state"
    owner = bench.reset(target, state)
    service = {"url": "http://127.0.0.1:12345"}
    bench.write(state / "runtime/service.json", service)
    monkeypatch.setattr(bench, "observe", lambda *_: service)

    class Opener:
        def open(self, message, timeout):
            if isinstance(message, str):
                return io.BytesIO(b'<meta name="csrf-token" content="test">')
            return io.BytesIO(b'{"stopping":true}')

    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Opener())
    attempts = []
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
    def connect(*args, **kwargs):
        attempts.append(args)
        assert 0 < kwargs["timeout"] <= 3
        if timeout_case != "deadline":
            assert kwargs["timeout"] == 3
        assert (state / "runtime/service.json").exists()
        if timeout_case == "deadline" or (timeout_case == "transient" and len(attempts) == 2):
            raise TimeoutError("listener closure not yet confirmed")
        if len(attempts) == 1:
            return Connection()
        raise ConnectionRefusedError
    monkeypatch.setattr(socket, "create_connection", connect)
    ticks = iter([0, 9, 11] if timeout_case == "deadline" else [0, 1, 2, 3, 4, 5])
    monkeypatch.setattr(bench.time, "monotonic", lambda: next(ticks))
    if timeout_case == "deadline":
        with pytest.raises(RuntimeError, match="did not stop"):
            bench.stop(state, owner)
        assert (state / "runtime/service.json").exists()
        return
    bench.stop(state, owner)
    assert len(attempts) == (3 if timeout_case == "transient" else 2)
    assert not (state / "runtime/service.json").exists()


def test_qa_start_retries_transient_service_marker_access(bench, tmp_path, monkeypatch):
    target = project(tmp_path)
    state = target / ".qa-state"
    owner = bench.reset(target, state)
    calls = []

    def observe(*_):
        calls.append(True)
        if len(calls) == 1:
            raise PermissionError("Windows marker publication in progress")
        return {"url": "http://127.0.0.1:12345"}

    monkeypatch.setattr(bench, "observe", observe)
    monkeypatch.setattr(bench.subprocess, "Popen", lambda *_, **__: None)
    assert bench.act(target, state, owner) == {"url": "http://127.0.0.1:12345"}
    assert len(calls) == 2
