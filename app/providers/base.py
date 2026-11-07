"""统一 Provider 契约。

固定 5 个方法：`start` / `stop` / `send_audio_frame` / `register_tools` /
`is_running`，另加可选的 `cancel_response`。不做能力协商、不做运行时发现、
不做会话内切换——契约只是为了让新增模型不动路由与共用后台。
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any

from app.events import SessionEvent

if TYPE_CHECKING:  # pragma: no cover
    from app.tools import ToolRegistry


class ProviderError(RuntimeError):
    """Provider 建连或运行期错误；路由据此语音回告并退回待机。"""


class BaseProvider(abc.ABC):
    """一条链路对某一家云服务的适配层。"""

    #: `s2s`（端到端实时语音）或 `llm`（ASR → LLM → TTS）
    link: str = ""

    def __init__(
        self,
        *,
        config: dict[str, str],
        tools: "ToolRegistry",
        on_event: Callable[[SessionEvent], None],
        on_audio: Callable[[bytes], None] | None = None,
        on_interrupt: Callable[[str], None] | None = None,
    ) -> None:
        self._config = dict(config)
        self._tools = tools
        self._on_event = on_event
        self._on_audio = on_audio
        self._on_interrupt = on_interrupt

    @property
    def config(self) -> dict[str, str]:
        """该 Provider 对应的配置组，键已去掉 `<链路>_<品牌>_` 前缀。"""
        return self._config

    def emit(self, event: SessionEvent) -> None:
        self._on_event(event)

    def emit_audio(self, pcm: bytes) -> None:
        """下行音频交给会话侧播放；S2S 链路用，三段式不用。"""
        if self._on_audio is not None:
            self._on_audio(pcm)

    def notify_interrupt(self, reason: str) -> None:
        """下行判定「用户开口」时通知会话侧停播；不支持的 Provider 不调。"""
        if self._on_interrupt is not None:
            self._on_interrupt(reason)

    async def cancel_response(self) -> None:
        """取消服务端进行中的回答；默认无操作，由支持的 Provider 覆写。"""

    async def chat(
        self, messages: list[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]:
        """文本流式对话：`llm` 链路必需，`s2s` 链路不实现。

        产出 `{"type": "text", "delta": str}` 或
        `{"type": "tool_calls", "calls": [...]}`。
        """
        raise NotImplementedError(f"{type(self).__name__} 不支持 chat()")
        yield  # 保持 async generator：调用方 async for 时才报错

    @abc.abstractmethod
    async def start(self) -> None:
        """建连并进入就绪；建连期上行的音频帧由调用方丢弃。"""

    @abc.abstractmethod
    async def stop(self) -> None:
        """释放连接与音频资源，保证之后可被重新 `start`。"""

    @abc.abstractmethod
    async def send_audio_frame(self, frame: bytes) -> None:
        """送入一帧按本链路规格编码的音频。"""

    @abc.abstractmethod
    def register_tools(self, tools: "ToolRegistry") -> None:
        """把统一工具 Schema 翻译成本服务的工具定义。"""

    @abc.abstractmethod
    def is_running(self) -> bool:
        """是否处于可收发状态。"""
