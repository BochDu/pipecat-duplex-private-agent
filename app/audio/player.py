"""aplay 播放进程：命令行参数、预热静音与收尾只在这里维护。

S2S 下行、唤醒提示音、edge-tts 都走同一套参数。预热时长按场景分三档：

- `PREROLL_MS`：会话首播，150 ms 足够
- `PREROLL_LONG_MS`：提示音与流式 TTS，起播丢头更明显，300 ms
- `PREROLL_REOPEN_MS`：打断后重开，60 ms，用最短确认换更快的起播
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

PLAYBACK_RATE = 24000  # 全部播放路径统一 24 kHz / 单声道 / S16LE
PREROLL_MS = 150
PREROLL_LONG_MS = 300
PREROLL_REOPEN_MS = 60
CLOSE_TIMEOUT = 2.0


def silence(ms: int, rate: int = PLAYBACK_RATE) -> bytes:
    """`ms` 毫秒的静音 PCM。"""
    return b"\x00" * (rate * 2 * ms // 1000)


async def spawn(
    device: str,
    *,
    rate: int = PLAYBACK_RATE,
    settle: float = 0.0,
) -> asyncio.subprocess.Process:
    """起 aplay 接收裸 PCM；`settle` 秒后仍退出就立刻报设备问题。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "aplay",
            "-q",
            "-D", device,
            "-t", "raw",
            "-f", "S16_LE",
            "-r", str(rate),
            "-c", "1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("未安装 aplay：sudo apt install -y alsa-utils") from exc
    if settle > 0:
        await asyncio.sleep(settle)
    if proc.returncode is not None:
        raise RuntimeError(
            f"播放器启动失败（退出码 {proc.returncode}）：检查播放设备 {device!r}，"
            "用 `aplay -l` 看可用设备"
        )
    return proc


async def prime(
    proc: asyncio.subprocess.Process,
    ms: int = PREROLL_MS,
    *,
    rate: int = PLAYBACK_RATE,
) -> None:
    """先喂一段静音，抵消 aplay 打开声卡后的起播丢帧；失败不阻断会话。"""
    if ms <= 0:
        return
    try:
        assert proc.stdin is not None
        proc.stdin.write(silence(ms, rate))
        await proc.stdin.drain()
    except Exception:  # noqa: BLE001 - 预热失败只影响开头几十毫秒
        logger.debug("播放器预热失败", exc_info=True)


async def close(proc: asyncio.subprocess.Process | None) -> None:
    """关闭播放器：先收尾 stdin，再 terminate，卡住就 kill，确保不留僵尸。"""
    if proc is None:
        return
    try:
        if proc.stdin is not None and not proc.stdin.is_closing():
            proc.stdin.close()
    except Exception:  # noqa: BLE001 - 关闭期不阻断
        logger.debug("播放器 stdin 关闭异常", exc_info=True)
    if await _wait(proc):
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    except Exception:  # noqa: BLE001
        logger.debug("播放器 terminate 异常", exc_info=True)
        return
    if await _wait(proc):
        return
    try:
        proc.kill()
    except ProcessLookupError:
        return
    except Exception:  # noqa: BLE001
        logger.debug("播放器 kill 异常", exc_info=True)
        return
    if not await _wait(proc):
        logger.warning("播放器进程未能回收（pid %s）", proc.pid)


async def play_pcm(
    data: bytes,
    device: str,
    *,
    rate: int = PLAYBACK_RATE,
    prime_ms: int = 0,
    timeout: float = 5.0,
) -> bool:
    """一次性播完一段裸 PCM；失败返回 False，由调用方决定回落。

    `prime_ms` 由调用方决定：缓存文件里已带静音就传 0，现场合成的音阶才需要。
    """
    if not data:
        return False
    try:
        proc = await spawn(device, rate=rate)
    except (RuntimeError, OSError):
        return False
    try:
        assert proc.stdin is not None
        if prime_ms > 0:
            proc.stdin.write(silence(prime_ms, rate))
            await proc.stdin.drain()
        proc.stdin.write(data)
        await proc.stdin.drain()
        proc.stdin.close()
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except Exception:  # noqa: BLE001 - 播放失败按未播出处理
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        return False
    return proc.returncode == 0


async def _wait(proc: asyncio.subprocess.Process, timeout: float = CLOSE_TIMEOUT) -> bool:
    """等进程退出；超时返回 False，交给调用方升级手段。"""
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
        return True
    except Exception:  # noqa: BLE001 - 超时或等待异常都算没退出
        return False
