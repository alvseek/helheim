from __future__ import annotations

import json

import pytest
import tiktoken
from fastapi.testclient import TestClient

from helheim.app import create_app
from helheim.config import load_settings
from helheim.store import CallRecord
from helheim.tokens import TokenCounter


@pytest.mark.parametrize("text", [
    "", "hello world", "こんにちは 🌱 café", "<|endoftext|>", '{"hello": "world"}',
])
def test_text_uses_the_named_encoding_without_special_token_interpretation(text):
    counter = TokenCounter()
    assert counter.encoding_name == "o200k_base"
    assert counter.text(text) == len(tiktoken.get_encoding("o200k_base").encode_ordinary(text))


@pytest.mark.parametrize("value", [
    None, "", [], {}, {"reasoning": [{"content": "こんにちは 🌱"}]}, ["one", 2, True],
])
def test_section_counts_its_json_without_mutating_the_value(value):
    before = json.dumps(value, ensure_ascii=False)
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    assert TokenCounter().value(value) == len(
        tiktoken.get_encoding("o200k_base").encode_ordinary(text)
    )
    assert json.dumps(value, ensure_ascii=False) == before


def stored_call(tmp_path, monkeypatch, *, text=None, payload=None):
    monkeypatch.setenv("HELHEIM_HOME", str(tmp_path))
    app = create_app(load_settings())
    payload = payload if payload is not None else {
        "shared": {"reasoning": [{"content": "hello world " * 20}]},
    }
    # Whitespace is deliberate: the total must count the returned text, not a re-serialization.
    text = text if text is not None else json.dumps(payload, ensure_ascii=False, indent=4)
    call_id = app.state.store.insert(CallRecord(
        server="munnin", tool="awaken", arguments={}, response_text=text,
        payload=payload, envelope={"content": [{"type": "text", "text": text}]},
        duration_ms=3,
    ))
    return app, call_id, text, payload


def test_existing_history_gets_a_total_and_expandable_section_badges(tmp_path, monkeypatch):
    app, call_id, text, payload = stored_call(tmp_path, monkeypatch)
    original = app.state.store.get(call_id)
    response = TestClient(app).get(f"/calls/{call_id}")
    assert response.status_code == 200
    counter = TokenCounter()
    assert f'data-response-tokens="{counter.text(text)}"' in response.text
    assert f'data-tokens="{counter.value(payload["shared"]["reasoning"])}"' in response.text
    assert "o200k_base" in response.text
    assert "not billed usage" in response.text
    assert "not additive" in response.text
    assert app.state.store.get(call_id) == original


def test_large_response_keeps_total_without_building_tree_counts(tmp_path, monkeypatch):
    monkeypatch.setattr("helheim.app.RENDER_LIMIT_BYTES", 10)
    app, call_id, text, _ = stored_call(tmp_path, monkeypatch)
    def refuse_tree(self, value):
        raise AssertionError("oversized response must not measure tree nodes")
    monkeypatch.setattr(TokenCounter, "value", refuse_tree)
    body = TestClient(app).get(f"/calls/{call_id}").text
    assert f'data-response-tokens="{TokenCounter().text(text)}"' in body
    assert "larger than the render limit" in body


def test_tokenizer_failure_does_not_hide_the_response_or_claim_zero(tmp_path, monkeypatch):
    app, call_id, _, _ = stored_call(tmp_path, monkeypatch)
    def unavailable(*args):
        raise OSError("vocabulary unavailable")
    monkeypatch.setattr("helheim.tokens.tiktoken.get_encoding", unavailable)
    response = TestClient(app).get(f"/calls/{call_id}")
    assert response.status_code == 200
    assert "Token count unavailable" in response.text
    assert "reasoning" in response.text
    assert "data-response-tokens" not in response.text
