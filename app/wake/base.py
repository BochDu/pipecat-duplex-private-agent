"""唤醒词来源接口。

待机期独占麦克风，零云端连接；命中后由路由建会话，麦克风交给链路。
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator


class WakeListener(abc.ABC):
    """唤醒词来源；离线引擎实现 `listen()` 即可接入路由。"""

    @abc.abstractmethod
    def listen(self) -> AsyncIterator[str]:
        """持续产出命中的唤醒词（与注册表里的 `word` 一致）。"""
        ...
