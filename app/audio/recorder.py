"""麦克风采集：arecord 子进程，16 kHz 单声道 s16le，20 ms 一帧。

`frames()` 供 S2S 连续上行；`record_utterance()` 起音 + 静音判停按句录制，
供一句话识别用。判停阈值按环境调试，可用 `AUDIO_IN_SILENCE_RMS` 覆盖。
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time

logger = logging.getLogger(__name__)

FRAME_MS = 20
FRAME_BYTES = 640  # 16000 Hz * 2 字节 * 20 ms
#: 与 config/defaults.env 的 AUDIO_IN_SILENCE_RMS 一致；缺键时两条链路同口径
DEFAULT_THRESHOLD = 900.0


def rms(frame: bytes) -> float:
    """一帧 s16le 的均方根能量，粗略代表说话声强。"""
    count = len(frame) // 2
    if count == 0:
        return 0.0
    samples = struct.unpack(f"<{count}h", frame[: count * 2])
    return (sum(sample * sample for sample in samples) / count) ** 0.5


class MicRecorder:
    """arecord 封装；同一实例的 `frames()` 迭代期间不可并发再启动。"""

    def __init__(self, device: str = "default", rate: int = 16000) -> None:
        self._device = device
        self._rate = rate
        self._proc: asyncio.subprocess.Process | None = None

    async def frames(self):
        """连续输出 20 ms 原始帧；调用 `stop()` 结束迭代。"""
        if self._proc is not None:
            raise RuntimeError("MicRecorder 已在采集，先 stop() 再重新开始")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                "arecord",
                "-q",
                "-D", self._device,
                "-f", "S16_LE",
                "-r", str(self._rate),
                "-c", "1",
                "-t", "raw",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("未安装 arecord：sudo apt install -y alsa-utils") from exc
        logger.info("麦克风开始采集：%s（%d Hz）", self._device, self._rate)
        count = 0
        error: str | None = None
        try:
            assert self._proc.stdout is not None
            while True:
                frame = await self._proc.stdout.read(FRAME_BYTES)
                if not frame:
                    break
                while len(frame) < FRAME_BYTES:
                    rest = await self._proc.stdout.read(FRAME_BYTES - len(frame))
                    if not rest:
                        break
                    frame += rest
                if len(frame) < FRAME_BYTES:
                    break
                count += 1
                yield frame
        finally:
            error = await self.stop()
        if count == 0:
            # 一帧都没采到：把 arecord 的 stderr 原样抛给调用方，便于查设备占用
            raise RuntimeError(
                f"arecord 未采到音频（设备 {self._device}）：{error or '无输出'}"
            )

    async def record_utterance(
        self,
        *,
        start_timeout: float = 8.0,
        silence_ms: int = 800,
        max_ms: int = 15000,
        threshold: float | None = None,
    ) -> bytes:
        """录一句话；无人说话返回空 bytes（调用方按结束会话处理）。

        起音后连续静音超过 `silence_ms` 或总时长超过 `max_ms` 判停。
        """
        limit = threshold if threshold is not None else DEFAULT_THRESHOLD
        started = time.monotonic()
        speech_started: float | None = None
        last_voice: float | None = None
        buffer = bytearray()
        async for frame in self.frames():
            now = time.monotonic()
            level = rms(frame)
            if speech_started is None:
                if level >= limit:
                    speech_started = now
                    last_voice = now
                    buffer.extend(frame)
                elif now - started > start_timeout:
                    logger.info("起音超时（%.1fs 无声）", start_timeout)
                    return b""
                continue
            buffer.extend(frame)
            if level >= limit:
                last_voice = now
            assert last_voice is not None
            if (now - last_voice) * 1000 >= silence_ms:
                break
            if (now - speech_started) * 1000 >= max_ms:
                logger.info("达到单句最大时长 %dms", max_ms)
                break
        return bytes(buffer)

    async def stop(self) -> str | None:
        """结束采集；返回 arecord 的 stderr 文本，供上层判断失败原因。"""
        proc, self._proc = self._proc, None
        if proc is None:
            return None
        if proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=2)
            except Exception:  # noqa: BLE001 - 释放失败不阻断
                logger.debug("arecord 释放异常", exc_info=True)
        if proc.stderr is None:
            return None
        try:
            raw = await asyncio.wait_for(proc.stderr.read(), timeout=1)
        except Exception:  # noqa: BLE001 - 读不到 stderr 不影响主流程
            return None
        text = " ".join(raw.decode("utf-8", "replace").split())
        return text or None
