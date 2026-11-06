"""记忆：会话结束统一抽取，按 `memory_scope` 决定全局或按 Provider 隔离。"""

from __future__ import annotations

from datetime import datetime, timezone

from app.storage import Storage


class MemoryManager:
    def __init__(self, storage: Storage, *, scope: str = "global") -> None:
        if scope not in {"global", "per_provider"}:
            raise ValueError(f"非法的 memory_scope：{scope!r}")
        self._storage = storage
        self._scope = scope

    @property
    def scope(self) -> str:
        return self._scope

    def remember(self, key: str, value: str, *, scope: str | None = None) -> None:
        self._storage.conn.execute(
            """
            INSERT INTO memories (key, value, scope, sensitivity, created_at)
            VALUES (?, ?, ?, 'local-only', ?)
            """,
            (
                key,
                value,
                scope or self._scope,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._storage.conn.commit()

    def extract(self, session_id: str, link: str) -> None:
        # TODO 阶段 5：从本会话事件抽取记忆。写入工具不向模型注册。
        return None
