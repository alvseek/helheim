"""Local history: every call, bounded in bytes.

One SQLite file holds what was asked and what came back, so an inspection can be
reopened and replayed. The bound is on bytes rather than rows because a single
``awaken`` is roughly 131 KB.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
create table if not exists calls (
    id           integer primary key autoincrement,
    at           text    not null,
    server       text    not null,
    tool         text    not null,
    args_json    text    not null default '{}',
    response_text text   not null default '',
    payload_json text    not null default 'null',
    envelope_json text   not null default 'null',
    res_bytes    integer not null default 0,
    duration_ms  integer not null default 0,
    outcome      text    not null default 'ok',
    is_error     integer not null default 0
);
create index if not exists calls_at on calls (at desc);
"""


@dataclass
class CallRecord:
    server: str
    tool: str
    arguments: dict[str, Any]
    response_text: str
    payload: Any
    envelope: Any
    duration_ms: int
    outcome: str = "ok"
    is_error: bool = False

    @property
    def res_bytes(self) -> int:
        return len(self.response_text.encode("utf-8"))


class Store:
    def __init__(self, path: Path, max_bytes: int) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._session() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        """One connection per operation, closed on the way out.

        ``with sqlite3.connect(...)`` commits but never closes, which leaks a
        handle per call once FastAPI starts serving from its threadpool.
        """
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def insert(self, record: CallRecord) -> int:
        with self._session() as conn:
            cursor = conn.execute(
                """
                insert into calls (at, server, tool, args_json, response_text,
                                   payload_json, envelope_json, res_bytes,
                                   duration_ms, outcome, is_error)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    record.server,
                    record.tool,
                    json.dumps(record.arguments, ensure_ascii=False),
                    record.response_text,
                    json.dumps(record.payload, ensure_ascii=False),
                    json.dumps(record.envelope, ensure_ascii=False),
                    record.res_bytes,
                    record.duration_ms,
                    record.outcome,
                    1 if record.is_error else 0,
                ),
            )
            call_id = int(cursor.lastrowid or 0)
            self._prune(conn)
            return call_id

    def _prune(self, conn: sqlite3.Connection) -> None:
        total = conn.execute("select coalesce(sum(res_bytes), 0) from calls").fetchone()[0]
        excess = int(total) - self.max_bytes
        if excess <= 0:
            return
        for row in conn.execute("select id, res_bytes from calls order by id asc").fetchall():
            if excess <= 0:
                break
            conn.execute("delete from calls where id = ?", (row["id"],))
            excess -= int(row["res_bytes"])

    def list(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._session() as conn:
            rows = conn.execute(
                """
                select id, at, server, tool, res_bytes, duration_ms, outcome
                from calls order by id desc limit ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, call_id: int) -> dict[str, Any] | None:
        with self._session() as conn:
            row = conn.execute("select * from calls where id = ?", (call_id,)).fetchone()
        if row is None:
            return None
        found = dict(row)
        found["payload"] = json.loads(found["payload_json"])
        found["envelope"] = json.loads(found["envelope_json"])
        found["arguments"] = json.loads(found["args_json"])
        return found

    def total_bytes(self) -> int:
        with self._session() as conn:
            return int(
                conn.execute("select coalesce(sum(res_bytes), 0) from calls").fetchone()[0]
            )
