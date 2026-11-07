"""三段式 Pipeline：ASR → LLM → TTS。

本模板负责本地听与说、云端想：ASR 断句后整句送 LLM，流式回复交 TTS 播报；
消息数组在会话内累积，即连续对话。厂商差异在 Provider。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from uuid import uuid4

from app.asr import ASR
from app.config import Settings
from app.events import SessionEvent
from app.providers.base import BaseProvider
from app.tools import ToolRegistry
from app.tts import TTS

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM = "你是一个简洁的私人语音助手，用口语、短句回答。"


class LLMPipeline:
    link = "llm"

    def __init__(
        self,
        *,
        provider: BaseProvider,
        settings: Settings,
        on_event: Callable[[SessionEvent], None],
        asr: ASR,
        tts: TTS,
        tools: ToolRegistry,
    ) -> None:
        self._provider = provider
        self._settings = settings
        self._on_event = on_event
        self._asr = asr
        self._tts = tts
        self._tools = tools
        self._running = False
        self._session_id = ""

    async def run(self) -> None:
        system = self._settings.get("LLM_SYSTEM_PROMPT", DEFAULT_SYSTEM) or DEFAULT_SYSTEM
        messages: list[dict] = [{"role": "system", "content": system}]
        self._session_id = f"llm:{uuid4().hex[:8]}"
        self._running = True
        logger.info("三段式会话开始：%s", self._session_id)
        try:
            greeting = (self._settings.get("LLM_GREETING", "") or "").strip()
            if greeting:
                logger.info("唤醒回应：%s", greeting)
                await self._tts.speak(greeting)
            while self._running:
                heard = time.monotonic()
                user = await self._asr.transcribe()
                if not user:
                    break
                heard_at = time.monotonic()
                self._emit("user", user)
                messages.append({"role": "user", "content": user})
                answer = await self._turn(messages)
                answered_at = time.monotonic()
                if answer:
                    await self._tts.speak(answer)
                    spoke_at = time.monotonic()
                    logger.info(
                        "时延：录音+识别 %.0f ms，思考 %.0f ms，播放全程 %.0f ms",
                        (heard_at - heard) * 1000,
                        (answered_at - heard_at) * 1000,
                        (spoke_at - answered_at) * 1000,
                    )
        finally:
            self._running = False
            logger.info("三段式会话结束：%s", self._session_id)

    async def stop(self) -> None:
        self._running = False
        await self._tts.stop()

    async def _turn(self, messages: list[dict]) -> str:
        """一轮问答；遇到工具调用则执行后继续，直到拿到最终文本。"""
        while self._running:
            buffer: list[str] = []
            tool_calls: list[dict] | None = None
            async for item in self._provider.chat(messages):
                if item["type"] == "text":
                    buffer.append(item["delta"])  # 先攒着，一轮只落一行
                elif item["type"] == "tool_calls":
                    tool_calls = item["calls"]

            text = "".join(buffer)
            if text:
                self._emit("assistant", text)
            if not tool_calls:
                messages.append({"role": "assistant", "content": text})
                return text

            messages.append(
                {
                    "role": "assistant",
                    "content": text,
                    "tool_calls": [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": call["arguments"],
                            },
                        }
                        for call in tool_calls
                    ],
                }
            )
            for call in tool_calls:
                try:
                    args = json.loads(call["arguments"] or "{}")
                except ValueError:
                    args = {}
                result = await self._tools.call(
                    call["name"],
                    args,
                    session_id=self._session_id,
                    link="llm",
                    call_id=call["id"],
                )
                messages.append(
                    {"role": "tool", "tool_call_id": call["id"], "content": result}
                )
        return ""

    def _emit(self, role: str, text: str) -> None:
        self._on_event(
            SessionEvent(
                session_id=self._session_id,
                link="llm",
                role=role,
                text=text,
            )
        )
