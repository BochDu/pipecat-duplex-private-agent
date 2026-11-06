"""ASR 统一契约。厂商实现放 `app/asr/<厂商>/`，只实现本接口。"""

from __future__ import annotations

import abc


class ASR(abc.ABC):
    """一句一次识别；返回空字符串表示用户结束会话。"""

    @abc.abstractmethod
    async def transcribe(self) -> str:
        ...
