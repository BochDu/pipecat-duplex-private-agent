"""语音合成抽象：一个项目一套 TTS，`.env` 的 `TTS_PROVIDER` 全局单选。

契约与厂商实现分离；当前用 edge-tts（云、免费、无需凭据），文本会发往
该服务，须在 `OUTBOUND_ALLOWLIST` 与隐私说明里登记。新增厂商四步：
建子包 → factory 加分发 → `.env.example` 补 `TTS_<品牌>_*` → 建
`preflight/tts_<品牌>/` 过预检。

多唤醒词只映射多 LLM（`config/providers.toml` 路由），不做唤醒词级覆盖。
"""

from app.tts.base import TTS
from app.tts.factory import build_tts

__all__ = ["TTS", "build_tts"]
