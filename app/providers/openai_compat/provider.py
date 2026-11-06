"""OpenAI 兼容 LLM 适配层（DeepSeek / GPT 共用）。

只负责文本侧：流式 chat completions 与 `tool_calls`。本地听与说在
`app/sessions/llm/` 模板，本层不碰音频。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from app.config import Settings, WakewordEntry
from app.events import SessionEvent
from app.providers.base import BaseProvider, ProviderError
from app.tools import ToolRegistry

logger = logging.getLogger(__name__)


class OpenAICompatProvider(BaseProvider):
    link = "llm"

    def __init__(
        self,
        *,
        config: dict[str, str],
        tools: ToolRegistry,
        on_event: Callable[[SessionEvent], None],
        on_audio: Callable[[bytes], None] | None = None,
    ) -> None:
        super().__init__(config=config, tools=tools, on_event=on_event, on_audio=on_audio)
        self._client = None
        self._running = False
        self._tool_schemas: list[dict[str, Any]] = []

    async def start(self) -> None:
        missing = [key for key in ("API_KEY", "BASE_URL", "MODEL") if not self.config.get(key)]
        if missing:
            raise ProviderError(f"配置组缺少：{', '.join(missing)}")
        if not self.config["API_KEY"].isascii():
            # 头字段只能是 ASCII，中文占位符在这里会变成晦涩的编码错误
            raise ProviderError("API_KEY 含非 ASCII 字符：检查 .env 是否把占位符或说明一起粘进去了")
        self._running = True

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._running = False

    async def send_audio_frame(self, frame: bytes) -> None:
        # 文本链路，音频由 sessions/llm 的 ASR 环节消费。
        return None

    def register_tools(self, tools: ToolRegistry) -> None:
        self._tool_schemas = tools.schemas()

    def is_running(self) -> bool:
        return self._running

    async def chat(self, messages: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
        """流式产出：`{"type": "text", "delta": str}` 或 `{"type": "tool_calls", "calls": [...]}`。"""
        try:
            import httpx
        except ModuleNotFoundError as exc:
            raise ProviderError("未安装 httpx：pip install httpx") from exc

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=60.0)

        url = self.config["BASE_URL"].rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.config['API_KEY']}"}
        payload: dict[str, Any] = {
            "model": self.config["MODEL"],
            "messages": messages,
            "stream": True,
        }
        if self._tool_schemas:
            payload["tools"] = self._tool_schemas
            payload["tool_choice"] = "auto"

        calls: dict[int, dict[str, str]] = {}
        async with self._client.stream("POST", url, headers=headers, json=payload) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise ProviderError(f"LLM {resp.status_code}：{body[:200]!r}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                text = delta.get("content")
                if text:
                    yield {"type": "text", "delta": text}
                for tc in delta.get("tool_calls") or []:
                    index = tc.get("index", 0)
                    slot = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    function = tc.get("function") or {}
                    if function.get("name"):
                        slot["name"] = function["name"]
                    if function.get("arguments"):
                        slot["arguments"] += function["arguments"]

        if calls:
            yield {
                "type": "tool_calls",
                "calls": [
                    {
                        "id": slot["id"] or f"call_{index}",
                        "name": slot["name"],
                        "arguments": slot["arguments"] or "{}",
                    }
                    for index, slot in calls.items()
                ],
            }


def create_provider(
    *,
    entry: WakewordEntry,
    settings: Settings,
    tools: ToolRegistry,
    on_event: Callable[[SessionEvent], None],
    on_audio: Callable[[bytes], None] | None = None,
) -> BaseProvider:
    return OpenAICompatProvider(
        config=settings.group(entry.config_group),
        tools=tools,
        on_event=on_event,
        on_audio=on_audio,
    )
