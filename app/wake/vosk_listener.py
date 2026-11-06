"""离线声学唤醒：Vosk 中文模型 + 唤醒词（含变体）匹配。

待机期独占麦克风、零云端连接；命中后先停掉采集再 `yield`，会话侧才能拿到设备
（否则 `app/sessions/s2s/pipeline.py` 的采集会 `Device or resource busy`）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from app.audio import FRAME_BYTES, MicRecorder
from app.wake.base import WakeListener

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "data/models/wakeword"


def normalize(text: str) -> str:
    """只留字母数字与汉字，去掉空格和标点。"""
    return "".join(ch for ch in text if ch.isalnum())


def close_enough(word: str, text: str) -> bool:
    """容错匹配：文本包含该词，或长度相差 ≤ 1 且编辑距离 ≤ 1。"""
    if not word or not text:
        return False
    if word in text:
        return True
    if len(text) < len(word) or len(text) - len(word) > 1:
        return False
    prev = list(range(len(text) + 1))
    for i, ca in enumerate(word, 1):
        cur = [i]
        for j, cb in enumerate(text, 1):
            cur.append(
                min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (0 if ca == cb else 1))
            )
        prev = cur
    return prev[-1] <= 1


def match_words(word: str, aliases: list[str], text: str) -> bool:
    """整句或句中片段命中 `word` / `aliases` 任一写法。

    三字以上才做滑窗：两字词（豆包）在长句里切片段会把日常对话误判成唤醒。
    """
    variants = [word, *aliases]
    if any(close_enough(variant, text) for variant in variants):
        return True
    size = len(word)
    if size < 3:
        return False
    for start in range(len(text) - size + 1):
        span = text[start : start + size]
        if any(close_enough(variant, span) for variant in variants):
            return True
    return False


class VoskWakeListener(WakeListener):
    """待机期常驻监听；每次命中都重启一次采集，确保麦克风能被会话接手。"""

    def __init__(
        self,
        *,
        words: list[str],
        aliases: dict[str, list[str]] | None = None,
        model_path: str = DEFAULT_MODEL,
        device: str = "default",
        rate: int = 16000,
        verbose: bool = False,
        on_text=None,
    ) -> None:
        self._words = [w for w in words if w]
        self._aliases = dict(aliases or {})
        self._model_path = model_path or DEFAULT_MODEL
        self._device = device or "default"
        self._rate = rate
        self._verbose = verbose
        self._on_text = on_text
        self._model = None
        self._recognizer = None

    # --- 模型 -------------------------------------------------------------
    def load(self) -> None:
        """加载模型；`listen()` 会自动调用，自检脚本可提前加载避免吞掉开头。"""
        try:
            from vosk import Model, SetLogLevel
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "未安装 vosk：.venv/bin/python -m pip install vosk"
            ) from exc

        path = Path(self._model_path).expanduser()
        if not path.exists():
            raise RuntimeError(
                f"唤醒模型目录不存在：{path}（下载见 preflight/README.md「唤醒词」）"
            )
        SetLogLevel(-1)  # 关掉 kaldi 的刷屏日志
        self._model = Model(str(path))
        self._recognizer = self._make_recognizer()
        logger.info(
            "唤醒模型已加载：%s（监听 %s）",
            path,
            "、".join(self._words),
        )

    def _make_recognizer(self):
        from vosk import KaldiRecognizer

        return KaldiRecognizer(self._model, self._rate)

    def _reset(self) -> None:
        """命中后丢弃残余音频状态，避免同一句话被连判两次。"""
        if self._model is not None:
            self._recognizer = self._make_recognizer()

    def _detect(self, frame: bytes) -> str | None:
        """喂一帧，返回命中的唤醒词（始终是注册表里的 `word`）。"""
        if self._recognizer is None:
            return None
        text = ""
        if self._recognizer.AcceptWaveform(frame):
            text = normalize(json.loads(self._recognizer.Result()).get("text") or "")
            if text:
                if self._verbose:
                    logger.info("听到：%s", text)
                if self._on_text is not None:
                    self._on_text(text)
        else:
            partial = json.loads(self._recognizer.PartialResult()).get("partial") or ""
            text = normalize(partial)
        if not text:
            return None
        for word in self._words:
            if match_words(word, self._aliases.get(word, []), text):
                return word
        return None

    # --- 监听 -------------------------------------------------------------
    async def listen(self) -> AsyncIterator[str]:
        if not self._words:
            raise RuntimeError("注册表里没有可监听的唤醒词")
        self.load()
        while True:
            recorder = MicRecorder(device=self._device, rate=self._rate)
            try:
                async for frame in recorder.frames():
                    if len(frame) != FRAME_BYTES:
                        continue
                    word = self._detect(frame)
                    if word is None:
                        continue
                    logger.info("唤醒命中：%s", word)
                    self._reset()
                    await recorder.stop()  # 先交还麦克风，再交给路由
                    yield word
                    break
            except RuntimeError as exc:
                logger.warning("唤醒监听中断：%s", exc)
                await asyncio.sleep(1)
            finally:
                await recorder.stop()
