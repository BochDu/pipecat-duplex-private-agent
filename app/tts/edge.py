"""edge-tts 实现：云、免费、无需凭据；文本出网到微软服务。"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from app.audio.player import (
    CLOSE_TIMEOUT,
    PLAYBACK_RATE,
    PREROLL_LONG_MS,
    silence,
    spawn,
)
from app.tts.base import TTS

logger = logging.getLogger(__name__)

PUMP_BYTES = 4096
DEBUG_PCM = Path("data") / "tts-last.pcm"  # 最近一次送播放的 PCM，排查起播用


class EdgeTTS(TTS):
    """edge-tts 边合成边解码边播：不整段等，首声更快。"""

    def __init__(
        self,
        voice: str = "zh-CN-XiaoxiaoNeural",
        volume: int = 100,
        device: str = "default",
    ) -> None:
        self._voice = voice
        self._volume = max(0, min(200, int(volume)))
        self._device = device or "default"
        self._procs: list[asyncio.subprocess.Process] = []

    def _volume_args(self) -> list[str]:
        if self._volume == 100:
            return []
        return ["-af", f"volume={self._volume / 100:.2f}"]

    async def speak(self, text: str) -> None:
        if not text.strip():
            return
        try:
            import edge_tts
        except ModuleNotFoundError as exc:
            raise RuntimeError("未安装 edge-tts：pip install edge-tts") from exc

        started = time.monotonic()
        # 合成结果直接进 ffmpeg 的 stdin，不再落临时文件，省一次写盘与等待
        decoder = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            *self._volume_args(),
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-ac",
            "1",
            "-ar",
            str(PLAYBACK_RATE),
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._procs = [decoder]
        player = await spawn(self._device)
        self._procs = [decoder, player]
        pump = asyncio.create_task(self._pump(decoder.stdout, player.stdin))
        try:
            assert decoder.stdin is not None
            first = 0.0
            async for chunk in edge_tts.Communicate(text, self._voice).stream():
                if chunk["type"] != "audio":
                    continue
                if not first:
                    first = time.monotonic()
                    logger.info("TTS 首包 %.0f ms", (first - started) * 1000)
                decoder.stdin.write(chunk["data"])
                await decoder.stdin.drain()
            decoder.stdin.close()
            await pump
            await asyncio.gather(decoder.wait(), player.wait())
        finally:
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
            await self.stop()

    async def _pump(self, source, target) -> None:
        """ffmpeg 的 PCM 流式写给 aplay；预热与首块一起写，避免设备空转。"""
        captured = bytearray()
        try:
            first = await source.read(PUMP_BYTES)
            if not first:
                return
            # 预热和首块一次写入：分开写时若首包来迟，aplay 播完静音会 underrun
            target.write(silence(PREROLL_LONG_MS))
            if len(captured) < PLAYBACK_RATE * 2 * 5:
                captured.extend(first)
            target.write(first)
            await target.drain()
            while True:
                chunk = await source.read(PUMP_BYTES)
                if not chunk:
                    break
                if len(captured) < PLAYBACK_RATE * 2 * 5:
                    captured.extend(chunk)
                target.write(chunk)
                await target.drain()
            target.close()
        except Exception:  # noqa: BLE001 - 打断时管道已关闭
            logger.debug("播放管道中断", exc_info=True)
        finally:
            self._dump(captured)

    def _dump(self, pcm: bytearray) -> None:
        """存下送播放的前 5 秒 PCM，供起播问题排查。"""
        if not pcm:
            return
        try:
            DEBUG_PCM.parent.mkdir(parents=True, exist_ok=True)
            DEBUG_PCM.write_bytes(bytes(pcm))
        except OSError:
            logger.debug("保存调试 PCM 失败", exc_info=True)

    async def stop(self) -> None:
        """终止并回收本轮的 ffmpeg / aplay；单个进程卡住不阻断其余清理。"""
        procs, self._procs = self._procs, []
        for proc in procs:
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
        for proc in procs:
            await self._reap(proc)

    async def _reap(self, proc: asyncio.subprocess.Process) -> None:
        try:
            await asyncio.wait_for(proc.wait(), CLOSE_TIMEOUT)
        except Exception:  # noqa: BLE001 - 超时或等待异常都继续升级回收
            logger.debug("TTS 子进程未按时退出（pid %s）", proc.pid, exc_info=True)
        if proc.returncode is not None:
            return
        try:
            proc.kill()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), CLOSE_TIMEOUT)
        except Exception:  # noqa: BLE001 - 终止期不阻断
            logger.warning("TTS 子进程未能回收（pid %s）", proc.pid)
