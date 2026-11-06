"""语音识别抽象：一个项目一套 ASR，`ASR_PROVIDER` 全局单选。

契约与厂商实现分离：本包只定接口与分发，厂商私有代码（断句、上传、重连）
全部关在自己的子包里。新增厂商四步：建子包 → factory 加分发 →
`config/defaults.env` 补 `ASR_<品牌>_*` → 建 `preflight/asr_<品牌>/` 过预检。

多唤醒词只映射多 LLM（`config/providers.toml` 路由），不做唤醒词级覆盖。
"""

from app.asr.base import ASR
from app.asr.factory import build_asr

__all__ = ["ASR", "build_asr"]
