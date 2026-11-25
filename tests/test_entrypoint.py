"""Entrypoint import and launcher must use only test-owned state."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import httpx


def isolated_env(tmp_path):
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("HELHEIM_"):
            env.pop(key)
    env["HELHEIM_HOME"] = str(tmp_path / "configured-home")
    env["HOME"] = str(tmp_path / "default-home")
    env["USERPROFILE"] = str(tmp_path / "default-home")
    return env


def test_import_app_does_not_open_or_create_desktop_store(tmp_path):
    env = isolated_env(tmp_path)
    result = subprocess.run(
        [sys.executable, "-c", "import helheim.app"],
        env=env, capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "configured-home").exists()
    assert not (tmp_path / "default-home" / ".config" / "helheim" / "helheim.db").exists()


def test_desktop_entrypoint_serves_with_the_factory(tmp_path):
    env = isolated_env(tmp_path)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    home = tmp_path / "configured-home"
    home.mkdir()
    (home / "config.toml").write_text(
        f'[app]\nhost = "127.0.0.1"\nport = {port}\n', encoding="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "helheim"], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        with httpx.Client(trust_env=False) as client:
            while True:
                assert process.poll() is None
                try:
                    response = client.get(f"http://127.0.0.1:{port}/health", timeout=1)
                    break
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert (home / "helheim.db").is_file()
    finally:
        process.terminate()
        process.wait(timeout=10)
