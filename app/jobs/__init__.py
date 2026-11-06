"""后台作业：长工具异步化、幂等与降级推送。

骨架先落状态机与接口，具体执行与恢复在阶段 5 补齐。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from app.storage import Storage

logger = logging.getLogger(__name__)

STATES = ("queued", "running", "done", "retry")


class JobManager:
    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    async def submit(
        self,
        name: str,
        work: Callable[[], Awaitable[str]],
        *,
        call_id: str | None = None,
    ) -> str:
        """登记并执行一个长工具；幂等键按 `call_id` 或任务名生成。"""
        job_id = call_id or name
        self._storage.conn.execute(
            """
            INSERT INTO background_jobs (job_id, status, payload, created_at)
            VALUES (?, ?, '{}', ?)
            """,
            (job_id, "queued", datetime.now(timezone.utc).isoformat()),
        )
        self._storage.conn.commit()
        # TODO 阶段 5：异步执行、幂等核对、结果回送或降级微信推送。
        logger.info("作业已登记（骨架未执行）：%s", job_id)
        return job_id
