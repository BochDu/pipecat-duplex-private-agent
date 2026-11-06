"""统一会话事件：两条链路都落到同一结构，storage 只认它。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class SessionEvent:
    """一次会话中的一条事件。

    `link` 标记来源链路（`s2s` / `llm`），共享层据此归档，不生成厂商字段。
    """

    session_id: str
    link: str
    role: str
    text: str = ""
    audio_ms: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
