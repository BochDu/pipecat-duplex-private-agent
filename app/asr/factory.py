"""按 `ASR_PROVIDER` 构造 ASR；未实现的厂商在这里集中报错。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.asr.base import ASR

if TYPE_CHECKING:  # pragma: no cover
    from app.config import Settings


def build_asr(name: str, settings: "Settings | None" = None) -> ASR:
    if name == "volcano":
        if settings is None:
            raise ValueError("volcano ASR 需要 settings（读取 ASR_VOLCANO_* 配置组）")
        from app.asr.volcano import VolcanoASR

        return VolcanoASR(settings)
    raise ValueError(f"未实现的 ASR：{name!r}（厂商实现放 app/asr/<厂商>/ 并在此登记）")
