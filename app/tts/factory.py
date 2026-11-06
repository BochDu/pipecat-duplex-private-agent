"""按 `TTS_PROVIDER` 构造 TTS；未实现的厂商在这里集中报错。"""

from __future__ import annotations

import logging

from app.tts.base import TTS
from app.tts.edge import EdgeTTS

logger = logging.getLogger(__name__)


class NullTTS(TTS):
    """不发声，只记日志；没有播放设备时用。"""

    async def speak(self, text: str) -> None:
        if text.strip():
            logger.info("[TTS] %s", text)

    async def stop(self) -> None:
        return None


def build_tts(
    name: str, voice: str, volume: int = 100, device: str = "default"
) -> TTS:
    if name in ("", "null", "none"):
        return NullTTS()
    if name == "edge":
        return EdgeTTS(voice, volume=volume, device=device)
    raise ValueError(f"未实现的 TTS：{name!r}（厂商实现放 app/tts/<厂商>/ 并在此登记）")
