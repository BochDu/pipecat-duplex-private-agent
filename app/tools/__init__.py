"""工具池：同一份 Schema 生成 S2S 工具定义与 LLM `tools`。

分级授权是内存态 `(session_id, scope)`，单次、本会话有效、不持久化。
"""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

SENSITIVITY_LEVELS = ("public", "internal", "local-only")


@dataclass
class ToolSpec:
    """一个可被模型调用的本地工具。"""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[str] | str]
    sensitivity: str = "public"


class ToolRegistry:
    """进程内唯一的工具注册表。"""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._grants: set[tuple[str, str]] = set()

    def register(self, spec: ToolSpec) -> None:
        if spec.sensitivity not in SENSITIVITY_LEVELS:
            raise ValueError(f"未知敏感级别：{spec.sensitivity!r}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return list(self._specs)

    def schemas(self) -> list[dict[str, Any]]:
        """统一 Schema；S2S 链路在 Provider 侧转成自家工具定义。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
            for spec in self._specs.values()
        ]

    def grant(self, session_id: str, scope: str) -> None:
        self._grants.add((session_id, scope))

    def revoke(self, session_id: str) -> None:
        self._grants = {item for item in self._grants if item[0] != session_id}

    def is_granted(self, session_id: str, scope: str) -> bool:
        return (session_id, scope) in self._grants

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        session_id: str,
        link: str,
        call_id: str | None = None,
    ) -> str:
        """执行工具；非 public 工具需要本会话的单次授权。"""
        spec = self._specs.get(name)
        if spec is None:
            return f"未知工具：{name}"
        if spec.sensitivity != "public" and not self.is_granted(
            session_id, spec.sensitivity
        ):
            return f"工具 {name} 需要单次授权（{spec.sensitivity}）"
        try:
            result = spec.handler(**arguments)
            if inspect.isawaitable(result):
                result = await result
            return str(result)
        except Exception as exc:  # noqa: BLE001 - 工具失败只回告，不中断会话
            logger.exception("工具执行失败：%s", name)
            return f"工具执行失败：{type(exc).__name__}"

    def audit_entry(
        self, *, call_id: str | None, session_id: str, name: str, arguments: dict
    ) -> dict[str, Any]:
        """留给审计落库的字段，storage 侧按需写入 `tool_logs`。"""
        return {
            "call_id": call_id,
            "session_id": session_id,
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        }
