from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from helheim.app import create_app
from helheim.config import load_settings
from helheim.store import CallRecord


def test_annotations_win_over_names() -> None:
    from helheim.mcp_client import ToolInfo

    annotated_read = ToolInfo(
        name="soft_delete",
        description="",
        input_schema={},
        annotations={"destructive": False},
    )
    assert annotated_read.destructive is False

    annotated_write = ToolInfo(
        name="awaken",
        description="",
        input_schema={},
        annotations={"destructive": True},
    )
    assert annotated_write.destructive is True

    by_name = ToolInfo(
        name="update_memory", description="", input_schema={}, annotations={}
    )
    assert by_name.destructive is True

    read_only = ToolInfo(
        name="edit_thing",
        description="",
        input_schema={},
        annotations={"read_only": True},
    )
    assert read_only.destructive is False

    plain = ToolInfo(name="query", description="", input_schema={}, annotations={})
    assert plain.destructive is False


def test_a_call_renders_as_a_tree(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HELHEIM_HOME", str(tmp_path))
    app = create_app(load_settings())
    client = TestClient(app)

    payload = {
        "shared": {"reasoning": [{"title": "one"}, {"title": "two"}]},
        "identity": [{"name": "meta"}],
    }
    call_id = app.state.store.insert(
        CallRecord(
            server="munnin",
            tool="awaken",
            arguments={"agent": "meta"},
            response_text="x" * 131,
            payload=payload,
            envelope={"content": [{"type": "text", "text": "..."}]},
            duration_ms=1180,
        )
    )

    response = client.get(f"/calls/{call_id}")
    assert response.status_code == 200
    body = response.text
    assert "131" in body
    assert "1180" in body
    assert "shared" in body and "reasoning" in body and "identity" in body
    assert body.count("<details") >= 4
    assert "one" in body and "two" in body


def test_history_lists_calls(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HELHEIM_HOME", str(tmp_path))
    app = create_app(load_settings())
    client = TestClient(app)
    app.state.store.insert(
        CallRecord(
            server="munnin",
            tool="awaken",
            arguments={},
            response_text="ok",
            payload={},
            envelope={},
            duration_ms=5,
        )
    )
    response = client.get("/history")
    assert response.status_code == 200
    assert "awaken" in response.text
    assert "bytes stored" in response.text


def test_a_server_without_a_token_says_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HELHEIM_HOME", str(tmp_path))
    client = TestClient(create_app(load_settings()))
    response = client.get("/s/munnin")
    assert response.status_code == 200
    assert "missing" in response.text
    assert "awaken" not in response.text


def test_a_tool_form_without_a_token_explains_itself(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HELHEIM_HOME", str(tmp_path))
    client = TestClient(create_app(load_settings()))
    response = client.get("/s/munnin/t/awaken")
    assert response.status_code == 409
    assert "not usable yet" in response.text


def test_an_unknown_server_is_a_404(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HELHEIM_HOME", str(tmp_path))
    client = TestClient(create_app(load_settings()))
    assert client.get("/s/nowhere").status_code == 404
    assert client.get("/calls/999").status_code == 404


def test_a_response_over_the_render_limit_is_not_expanded(
    tmp_path: Path, monkeypatch
) -> None:
    import helheim.app as app_module

    monkeypatch.setenv("HELHEIM_HOME", str(tmp_path))
    monkeypatch.setattr(app_module, "RENDER_LIMIT_BYTES", 10)
    app = app_module.create_app(load_settings())
    client = TestClient(app)
    call_id = app.state.store.insert(
        CallRecord(
            server="munnin",
            tool="awaken",
            arguments={},
            response_text="x" * 200,
            payload={"a": {"b": 1}},
            envelope={"content": []},
            duration_ms=3,
        )
    )
    response = client.get(f"/calls/{call_id}")
    assert response.status_code == 200
    assert "larger than the render limit" in response.text
    assert "<details" in response.text  # the raw envelope is still reachable
