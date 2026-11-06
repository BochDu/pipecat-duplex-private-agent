"""唤醒词来源：终端输入（调试）、sherpa-onnx 关键词检测、Vosk（离线 ASR）。

待机期由监听方独占麦克风；命中后立刻交还，会话侧重新打开设备。
"""

from app.wake.base import WakeListener
from app.wake.factory import build_listener
from app.wake.vosk_listener import VoskWakeListener

__all__ = [
    "WakeListener",
    "VoskWakeListener",
    "build_listener",
]
