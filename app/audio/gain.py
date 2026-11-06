"""PCM16 音量缩放：0~100 的百分比，播放前统一处理。"""

from __future__ import annotations

import sys
from array import array

PEAK = 32767
FLOOR = -32768
MAX_BOOST_DB = 24.0
HOLD_RATIO = 0.7  # 包络里保持满增益的部分，其余线性回落


def scale_pcm16(data: bytes, volume: int = 100) -> bytes:
    """按百分比缩放 S16LE PCM；0 静音，100 原样，>100 放大并削波保护。"""
    if not data or volume == 100:
        return data
    usable = len(data) // 2 * 2
    if volume <= 0:
        return bytes(usable)
    samples = array("h")
    samples.frombytes(data[:usable])
    if sys.byteorder == "big":
        samples.byteswap()
    gain = volume / 100.0
    scaled = array(
        "h", (max(FLOOR, min(PEAK, int(value * gain))) for value in samples)
    )
    if sys.byteorder == "big":
        scaled.byteswap()
    return scaled.tobytes()


def db_to_gain(db: float) -> float:
    """分贝 → 线性倍数。"""
    return 10.0 ** (db / 20.0)


class OnsetBoost:
    """段首补偿：开头保持 boost，再在剩下的份额里回落，总量为 seconds。

    前 `HOLD_RATIO` 的时间维持满增益，覆盖偏弱的头几个字；余下时间线性
    落回 1.0，回落压在第一个词尾，避免整句都在渐弱。
    """

    def __init__(
        self, boost_db: float = 0.0, seconds: float = 0.6, rate: int = 24000
    ) -> None:
        self._gain = db_to_gain(min(MAX_BOOST_DB, max(0.0, boost_db)))
        self._span = int(rate * max(0.0, seconds))
        self._done = 0
        self._envelope: list[float] = []
        if self._gain <= 1.0 or self._span <= 0:
            self._gain = 1.0
            self._span = 0
        else:
            span = self._span
            hold = max(1, int(span * HOLD_RATIO))
            release = span - hold
            if release <= 0:
                self._envelope = [self._gain] * span
            else:
                self._envelope = [self._gain] * hold + [
                    self._gain + (1.0 - self._gain) * index / release
                    for index in range(release)
                ]

    @property
    def enabled(self) -> bool:
        return self._span > 0

    def reset(self) -> None:
        """标记新一段开始，下个 `apply()` 从头算包络。"""
        self._done = 0

    def finish(self) -> None:
        """立即结束当前包络；之后 `apply()` 原样返回。"""
        self._done = self._span

    def apply(self, data: bytes) -> bytes:
        """乘包络并做削波保护；跨块累计，超出开头 seconds 后原样返回。"""
        if not self.enabled or not data or self._done >= self._span:
            return data
        usable = len(data) // 2 * 2
        samples = array("h")
        samples.frombytes(data[:usable])
        if sys.byteorder == "big":
            samples.byteswap()
        span = self._span
        envelope = self._envelope
        done = self._done
        out = array("h")
        for value in samples:
            if done < span:
                gain = envelope[done]
                done += 1
            else:
                gain = 1.0
            out.append(max(FLOOR, min(PEAK, int(value * gain))))
        self._done = done
        if sys.byteorder == "big":
            out.byteswap()
        return out.tobytes() + data[usable:]
