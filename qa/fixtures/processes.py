"""Own one real HTTP dependency per integration fixture; no user config or external endpoints."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from qa import bench


class Peer:
    def __init__(self, project, role):
        self.project = Path(project).resolve()
        self.root = Path(tempfile.gettempdir()) / "opencode" / ("helheim-qa-" + uuid.uuid4().hex)
        self.role = role
        self.process = None
        self.url = None
        self.owner = None
        try:
            self.owner = bench.reset(self.project, self.root)
            bench.inject(self.root)
        except BaseException:
            try:
                self.close()
            except BaseException:
                pass
            raise

    def start(self):
        try:
            log = (self.root / "runtime/peer.log").open("ab")
            env = dict(os.environ, HELHEIM_HOME=str(self.root / "runtime/home"))
            try:
                self.process = subprocess.Popen([
                    sys.executable, "-m", "qa.fixtures." + self.role,
                    "--project", str(self.project), "--state", str(self.root),
                ], cwd=self.project, env=env, stdin=subprocess.DEVNULL, stdout=log,
                    stderr=subprocess.STDOUT)
            finally:
                log.close()
            kind = "memory" if self.role == "paged_remote" else "git"
            location = self.root / "runtime" / (kind + "-peer.json")
            deadline = time.monotonic() + 20
            while True:
                if self.process.poll() is not None:
                    raise RuntimeError("QA peer failed; inspect "
                                       + str(self.root / "runtime/peer.log"))
                try:
                    self.url = bench.read(location)["url"]
                    actual = self.request("/__qa__/health", authenticated=False)
                    assert actual == {"kind": "helheim-qa-" + kind, "run": self.owner["run"],
                                      "nonce_hash": hashlib.sha256(
                                          self.owner["nonce"].encode()).hexdigest()}
                    return self
                except (FileNotFoundError, URLError):
                    if time.monotonic() > deadline:
                        raise RuntimeError("QA peer did not become ready") from None
                    threading.Event().wait(0.05)
        except BaseException:
            try:
                self.close()
            except BaseException:
                pass
            raise

    def request(self, path, data=None, *, authenticated=True, timeout=20):
        if not self.url or not self.url.startswith("http://127.0.0.1:"):
            raise ValueError("Missing owned loopback peer")
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer " + self.owner["nonce"]
        message = Request(self.url + path, data=None if data is None else json.dumps(data).encode(),
                          headers=headers)
        with urlopen(message, timeout=timeout) as response:
            return json.load(response)

    def close(self):
        process = self.process
        if process is not None and process.poll() is None:
            stopped = False
            if self.url:
                try:
                    self.request("/__qa__/stop", {})
                    process.wait(timeout=10)
                    stopped = process.poll() is not None
                except Exception:
                    pass
            if not stopped and process.poll() is None:
                bench.stop_process(process)
        if process is not None:
            assert process.poll() is not None
        if not self.root.exists():
            return
        assert bench.owned(self.root, self.project)["run"] == self.owner["run"]
        shutil.rmtree(self.root)  # Exact generated, marker-verified disposable root.
