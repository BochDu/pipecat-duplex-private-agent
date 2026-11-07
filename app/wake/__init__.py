"""唤醒词来源：sherpa-onnx 关键词检测。

待机期由监听方独占麦克风；命中后立刻交还，会话侧重新打开设备。
"""

from app.wake.base import WakeListener
from app.wake.factory import build_listener

__all__ = [
    "WakeListener",
    "build_listener",
]
