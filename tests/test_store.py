from __future__ import annotations

from pathlib import Path

from helheim.store import CallRecord, Store


def record(text: str, tool: str = "awaken") -> CallRecord:
    return CallRecord(
        server="munnin",
        tool=tool,
        arguments={"agent": "meta"},
        response_text=text,
        payload={"text": text},
        envelope={"content": []},
        duration_ms=12,
    )


def test_a_call_round_trips(tmp_path: Path) -> None:
    store = Store(tmp_path / "h.db", max_bytes=1024 * 1024)
    call_id = store.insert(record("hello"))
    found = store.get(call_id)
    assert found is not None
    assert found["server"] == "munnin"
    assert found["tool"] == "awaken"
    assert found["arguments"] == {"agent": "meta"}
    assert found["payload"] == {"text": "hello"}
    assert found["res_bytes"] == 5
    assert found["outcome"] == "ok"


def test_list_is_newest_first(tmp_path: Path) -> None:
    store = Store(tmp_path / "h.db", max_bytes=1024 * 1024)
    first = store.insert(record("one"))
    second = store.insert(record("two"))
    listed = store.list()
    assert [row["id"] for row in listed] == [second, first]


def test_the_cap_is_in_bytes_and_prunes_the_oldest(tmp_path: Path) -> None:
    store = Store(tmp_path / "h.db", max_bytes=25)
    store.insert(record("a" * 10))
    store.insert(record("b" * 10))
    store.insert(record("c" * 10))
    remaining = store.list()
    assert len(remaining) == 2
    assert store.total_bytes() <= 25
    assert [row["id"] for row in remaining] == [3, 2]


def test_a_single_call_larger_than_the_cap_leaves_steering_room(tmp_path: Path) -> None:
    store = Store(tmp_path / "h.db", max_bytes=5)
    store.insert(record("a" * 50))
    assert store.list() == []
    assert store.total_bytes() == 0


def test_an_unknown_call_is_none(tmp_path: Path) -> None:
    store = Store(tmp_path / "h.db", max_bytes=1024)
    assert store.get(999) is None
