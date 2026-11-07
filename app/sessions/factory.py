"""会话模板登记表：注册表 `pipeline` 字段 → 具体模板。

路由只按名字取模板，不再判断链路字符串；新增一条链路在这里登记一次即可。
模板统一契约：`run()` 跑到会话结束，`stop()` 释放资源。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from app.asr import build_asr
from app.config import Settings
from app.events import SessionEvent
from app.providers.base import BaseProvider
from app.sessions.llm.pipeline import LLMPipeline
from app.sessions.s2s.pipeline import S2SPipeline
from app.tools import ToolRegistry
from app.tts import build_tts


class SessionPipeline(Protocol):
    """路由只要求这两个方法，具体模板自行管理内部任务。"""

    async def run(self) -> None: ...

    async def stop(self) -> None: ...


Builder = Callable[..., SessionPipeline]

#: 建连后服务端自己会开口的模板：不需要唤醒提示音，也省掉声卡切换等待
SELF_GREETING = frozenset({"sessions/s2s"})


def _build_s2s(
    *,
    provider: BaseProvider,
    settings: Settings,
    tools: ToolRegistry,
    on_event: Callable[[SessionEvent], None],
    on_audio: Callable[[bytes], None] | None,
) -> SessionPipeline:
    return S2SPipeline(
        provider=provider,
        settings=settings,
        on_event=on_event,
        on_audio=on_audio,
    )


def _build_llm(
    *,
    provider: BaseProvider,
    settings: Settings,
    tools: ToolRegistry,
    on_event: Callable[[SessionEvent], None],
    on_audio: Callable[[bytes], None] | None,
) -> SessionPipeline:
    return LLMPipeline(
        provider=provider,
        settings=settings,
        on_event=on_event,
        asr=build_asr(settings.get("ASR_PROVIDER", "volcano") or "volcano", settings),
        tts=build_tts(
            settings.get("TTS_PROVIDER", "null") or "null",
            settings.get("TTS_VOICE", "") or "",
            settings.out_volume,
            settings.out_device,
        ),
        tools=tools,
    )


BUILDERS: dict[str, Builder] = {
    "sessions/s2s": _build_s2s,
    "sessions/llm": _build_llm,
}


def build_pipeline(
    name: str,
    *,
    provider: BaseProvider,
    settings: Settings,
    tools: ToolRegistry,
    on_event: Callable[[SessionEvent], None],
    on_audio: Callable[[bytes], None] | None = None,
) -> SessionPipeline:
    """按注册表里的模板名构造会话；名字写错立刻报出可选值。"""
    builder = BUILDERS.get(name)
    if builder is None:
        raise ValueError(
            f"未登记的会话模板：{name!r}（可选：{', '.join(sorted(BUILDERS))}）"
        )
    return builder(
        provider=provider,
        settings=settings,
        tools=tools,
        on_event=on_event,
        on_audio=on_audio,
    )


def speaks_first(name: str) -> bool:
    """该模板建连后服务端会先开口，唤醒时不必再播提示音。"""
    return name in SELF_GREETING
