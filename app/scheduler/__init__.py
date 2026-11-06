"""定时调度：APScheduler 幂等恢复。

骨架只定义启停接口；具体作业注册与恢复在阶段 5 落地。
"""

from __future__ import annotations

import logging

from app.storage import Storage

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, storage: Storage) -> None:
        self._storage = storage
        self._started = False
        self._backend = None

    async def start(self) -> None:
        # TODO 阶段 5：延迟导入 APScheduler 并装载持久化作业。
        if self._started:
            return
        self._started = True
        logger.info("调度器骨架启动（未装载作业）")

    async def stop(self) -> None:
        self._started = False
        self._backend = None

    @property
    def started(self) -> bool:
        return self._started
