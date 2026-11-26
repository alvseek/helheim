"""Hosted inspector and guarded reads over the owned captured-page MCP peer."""

from __future__ import annotations

import hashlib
import html
import json
import re
import socket
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from test_hosted_login import ORIGIN, hosted_settings

from helheim.app import create_app
from helheim.auth import Token
from helheim.config import Server
from helheim.hosted_login import SESSION_COOKIE
from qa.fixtures.processes import Peer

PROJECT = Path(__file__).resolve().parents[1]
SEED = PROJECT / "qa/seeds/paged-read.json"


@pytest.fixture
def rig(tmp_path):
    peer = Peer(PROJECT, "paged_remote").start()
    root, process = peer.root, peer.process
    port = urlsplit(peer.url).port
    try:
        server = Server("paged", peer.url + "/mcp", peer.url + "/mcp")
        app = create_app(hosted_settings(tmp_path / "hosted", servers=(server,)))
        auth = app.state.auth
        auth.owner_gate.sessions["test-owner"] = time.monotonic() + 600
        # Test-only provisioned identity and resource token; no consent exchange.
        auth._store("paged", Token(peer.owner["nonce"], "", time.time() + 600, server.resource))
        with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
            client.cookies.set(SESSION_COOKIE, "test-owner", domain="helheim.example", path="/")
            yield client, auth, peer, server
        assert peer.request("/__qa__/state")["writes"] == 0
    finally:
        peer.close()
        assert process.poll() is not None
        assert not root.exists()
        with socket.socket() as probe:
            probe.settimeout(1)
            assert probe.connect_ex(("127.0.0.1", port)) != 0


def editor_init(response):
    match = re.search(r"data-editor-init='([^']*)'", response.text)
    assert match is not None
    return json.loads(html.unescape(match.group(1)))


def test_hosted_inspector_and_guarded_memory_reconstruct_captured_pages(rig):
    client, _, peer, _ = rig
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    uuid = seed["pages"][0]["uuid"]
    listing = client.get("/s/paged")
    assert listing.status_code == 200
    for name in ("list_memories", "get_memory", "edit_memory_body", "edit_memory_metadata"):
        assert f"/s/paged/t/{name}" in listing.text
    opened = client.get("/memory/paged/open", params={"uuid": uuid})
    assert opened.status_code == 200
    initial = editor_init(opened)
    assert initial["body"] == seed["body"]
    assert hashlib.sha256(initial["body"].encode()).hexdigest() == (
        seed["pages"][0]["edit_body"]["sha256"]
    )
    assert initial["context"] == seed["pages"][0]["edit_context"]
    assert initial["metadata"] == {
        key: seed["pages"][0][key] for key in ("title", "content_summary", "tags", "project")
    }
    state = peer.request("/__qa__/state")
    assert state["calls"] == [
        {"tool": "get_memory", "cursor": None},
        {"tool": "get_memory", "cursor": seed["pages"][0]["next_cursor"]},
    ]
    assert state["writes"] == 0


def test_hosted_guarded_open_refuses_a_mismatched_continuation(rig):
    client, auth, peer, _ = rig
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    peer.request("/__qa__/mode", {"mode": "revision"})
    response = client.get("/memory/paged/open", params={"uuid": seed["pages"][0]["uuid"]})
    assert response.status_code == 400
    assert "revision changed while loading" in response.text
    assert "data-editor-init" not in response.text
    assert not auth.settings.tokens_path.exists()
    assert not client.app.state.memory_editor.sessions
    state = peer.request("/__qa__/state")
    assert [call["tool"] for call in state["calls"]] == ["get_memory", "get_memory"]
    assert state["writes"] == 0


def test_hosted_missing_resource_token_does_not_reach_the_peer(rig):
    client, auth, peer, server = rig
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    auth._store("paged", Token(peer.owner["nonce"], "", time.time() + 600,
                               "https://different.example/mcp"))
    listing = client.get("/s/paged")
    assert listing.status_code == 200 and "missing" in listing.text
    assert "/s/paged/t/get" not in listing.text
    opened = client.get("/memory/paged/open", params={"uuid": seed["pages"][0]["uuid"]})
    assert opened.status_code == 502 and "data-editor-init" not in opened.text
    assert auth._load(server.name) is None
    assert peer.request("/__qa__/state")["calls"] == []
    assert peer.request("/__qa__/state")["writes"] == 0
