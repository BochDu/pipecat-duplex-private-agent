"""端到端实时语音 Pipeline 模板。

只负责通用编排：采集 → Provider → 播报、静默超时与资源释放。
厂商差异全部在 `app/providers/<provider>/`，本模板不出现品牌代码。

音频路径（不经 pipecat，子进程直通）：
- 上行：arecord 16 kHz 帧流 → `provider.send_audio_frame`（实时节奏由采集端保证）
- 下行：Provider `emit_audio` → `feed_audio` 入队 → aplay 播放 24 kHz
- 结束：无人说话且无播报超过 `SILENCE_TIMEOUT_SEC`，或 Provider 断开，或外部 `stop()`

设备：`AUDIO_IN_DEVICE` / `AUDIO_OUT_DEVICE` 分别指定，缺省退回 `AUDIO_DEVICE`。
播放走 aplay（与 `preflight/audio-check.sh` 同一条路径），设备名写错时能直接报出来。

已知限制（TODO）：扬声器回采会被当作说话声，打断依赖服务端 VAD；
本模板暂不做本地回声消除与打断停播。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from app.audio import MicRecorder, rms, scale_pcm16
from app.config import Settings
from app.events import SessionEvent

logger = logging.getLogger(__name__)

FRAME_BYTES = 640
PLAYBACK_RATE = 24000
PREROLL_MS = 150  # 播放前先喂的静音：声卡打开后头几十毫秒会丢采样
DEFAULT_THRESHOLD = 500.0
LEVEL_LOG_SEC = 5.0
SPEECH_HOLD_SEC = 0.6
SEND_TIMEOUT = 6.0


class S2SPipeline:
    link = "s2s"

    def __init__(
        self,
        *,
        provider,  # BaseProvider
        settings: Settings,
        on_event: Callable[[SessionEvent], None],
        on_audio: Callable[[bytes], None] | None = None,
    ) -> None:
        self._provider = provider
        self._settings = settings
        self._on_event = on_event
        self._running = False
        self._audio_queue: asyncio.Queue[bytes] | None = None
        self._played_bytes = 0

    def feed_audio(self, pcm: bytes) -> None:
        """Provider 下行音频入队，由播放侧消费。"""
        if self._audio_queue is None:
            self._audio_queue = asyncio.Queue()
        self._audio_queue.put_nowait(pcm)

    async def run(self) -> None:
        in_device = self._device("AUDIO_IN_DEVICE")
        out_device = self._device("AUDIO_OUT_DEVICE")
        silence_sec = float(self._settings.get("SILENCE_TIMEOUT_SEC", "") or 20)
        recorder = MicRecorder(device=in_device)
        player = await self._spawn_player(out_device)
        await self._prime_player(player)
        # 不重建队列：provider.start() 后、run() 前到达的下行音频已在队里
        if self._audio_queue is None:
            self._audio_queue = asyncio.Queue()
        self._running = True
        logger.info(
            "S2S 会话开始：采集 %s → 播放 %s，%.0fs 无说话判停",
            in_device,
            out_device,
            silence_sec,
        )
        self._log_volume()
        try:
            tasks = [
                asyncio.create_task(self._upstream(recorder, silence_sec)),
                asyncio.create_task(self._playback(player)),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                exc = task.exception()
                if exc is not None:
                    raise exc
        finally:
            self._running = False
            await recorder.stop()
            await self._close_player(player)
            logger.info(
                "S2S 会话结束（下行共播放 %.1fs）",
                self._played_bytes / (PLAYBACK_RATE * 2),
            )

    async def stop(self) -> None:
        self._running = False

    def _log_volume(self) -> None:
        """音量分两层：服务端合成端与本地播放端，各自百分比独立。"""
        endpoint = self._settings.out_volume
        server = int(getattr(self._provider, "loudness_percent", 100))
        logger.info("音量：服务端 %d%% × 播放端 %d%%（100%% 为原始）", server, endpoint)
        if server > 100 and endpoint > 100:
            logger.warning(
                "两层同时放大（%d%% × %d%%）会叠乘削波：把 AUDIO_OUT_VOLUME 留在 100，"
                "只调 S2S_DOUBAO_LOUDNESS",
                server,
                endpoint,
            )

    def _device(self, key: str) -> str:
        """取设备名；两个新键都没配时退回旧的 `AUDIO_DEVICE`。"""
        return (
            self._settings.get(key, "")
            or self._settings.get("AUDIO_DEVICE", "")
            or "default"
        )

    async def _spawn_player(self, device: str) -> asyncio.subprocess.Process:
        """起 aplay 接收裸 PCM；设备写错会在半秒内退出而非静默失败。"""
        try:
            player = await asyncio.create_subprocess_exec(
                "aplay",
                "-q",
                "-D", device,
                "-t", "raw",
                "-f", "S16_LE",
                "-r", str(PLAYBACK_RATE),
                "-c", "1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("未安装 aplay：sudo apt install -y alsa-utils") from exc
        await asyncio.sleep(0.15)
        if player.returncode is not None:
            raise RuntimeError(
                f"播放器启动失败（退出码 {player.returncode}）："
                f"检查 AUDIO_OUT_DEVICE={device!r}，用 `aplay -l` 看可用设备"
            )
        return player

    async def _prime_player(self, player: asyncio.subprocess.Process) -> None:
        """先喂一段静音，抵消 aplay 打开声卡后的起播丢帧。"""
        silence = b"\x00" * (PLAYBACK_RATE * 2 * PREROLL_MS // 1000)
        try:
            assert player.stdin is not None
            player.stdin.write(silence)
            await player.stdin.drain()
        except Exception:  # noqa: BLE001 - 预热失败不阻断会话
            logger.debug("播放器预热失败", exc_info=True)

    async def _close_player(self, player: asyncio.subprocess.Process) -> None:
        try:
            if player.stdin is not None and not player.stdin.is_closing():
                player.stdin.close()
        except Exception:  # noqa: BLE001 - 关闭期不阻断
            logger.debug("播放器 stdin 关闭异常", exc_info=True)
        try:
            await asyncio.wait_for(player.wait(), timeout=2)
            return
        except asyncio.TimeoutError:
            logger.debug("播放器未在 2s 内自然退出，强制结束")
        except Exception:  # noqa: BLE001
            logger.debug("播放器等待异常", exc_info=True)
        try:
            player.terminate()
            await asyncio.wait_for(player.wait(), timeout=2)
        except Exception:  # noqa: BLE001 - 释放失败不阻断
            logger.debug("播放器释放异常", exc_info=True)

    async def _upstream(self, recorder: MicRecorder, silence_sec: float) -> None:
        """麦克风帧流持续上行；静默超时或 Provider 断开则结束会话。

        续时：检测到人声、或下行仍在播报，都把静默计时推到现在。
        """
        threshold = float(self._settings.get("AUDIO_IN_SILENCE_RMS", "") or DEFAULT_THRESHOLD)
        last_speech = time.monotonic()  # 最近一次检测到人声，用于判定说完
        active_at = last_speech  # 静默计时起点：人声或播报取最近一次
        sent = 0
        peak = 0.0
        speaking = False
        last_report = last_speech
        last_level = 0.0
        last_played = self._played_bytes
        async for frame in recorder.frames():
            now = time.monotonic()
            if not self._running:
                return
            if not self._provider.is_running():
                logger.info("Provider 已断开，结束会话")
                return
            if len(frame) == FRAME_BYTES:
                try:
                    await asyncio.wait_for(
                        self._provider.send_audio_frame(frame), timeout=SEND_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    logger.warning("上行发送超时 %.0fs，结束会话", SEND_TIMEOUT)
                    return
            sent += 1
            played = self._played_bytes
            if played > last_played:
                # 服务端还在出声，对话仍在进行，同样续时
                last_played = played
                active_at = now
            level = rms(frame)
            last_level = level
            peak = max(peak, level)
            if level >= threshold:
                last_speech = now
                active_at = now
                if not speaking:
                    speaking = True
                    logger.info("检测到说话：RMS %.0f（阈值 %.0f）", level, threshold)
            else:
                if speaking and now - last_speech >= SPEECH_HOLD_SEC:
                    speaking = False
                if now - active_at > silence_sec:
                    logger.info("%.0fs 无人说话也无播报，结束会话", silence_sec)
                    return
            if now - last_report >= LEVEL_LOG_SEC:
                logger.info(
                    "上行 %d 帧 / %.1fs：峰值 RMS %.0f，当前 RMS %.0f；下行已播 %.1fs",
                    sent,
                    now - last_report,
                    peak,
                    last_level,
                    self._played_bytes / (PLAYBACK_RATE * 2),
                )
                peak = 0.0
                last_report = now

    async def _playback(self, player: asyncio.subprocess.Process) -> None:
        """消费下行队列写入播放器 stdin。"""
        assert player.stdin is not None
        queue = self._audio_queue
        assert queue is not None
        while self._running:
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            try:
                player.stdin.write(scale_pcm16(chunk, self._settings.out_volume))
                await player.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise RuntimeError(
                    "播放器已退出：检查 AUDIO_OUT_DEVICE 是否可用（aplay -l）"
                ) from exc
            self._played_bytes += len(chunk)
