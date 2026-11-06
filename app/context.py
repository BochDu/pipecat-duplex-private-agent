"""应用级单例容器。

启动时组装一次，注入唤醒路由与各 Provider，保证 ToolRegistry / Memory /
Storage / Scheduler 全项目唯一。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Registry, Settings
from app.events import SessionEvent
from app.jobs import JobManager
from app.memory import MemoryManager
from app.notes import NotesIndex
from app.scheduler import Scheduler
from app.storage import Storage
from app.tools import ToolRegistry


@dataclass
class AppContext:
    settings: Settings
    registry: Registry
    storage: Storage
    tools: ToolRegistry
    memory: MemoryManager
    jobs: JobManager
    scheduler: Scheduler
    notes: NotesIndex

    def record(self, event: SessionEvent) -> None:
        self.storage.record(event)
