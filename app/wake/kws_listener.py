"""离线关键词唤醒：sherpa-onnx 关键词检测（Zipformer transducer）。

音频直接与关键词的拼音序列匹配，不经过「先识别成文字再比对」，
因此自造词不需要训练、也不依赖语言模型里有没有这个词。

模型与关键词文件见 docs/setup.md「⑧ 唤醒词模型」与 docs/config.md。
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

DEFAULT_MODEL_DIR = "data/models/kws"
SAMPLE_RATE = 16000
GIVE_UP_HINT_AFTER = 5  # 连续失败多少次后给完整修法并放慢重试
RETRY_QUIET_EVERY = 12  # 之后每多少次失败提示一次（5 s 一次 ≈ 每分钟）


def _candidates(directory: Path, role: str) -> list[Path]:
    """按「chunk-16 优先、int8 优先」排序该角色的 onnx 文件。"""
    files = sorted(directory.glob(f"{role}-*.onnx"))
    files.sort(
        key=lambda p: (
            0 if "chunk-16" in p.name else 1,
            0 if p.name.endswith(".int8.onnx") else 1,
        )
    )
    return files


def find_models(directory: Path) -> tuple[str, str, str]:
    """配出一组同规格（epoch/chunk）的 encoder / decoder / joiner。

    三者各自优先 int8，缺失时退回 fp32：官方模型只给 decoder 的 fp32 版。
    """
    if not directory.is_dir():
        raise RuntimeError(
            f"KWS 模型目录不存在：{directory}（下载见 docs/setup.md「⑧ 唤醒词模型」）"
        )
    for encoder in _candidates(directory, "encoder"):
        spec = encoder.name[len("encoder-") :].replace(".int8.onnx", "").replace(".onnx", "")
        chosen: list[str] = []
        for role in ("encoder", "decoder", "joiner"):
            for name in (f"{role}-{spec}.int8.onnx", f"{role}-{spec}.onnx"):
                if (directory / name).is_file():
                    chosen.append(str(directory / name))
                    break
            else:
                chosen = []
                break
        if chosen:
            return chosen[0], chosen[1], chosen[2]
    raise RuntimeError(f"KWS 模型不完整（缺 encoder/decoder/joiner）：{directory}")


def parse_result(value) -> str:
    """把 get_result 的返回值统一成关键词文本，未命中返回空串。"""
    if not value:
        return ""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{"):
            try:
                return str(json.loads(text).get("keyword") or "").strip()
            except json.JSONDecodeError:
                return ""
        return text
    if isinstance(value, dict):
        return str(value.get("keyword") or "").strip()
    return str(value).strip()


class KwsWakeListener(WakeListener):
    """待机期常驻监听；命中后先交还麦克风再 yield。"""

    def __init__(
        self,
        *,
        words: list[str],
        model_dir: str = DEFAULT_MODEL_DIR,
        keywords_file: str = "",
        device: str = "default",
        rate: int = SAMPLE_RATE,
        num_threads: int = 2,
        score: float = 1.0,
        threshold: float = 0.25,
    ) -> None:
        self._words = [w for w in words if w]
        self._model_dir = Path(model_dir or DEFAULT_MODEL_DIR).expanduser()
        self._keywords_file = (
            Path(keywords_file).expanduser()
            if keywords_file
            else self._model_dir / "keywords.txt"
        )
        self._device = device or "default"
        self._rate = rate
        self._num_threads = max(1, int(num_threads))
        self._score = float(score)
        self._threshold = float(threshold)
        self._spotter = None

    def load(self) -> None:
        try:
            import sherpa_onnx
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "未安装 sherpa-onnx：.venv/bin/python -m pip install sherpa-onnx"
            ) from exc

        if not self._keywords_file.is_file():
            raise RuntimeError(
                f"关键词文件不存在：{self._keywords_file}"
                "（拼音 token 写法见 docs/config.md，安装见 docs/setup.md「⑧ 唤醒词模型」）"
            )
        tokens = self._model_dir / "tokens.txt"
        encoder, decoder, joiner = find_models(self._model_dir)

        common = dict(
            tokens=str(tokens),
            encoder=encoder,
            decoder=decoder,
            joiner=joiner,
            keywords_file=str(self._keywords_file),
            num_threads=self._num_threads,
            max_active_paths=4,
            provider="cpu",
        )
        try:
            self._spotter = sherpa_onnx.KeywordSpotter(
                **common,
                keywords_score=self._score,
                keywords_threshold=self._threshold,
            )
        except TypeError:  # 旧版绑定没有这两个参数，用文件里的 :score #threshold
            self._spotter = sherpa_onnx.KeywordSpotter(**common)
        logger.info(
            "唤醒引擎已加载：sherpa-onnx KWS（监听 %s，线程 %d）",
            "、".join(self._words),
            self._num_threads,
        )

    def _reset(self, stream) -> None:
        """命中后清空解码状态，避免同一句话连判两次。"""
        reset = getattr(self._spotter, "reset_stream", None)
        if callable(reset):
            try:
                reset(stream)
            except Exception:  # noqa: BLE001 - 复位失败不影响下一次建流
                logger.debug("KWS 复位异常", exc_info=True)

    async def listen(self) -> AsyncIterator[str]:
        if not self._words:
            raise RuntimeError("注册表里没有可监听的唤醒词")
        self.load()
        import numpy as np

        failures = 0
        while True:
            recorder = MicRecorder(device=self._device, rate=self._rate)
            stream = self._spotter.create_stream()
            try:
                async for frame in recorder.frames():
                    failures = 0
                    if len(frame) != FRAME_BYTES:
                        continue
                    samples = np.frombuffer(frame, dtype="<i2").astype("float32") / 32768.0
                    stream.accept_waveform(self._rate, samples)
                    while self._spotter.is_ready(stream):
                        self._spotter.decode_stream(stream)
                    word = parse_result(self._spotter.get_result(stream))
                    if not word:
                        continue
                    if word not in self._words:
                        logger.warning(
                            "命中未注册的关键词 %s（检查 keywords.txt 与注册表是否一致）",
                            word,
                        )
                        self._reset(stream)
                        continue
                    logger.info("唤醒命中：%s", word)
                    await recorder.stop()  # 先交还麦克风，再交给路由
                    yield word
                    break
            except RuntimeError as exc:
                failures += 1
                if failures == 1:
                    logger.warning("唤醒监听中断：%s", exc)
                elif failures == GIVE_UP_HINT_AFTER:
                    # 持续打不开：给出可执行的修法，之后降低提示频率
                    logger.error(
                        "麦克风 %s 连续 %d 次无法采集：在 .env 写 AUDIO_IN_DEVICE=plughw:N,0"
                        "（用 arecord -l 查，取 USB 声卡那一行的 card 与 device）",
                        self._device,
                        failures,
                    )
                elif failures % RETRY_QUIET_EVERY == 0:
                    logger.warning(
                        "麦克风 %s 仍无法采集（第 %d 次），检查 AUDIO_IN_DEVICE",
                        self._device,
                        failures,
                    )
                await asyncio.sleep(1 if failures < GIVE_UP_HINT_AFTER else 5)
            finally:
                await recorder.stop()
