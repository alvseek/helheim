"""Owned-state QA loop. No normal config, OAuth cache, or external server is loaded."""
from __future__ import annotations

import argparse
import hashlib
import html
import http.client
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from fastapi import Request as WebRequest

KIND = "helheim-owned-qa"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    data = (json.dumps(value, indent=2) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def reparse(path):
    if not path.exists() and not path.is_symlink():
        return False
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)


def state_path(project, supplied=None):
    project = Path(project).resolve()
    raw = Path(supplied).absolute() if supplied else project / ".qa-state"
    if reparse(raw):
        raise ValueError("QA state must not be a symlink or reparse point")
    resolved = raw.resolve()
    default = project / ".qa-state"
    approved_temp = Path(tempfile.gettempdir()).resolve() / "opencode"
    if resolved != default and not (
        resolved.parent == approved_temp and resolved.name.startswith("helheim-qa-")
    ):
        raise ValueError("QA state must be project/.qa-state or temp/opencode/helheim-qa-*")
    return project, resolved


def owned(root, project):
    if reparse(root / "owner.json"):
        raise ValueError("QA ownership marker must not be a link")
    record = read(root / "owner.json")
    if (record.get("kind") != KIND or record.get("root") != str(root)
            or record.get("project") != str(project)):
        raise ValueError("QA ownership marker does not match this exact state/project")
    runtime = root / "runtime"
    if reparse(runtime) or any(reparse(path) for path in runtime.rglob("*")):
        raise ValueError("Refusing access across a runtime symlink/reparse point")
    return record


def request(url, data=None, headers=None):
    message = Request(url, data=None if data is None else json.dumps(data).encode(),
                      headers=headers or {})
    with urlopen(message, timeout=5) as response:
        raw = response.read()
        return json.loads(raw) if response.headers.get_content_type() == "application/json" else raw


def observe(root, owner):
    service = read(root / "runtime/service.json")
    base = service["url"]
    parsed = urlsplit(base)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise ValueError("QA service must be literal loopback HTTP")
    actual = request(base + "/__qa__/health")
    if actual != {"kind": KIND, "run": owner["run"],
                  "nonce_hash": hashlib.sha256(owner["nonce"].encode()).hexdigest()}:
        raise ValueError("The listening service is not this owned QA instance")
    assert request(base + "/health")["ok"] is True
    return service


def stop(root, owner):
    location = root / "runtime/service.json"
    if not location.exists():
        return
    try:
        service = observe(root, owner)
    except HTTPError as error:
        raise ValueError("The QA port answers as an unexpected service") from error
    except URLError:
        location.unlink(missing_ok=True)
        return
    import http.cookiejar
    import urllib.request
    client = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    base = service["url"]
    with client.open(base + "/procedures", timeout=5) as response:
        page = response.read().decode()
    csrf = html.unescape(re.search(r'name="csrf-token" content="([^"]+)"', page).group(1))
    message = Request(base + "/__qa__/stop", data=b"{}", headers={
        "Origin": base, "X-CSRF-Token": csrf, "Content-Type": "application/json",
        "X-Helheim-QA": owner["nonce"],
    })
    with client.open(message, timeout=5) as response:
        assert json.load(response) == {"stopping": True}
    deadline = time.monotonic() + 10
    target = urlsplit(base)
    while True:
        try:
            # Windows loopback refusal can take about two seconds to arrive.
            remaining = max(0.001, deadline - time.monotonic())
            with socket.create_connection((target.hostname, target.port or 80),
                                          timeout=min(3, remaining)):
                pass
        except ConnectionRefusedError:
            break
        except TimeoutError:
            # A timeout does not establish closure; retain ownership and keep
            # checking until refusal or the bounded shutdown deadline.
            pass
        if time.monotonic() > deadline:
            raise RuntimeError("Owned QA service did not stop")
        threading.Event().wait(0.05)
    location.unlink(missing_ok=True)


def reset(project, root):
    _, root = state_path(project, root)
    if root.exists():
        owner = owned(root, project)
        stop(root, owner)
        runtime = root / "runtime"
        if reparse(runtime) or any(reparse(path) for path in runtime.rglob("*")):
            raise ValueError("Refusing reset across a runtime symlink/reparse point")
        if runtime.exists():
            shutil.rmtree(runtime)  # Exact marker-guarded disposable child, never user state.
    root.mkdir(parents=True, exist_ok=True)
    owner = {"kind": KIND, "root": str(root), "project": str(project),
             "run": secrets.token_hex(16), "nonce": secrets.token_urlsafe(32)}
    write(root / "owner.json", owner)
    (root / "runtime/home").mkdir(parents=True, exist_ok=True)
    return owner


def inject(root):
    from helheim.config import DEFAULT_MAX_STORE_BYTES, DEFAULT_SERVERS
    from helheim.store import Store
    from helheim.workspaces import WorkspaceStore
    home = root / "runtime/home"
    # app.py constructs a module-level default app during import. Redirect that
    # import too, not just the explicitly served factory instance.
    config = ['[app]', 'host = "127.0.0.1"', 'port = 0', 'client_id = "qa-bench"']
    for name, _ in DEFAULT_SERVERS:
        config.extend([f'[servers.{json.dumps(name)}]', 'url = "http://127.0.0.1:9/mcp"',
                       'resource = "http://127.0.0.1:9/mcp"'])
    (home / "config.toml").write_bytes(("\n".join(config) + "\n").encode())
    history = Store(home / "helheim.db", DEFAULT_MAX_STORE_BYTES)
    store = WorkspaceStore(home / "helheim.db")
    if store.list() or history.list():
        raise ValueError("RESET before injecting over an existing QA specimen")
    write(root / "runtime/config.json", {"kind": KIND, "servers": [], "repositories": []})


def serve(project, root, owner):
    import hmac

    import uvicorn
    from fastapi import HTTPException

    os.environ["HELHEIM_HOME"] = str(root / "runtime/home")
    from helheim.app import create_app
    from helheim.config import Settings

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = read(root / "runtime/config.json")
    if config != {"kind": KIND, "servers": [], "repositories": []}:
        raise ValueError("Boundary fixture configuration is not installed yet")
    app = create_app(Settings(home=root / "runtime/home", host="127.0.0.1",
                              port=port, client_id="qa-bench", servers=(),
                              procedure_repositories=()))

    @app.get("/__qa__/health")
    def identity():
        return {"kind": KIND, "run": owner["run"],
                "nonce_hash": hashlib.sha256(owner["nonce"].encode()).hexdigest()}

    @app.post("/__qa__/stop")
    async def shutdown(request: WebRequest):
        if not hmac.compare_digest(request.headers.get("x-helheim-qa", ""), owner["nonce"]):
            raise HTTPException(403)
        await request.body()
        server.should_exit = True
        return {"stopping": True}

    service = {"url": f"http://127.0.0.1:{port}", "pid": os.getpid(), "run": owner["run"]}
    write(root / "runtime/service.json", service)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                          log_level="warning", access_log=False))
    server.run(sockets=[sock])


def stop_process(process, timeout=10):
    """Stop exactly this owned Popen handle and wait for its actual exit."""
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        # The venv launcher can own a separate interpreter child. Target only
        # this exact Popen PID and its process tree; never a guessed PID.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=timeout, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        process.wait(timeout=timeout)
    if process.poll() is None:
        raise RuntimeError("Owned QA process did not stop")


def act(project, root, owner):
    location = root / "runtime/service.json"
    if location.exists():
        try:
            return observe(root, owner)
        except HTTPError as error:
            raise ValueError("The QA port answers as an unexpected service") from error
        except URLError:
            location.unlink(missing_ok=True)
    log = (root / "runtime/app.log").open("ab")
    options = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
               if os.name == "nt" else {"start_new_session": True})
    command = [sys.executable, str(project / "qa/bench.py"), "serve",
               "--project", str(project), "--state", str(root)]
    process = None
    try:
        try:
            process = subprocess.Popen(command, cwd=project, stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT, **options)
        finally:
            log.close()
        deadline = time.monotonic() + 30
        while True:
            try:
                return observe(root, owner)
            except (FileNotFoundError, PermissionError, URLError, http.client.RemoteDisconnected,
                    ConnectionResetError) as error:
                if time.monotonic() > deadline:
                    raise RuntimeError("QA boot failed; inspect owned runtime/app.log") from error
                threading.Event().wait(0.05)
    except BaseException:
        # A boot timeout or cancellation must not leave a detached child that
        # later reset cannot see because no service marker was published.
        if process is not None:
            try:
                stop_process(process)
            except BaseException:
                pass
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("reset", "inject", "act", "observe", "stop", "serve"))
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--state", type=Path)
    args = parser.parse_args()
    project, root = state_path(args.project, args.state)
    data = tomllib.loads((project / "pyproject.toml").read_text())
    if data.get("project", {}).get("name") != "helheim":
        raise ValueError("Run this bench from the Helheim project")
    if args.phase == "reset":
        owner = reset(project, root)
    else:
        owner = owned(root, project)
        if args.phase == "inject":
            inject(root)
        elif args.phase == "act":
            print(json.dumps(act(project, root, owner)))
        elif args.phase == "observe":
            print(json.dumps(observe(root, owner)))
        elif args.phase == "stop":
            stop(root, owner)
        elif args.phase == "serve":
            serve(project, root, owner)
    if args.phase in ("reset", "inject", "stop"):
        print(json.dumps({"phase": args.phase, "state": str(root), "run": owner["run"]}))


if __name__ == "__main__":
    main()
