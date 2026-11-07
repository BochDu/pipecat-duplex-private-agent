"""按 `WAKEWORD_ENGINE` 构造唤醒词来源；新增引擎在这里集中登记。

当前只有 `kws`：sherpa-onnx 关键词检测，直接匹配拼音，自造词无需训练。
"""

from __future__ import annotations

import logging

from app.wake.base import WakeListener

logger = logging.getLogger(__name__)


def build_listener(settings, words: list[str]) -> WakeListener:
    engine = (settings.get("WAKEWORD_ENGINE", "") or "kws").strip().lower()
    if engine != "kws":
        raise ValueError(
            f"未实现的唤醒引擎：{engine!r}（只支持 kws，见 docs/config.md「唤醒词」）"
        )
    from app.wake.kws_listener import KwsWakeListener

    return KwsWakeListener(
        words=words,
        model_dir=settings.get("WAKEWORD_KWS_DIR", "") or "data/models/kws",
        keywords_file=settings.get("WAKEWORD_KWS_KEYWORDS", "") or "",
        device=settings.in_device,
        num_threads=settings.int("WAKEWORD_KWS_THREADS", 2, lo=1, hi=16),
        score=settings.num("WAKEWORD_KWS_SCORE", 1.0),
        threshold=settings.num("WAKEWORD_KWS_THRESHOLD", 0.25),
    )
