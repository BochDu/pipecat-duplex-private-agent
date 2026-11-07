"""ASR 统一契约。厂商实现放 `app/asr/<厂商>/`，只实现本接口。"""

from __future__ import annotations

import abc


class ASR(abc.ABC):
    """一句一次识别；返回空字符串表示用户结束会话。"""

    @abc.abstractmethod
    async def transcribe(self, *, deadline: float | None = None) -> str:
        """录一句并识别。

        `deadline` 是 `time.monotonic()` 时间点，限制「等起音」的额度：到点还没
        人声就返回空字符串。不传则用配置里的等待时长。重试复用同一额度。
        """
