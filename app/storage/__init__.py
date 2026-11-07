"""SQLite 存储：会话、任务、作业、记忆、笔记索引与审计。

`conversations` 走内存队列 + 后台批量提交：S2S 每段文字都会产生一条事件，
逐条 `commit()` 在 SD 卡上会阻塞事件循环，直接拖慢采集与打断时序。

表结构与 [技术方案](../docs/architecture.md) 的「数据与分级」一致；
`conversations` 用 `link` 标记来源链路，共享层不生成厂商字段。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path

from app.events import SessionEvent

logger = logging.getLogger(__name__)

FLUSH_INTERVAL_SEC = 0.5  # 后台落盘的最长合并窗口：同一轮增量一次写完
INSERT_EVENT = """
    INSERT INTO conversations
        (session_id, link, role, text, audio_ms, usage, call_id, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

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
        self._pending: list[tuple] = []
        self._flush_task: asyncio.Task | None = None
        self._flush_wake: asyncio.Event | None = None

    def init(self) -> "Storage":
        self._dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self._dir / "agent.db", check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        # WAL 让读写不互相阻塞，NORMAL 在 SD 卡上少一次 fsync
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        return self

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Storage 未初始化，先调用 init()")
        return self._conn

    def record(self, event: SessionEvent) -> None:
        """只入队：落盘交给后台 task，事件回调不被磁盘拖住。"""
        self._pending.append(
            (
                event.session_id,
                event.link,
                event.role,
                event.text,
                event.audio_ms,
                json.dumps(event.usage, ensure_ascii=False),
                event.call_id,
                event.created_at,
            )
        )
        self._wake()

    def flush(self) -> None:
        """把待写事件一次 executemany + commit；失败只告警，不拖垮会话。"""
        if not self._pending:
            return
        rows, self._pending = self._pending, []
        try:
            self.conn.executemany(INSERT_EVENT, rows)
            self.conn.commit()
        except sqlite3.Error:
            logger.warning("会话事件落盘失败，丢弃 %d 条", len(rows), exc_info=True)

    def _wake(self) -> None:
        """有事件就确保后台 task 在跑；没有事件循环时由 `flush()` 显式落盘。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._flush_wake is None:
            self._flush_wake = asyncio.Event()
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = loop.create_task(self._flush_loop())
        self._flush_wake.set()

    async def _flush_loop(self) -> None:
        assert self._flush_wake is not None
        while True:
            await self._flush_wake.wait()
            self._flush_wake.clear()
            await asyncio.sleep(FLUSH_INTERVAL_SEC)
            self.flush()

    def close(self) -> None:
        if self._flush_task is not None:
            self._flush_task.cancel()
            self._flush_task = None
        self.flush()
        if self._conn is not None:
            self._conn.close()
            self._conn = None
