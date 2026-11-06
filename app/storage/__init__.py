"""SQLite 存储：会话、任务、作业、记忆、笔记索引与审计。

表结构与 [技术方案](../docs/architecture.md) 的「数据与分级」一致；
`conversations` 用 `link` 标记来源链路，共享层不生成厂商字段。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.events import SessionEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    link TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    audio_ms INTEGER NOT NULL DEFAULT 0,
    usage TEXT NOT NULL DEFAULT '{}',
    call_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id TEXT,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS background_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global',
    sensitivity TEXT NOT NULL DEFAULT 'local-only',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notes_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    sensitivity TEXT NOT NULL DEFAULT 'local-only',
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS push_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id TEXT,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT,
    session_id TEXT,
    name TEXT NOT NULL,
    scope TEXT,
    arguments TEXT NOT NULL DEFAULT '{}',
    result TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


class Storage:
    """进程内唯一的 SQLite 句柄。"""

    def __init__(self, data_dir: Path | str) -> None:
        self._dir = Path(data_dir)
        self._conn: sqlite3.Connection | None = None

    def init(self) -> "Storage":
        self._dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self._dir / "agent.db", check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        return self

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Storage 未初始化，先调用 init()")
        return self._conn

    def record(self, event: SessionEvent) -> None:
        self.conn.execute(
            """
            INSERT INTO conversations
                (session_id, link, role, text, audio_ms, usage, call_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.session_id,
                event.link,
                event.role,
                event.text,
                event.audio_ms,
                json.dumps(event.usage, ensure_ascii=False),
                event.call_id,
                event.created_at,
            ),
        )
        self.conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
