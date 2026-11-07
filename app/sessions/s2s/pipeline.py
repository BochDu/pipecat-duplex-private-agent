"""端到端实时语音 Pipeline 模板。

只负责通用编排：采集 → Provider → 播报、打断停播、静默超时与资源释放。
厂商差异全部在 `app/providers/<provider>/`，本模板不出现品牌代码。

音频路径（不经 pipecat，子进程直通）：
- 上行：arecord 16 kHz 帧流 → `provider.send_audio_frame`（实时节奏由采集端保证）
- 下行：Provider `emit_audio` → `feed_audio` 入队 → aplay 播放 24 kHz
- 打断：上行持续 `BARGE_MIN_SEC` 过 `S2S_BARGE_IN_RMS`，或 Provider 报「用户开口」，
  立即清空待播队列、结束 aplay（管道与 ALSA 缓冲里的声音一并丢弃）、重建播放器，
  同时让 Provider 发 `response.cancel` 取消服务端这一轮回答。
  同一时刻只允许一个打断在跑：`BARGE_COOLDOWN_SEC` 内的重复请求直接丢弃，
  重建期间队列只积压、不消费，新音频不会丢
- 结束：无人说话且无播报超过 `SILENCE_TIMEOUT_SEC`，或 Provider 断开，或外部 `stop()`

设备：`AUDIO_IN_DEVICE` / `AUDIO_OUT_DEVICE` 分别指定，缺省退回 `AUDIO_DEVICE`。
播放走 aplay（与 `preflight/audio-check.sh` 同一条路径），设备名写错时能直接报出来。

约束：aplay 的管道与 ALSA 缓冲合计约 1.5 s，只清队列无法停声，必须结束进程；
扬声器回采电平低于打断阈值才不会自己打断自己（`S2S_BARGE_IN_RMS` 高于说话阈值）。
自打断先降回采与音量，其次抬阈值；不要加长 `BARGE_MIN_SEC`，那会直接增加停声延迟。
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

BARGE_MIN_SEC = 0.25  # 人声要连续这么久才算打断：压掉吸气、爆音、回采残响
BARGE_COOLDOWN_SEC = 0.8  # 两次打断的最小间隔，同一句话不反复取消
PLAY_TAIL_SEC = 1.5  # 管道与 ALSA 缓冲的余量：刚写完播放器仍算「在播」
PLAYER_SETTLE_SEC = 0.15  # 首播时等 aplay 打开声卡
PLAYER_REOPEN_SEC = 0.05  # 打断后重建只做最短确认，换来起播更快


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
        self._player: asyncio.subprocess.Process | None = None
        self._out_device = "default"
        self._respawn = False  # 打断后由播放循环重建 aplay
        self._epoch = 0  # 播放代次：打断即 +1，旧代次音频作废
        self._last_write_at = float("-inf")
        self._interrupted_at = float("-inf")
        self._cancel_tasks: set[asyncio.Task] = set()
        self._barge_in = self._barge_enabled()
        self._barge_rms = self._barge_rms_value()

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
        self._out_device = out_device
        self._player = await self._spawn_player(out_device, PLAYER_SETTLE_SEC)
        await self._prime_player(self._player)
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
                asyncio.create_task(self._playback()),
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
            player, self._player = self._player, None
            await self._close_player(player)
            logger.info(
                "S2S 会话结束（下行共播放 %.1fs）",
                self._played_bytes / (PLAYBACK_RATE * 2),
            )

    async def stop(self) -> None:
        self._running = False
        player, self._player = self._player, None
        await self._close_player(player)

    def interrupt(self, reason: str) -> bool:
        """立刻停声并取消服务端这一轮回答；返回是否真的执行了打断。

        同步方法：采集循环与 Provider 下行都在同一个事件循环里调用。
        停声必须结束 aplay——队列之外，管道与 ALSA 缓冲里还有约 1.5 s 音频。
        """
        if not self._running:
            return False
        now = time.monotonic()
        if now - self._interrupted_at < BARGE_COOLDOWN_SEC:
            logger.info(
                "打断被冷却拦截（%s）：距上次 %.2fs < %.2fs",
                reason,
                now - self._interrupted_at,
                BARGE_COOLDOWN_SEC,
            )
            return False
        if not self._is_playing():
            logger.debug("没有在播音频，不需要打断（%s）", reason)
            return False

        dropped = self._drop_pending()
        self._epoch += 1
        self._respawn = True
        self._last_write_at = float("-inf")
        self._interrupted_at = now
        player = self._player
        if player is not None and player.returncode is None:
            try:
                player.terminate()
            except ProcessLookupError:
                pass
        logger.info(
            "打断（%s）：丢弃 %.1fs 待播音频，取消服务端回答，response_id=%s",
            reason,
            dropped / (PLAYBACK_RATE * 2),
            self._response_id(),
        )
        self._request_cancel()
        return True

    def _response_id(self) -> str:
        """当前下行回答的 id，仅用于日志定位；Provider 不提供时留空。"""
        return getattr(self._provider, "current_response_id", "") or "(空)"

    def _is_playing(self) -> bool:
        """队列里还有待播，或刚写过播放器（管道与 ALSA 缓冲里仍有声音）。"""
        queue = self._audio_queue
        if queue is not None and not queue.empty():
            return True
        return time.monotonic() - self._last_write_at < PLAY_TAIL_SEC

    def _drop_pending(self) -> int:
        """清空待播队列，返回丢弃的字节数。"""
        queue = self._audio_queue
        if queue is None:
            return 0
        dropped = 0
        while True:
            try:
                dropped += len(queue.get_nowait())
            except asyncio.QueueEmpty:
                return dropped

    def _request_cancel(self) -> None:
        """让 Provider 取消服务端回答；不阻塞采集循环，失败只记日志。"""
        cancel = getattr(self._provider, "cancel_response", None)
        if cancel is None:
            return
        task = asyncio.create_task(cancel())
        self._cancel_tasks.add(task)
        task.add_done_callback(self._cancel_tasks.discard)

    def _barge_enabled(self) -> bool:
        """本地起音打断开关，缺省开。"""
        raw = (self._settings.get("S2S_BARGE_IN", "") or "1").strip().lower()
        return raw not in {"0", "false", "no", "off"}

    def _barge_rms_value(self) -> float:
        """打断阈值，缺省高于说话阈值：避开扬声器回采。"""
        raw = (self._settings.get("S2S_BARGE_IN_RMS", "") or "").strip()
        try:
            return float(raw) if raw else 1200.0
        except ValueError:
            return 1200.0

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

    async def _spawn_player(
        self, device: str, settle: float = PLAYER_SETTLE_SEC
    ) -> asyncio.subprocess.Process:
        """起 aplay 接收裸 PCM；设备写错会立刻退出而非静默失败。"""
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
        await asyncio.sleep(settle)
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

    async def _close_player(
        self, player: asyncio.subprocess.Process | None
    ) -> None:
        """关闭播放器：先收尾 stdin，再 terminate，卡住就 kill，确保不留僵尸。"""
        if player is None:
            return
        try:
            if player.stdin is not None and not player.stdin.is_closing():
                player.stdin.close()
        except Exception:  # noqa: BLE001 - 关闭期不阻断
            logger.debug("播放器 stdin 关闭异常", exc_info=True)
        if await self._wait_player(player, 2):
            return
        try:
            player.terminate()
        except ProcessLookupError:
            return
        except Exception:  # noqa: BLE001
            logger.debug("播放器 terminate 异常", exc_info=True)
            return
        if await self._wait_player(player, 2):
            return
        try:
            player.kill()
        except ProcessLookupError:
            return
        except Exception:  # noqa: BLE001
            logger.debug("播放器 kill 异常", exc_info=True)
            return
        if not await self._wait_player(player, 2):
            logger.warning("播放器进程未能回收（pid %s）", player.pid)

    async def _wait_player(
        self, player: asyncio.subprocess.Process, timeout: float
    ) -> bool:
        """等进程退出；超时返回 False，交给调用方升级手段。"""
        try:
            await asyncio.wait_for(player.wait(), timeout=timeout)
            return True
        except Exception:  # noqa: BLE001 - 超时或等待异常都算没退出
            return False

    async def _reopen_player(self) -> asyncio.subprocess.Process:
        """打断后重建 aplay：旧进程结束即丢掉管道与 ALSA 里的残留音频。

        新进程交给调用方接管，避免与会话结束时的释放并发。
        """
        old, self._player = self._player, None
        await self._close_player(old)
        return await self._spawn_player(self._out_device, PLAYER_REOPEN_SEC)

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
        voice_since: float | None = None
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
            # 本地起音打断：人声要持续过 BARGE_MIN_SEC 才算，压掉吸气与瞬时噪声
            if self._barge_in and level >= self._barge_rms and self._is_playing():
                if voice_since is None:
                    voice_since = now
                elif now - voice_since >= BARGE_MIN_SEC:
                    voice_since = None
                    self.interrupt("本地起音")
            else:
                voice_since = None
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

    async def _playback(self) -> None:
        """消费下行队列写入播放器 stdin；被打断时先重建播放器再继续。"""
        queue = self._audio_queue
        assert queue is not None
        while self._running:
            if self._respawn:
                self._respawn = False
                player = await self._reopen_player()
                if not self._running:  # 重建期间会话结束：别把新进程漏掉
                    await self._close_player(player)
                    return
                self._player = player
                continue
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue
            player = self._player
            if player is None or player.stdin is None:
                continue
            epoch = self._epoch
            try:
                player.stdin.write(scale_pcm16(chunk, self._settings.out_volume))
                await player.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                if self._epoch != epoch or self._respawn:
                    continue  # 打断终止了旧进程，重建交给循环开头
                raise RuntimeError(
                    "播放器已退出：检查 AUDIO_OUT_DEVICE 是否可用（aplay -l）"
                ) from exc
            if self._epoch == epoch:
                self._played_bytes += len(chunk)
                self._last_write_at = time.monotonic()
