"""TTS 统一契约。厂商实现放 `app/tts/<厂商>/`，只实现本接口。"""

from __future__ import annotations

import abc


class TTS(abc.ABC):
    @abc.abstractmethod
    async def speak(self, text: str) -> None:
        ...

    @abc.abstractmethod
    async def stop(self) -> None:
        ...
