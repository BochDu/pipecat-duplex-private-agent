"""全局唤醒路由与会话生命周期。

- 启动时加载注册表；唤醒命中后按 `link` 选模板、按 `provider` 懒加载实现、
  按 `config_group` 取凭据。
- 同一时刻只有一条会话；切换严格「先停后启」。
- 会话期不再查注册表，Provider 在会话创建时绑定、整场不变。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from enum import Enum

from app.audio import play_chime
from app.config import WakewordEntry
from app.context import AppContext
from app.events import SessionEvent
from app.providers.base import BaseProvider, ProviderError
from app.sessions.factory import build_pipeline, speaks_first
from app.wake import WakeListener

logger = logging.getLogger(__name__)

CHIME_SETTLE_SEC = 0.2  # 提示音结束后等声卡切回采集


class SessionState(Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    IN_SESSION = "in_session"
    STOPPING = "stopping"


__all__ = ["SessionState", "WakeListener", "WakeRouter"]


class WakeRouter:
    def __init__(self, context: AppContext) -> None:
        self._ctx = context
        self._state = SessionState.IDLE
        self._provider: BaseProvider | None = None
        self._pipeline = None
        self._session_id = ""
        self._lock = asyncio.Lock()

    @property
    def state(self) -> SessionState:
        return self._state

    async def route(self, word: str) -> bool:
        """一次唤醒：解析注册表 → 停旧会话 → 建新会话 → 运行到结束。"""
        entry = self._ctx.registry.resolve(word)
        if entry is None:
            logger.info("未登记的唤醒词：%r", word)
            return False

        async with self._lock:
            await self._release()
            self._state = SessionState.CONNECTING
            try:
                provider = self._load_provider(entry)
                self._provider = provider
                self._pipeline = self._build_pipeline(entry, provider)
                provider.register_tools(self._ctx.tools)
                await provider.start()
            except Exception as exc:  # noqa: BLE001 - 建连失败统一回退
                logger.warning("建连失败（%s）：%s", entry.provider, exc, exc_info=True)
                await self._release()
                return False

            self._session_id = f"{entry.provider}:{entry.word}"
            self._state = SessionState.IN_SESSION
            logger.info("会话就绪：%s（%s / %s）", entry.word, entry.link, entry.provider)

        try:
            await self._pipeline.run()
        except NotImplementedError as exc:
            logger.warning("会话编排未实现：%s", exc)
        except Exception as exc:  # noqa: BLE001 - 会话异常不让进程崩
            logger.warning("会话异常：%s", exc, exc_info=True)
        finally:
            async with self._lock:
                await self._release()
        return True

    async def stop(self) -> None:
        async with self._lock:
            await self._release()

    async def serve(self, listener: WakeListener) -> None:
        async for word in listener.listen():
            entry = self._ctx.registry.resolve(word)
            if entry is None:
                logger.info("未登记的唤醒词：%r", word)
                continue
            # 服务端会先开口的模板不需要提示音，三段式才需要
            pipeline = self._ctx.registry.pipeline_for(entry.provider)
            if not speaks_first(pipeline):
                await self._chime()
                # 同一块 USB 声卡从播放切回采集需要一点时间
                await asyncio.sleep(CHIME_SETTLE_SEC)
            await self.route(word)

    async def _chime(self) -> None:
        """唤醒即响：建连前先给反馈，避免用户以为没反应。"""
        settings = self._ctx.settings
        if not settings.flag("WAKE_CHIME"):
            return
        device = settings.out_device
        voice = settings.get("TTS_VOICE", "") or ""
        if not await play_chime(device, settings.out_volume, voice):
            logger.warning(
                "唤醒提示音没播出来（设备 %s）：检查 AUDIO_OUT_DEVICE 与 amixer 音量",
                device,
            )

    def _load_provider(self, entry: WakewordEntry) -> BaseProvider:
        module = importlib.import_module(f"app.providers.{entry.provider}.provider")
        create = getattr(module, "create_provider", None)
        if create is None:
            raise ProviderError(f"Provider {entry.provider!r} 缺少 create_provider")
        return create(
            entry=entry,
            settings=self._ctx.settings,
            tools=self._ctx.tools,
            on_event=self._record,
            on_audio=self._on_audio,
            on_interrupt=self._on_interrupt,
        )

    def _build_pipeline(self, entry: WakewordEntry, provider: BaseProvider):
        """按注册表声明的模板名构造，不再按 `link` 分支。"""
        return build_pipeline(
            self._ctx.registry.pipeline_for(entry.provider),
            provider=provider,
            settings=self._ctx.settings,
            tools=self._ctx.tools,
            on_event=self._record,
            on_audio=self._on_audio,
        )

    async def _release(self) -> None:
        """释放当前会话（调用方需持有锁或确保独占）；任一步失败都不阻断其余清理。"""
        if self._provider is None:
            self._state = SessionState.IDLE
            return
        self._state = SessionState.STOPPING
        try:
            if self._pipeline is not None:
                try:
                    await self._pipeline.stop()
                except Exception:  # noqa: BLE001 - 继续释放 Provider
                    logger.exception("会话编排停止失败")
            try:
                await self._provider.stop()
            except Exception:  # noqa: BLE001 - 仍需回到待机
                logger.exception("Provider 停止失败")
        finally:
            self._provider = None
            self._pipeline = None
            if self._session_id:
                try:
                    self._ctx.tools.revoke(self._session_id)
                except Exception:  # noqa: BLE001 - 授权回收失败不阻断回待机
                    logger.exception("工具授权回收失败")
            self._session_id = ""
            self._state = SessionState.IDLE

    def _on_audio(self, pcm: bytes) -> None:
        if self._pipeline is not None and hasattr(self._pipeline, "feed_audio"):
            self._pipeline.feed_audio(pcm)

    def _on_interrupt(self, reason: str) -> None:
        """Provider 判定用户开口：交给会话侧停播（只有 S2S 支持）。"""
        if self._pipeline is not None and hasattr(self._pipeline, "interrupt"):
            self._pipeline.interrupt(reason)

    def _record(self, event: SessionEvent) -> None:
        self._ctx.record(event)
