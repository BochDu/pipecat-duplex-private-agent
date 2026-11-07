"""唤醒提示音：优先播一句预生成的人声，没有缓存时回落到三声音阶。"""

from __future__ import annotations

import asyncio
import logging
import math
from array import array
from pathlib import Path

from app.audio.gain import scale_pcm16
from app.audio.player import PREROLL_LONG_MS, play_pcm

logger = logging.getLogger(__name__)

# (频率 Hz, 时长 s)；频率 0 表示静音间隔
NOTES = ((880.0, 0.10), (0.0, 0.04), (1174.7, 0.14), (0.0, 0.04), (1568.0, 0.18))
AMPLITUDE = 22000
FADE_SEC = 0.01
SPOKEN_TEXT = "我在，请说"  # 人声提示：够长才不会被设备吞掉，也最像「可以说了」
CACHE_DIR = Path("data")
SPOKEN_VOLUME = 150  # 提示音比正文再响一点，唤醒瞬间环境噪声大


def chime_pcm(rate: int = 24000) -> bytes:
    """返回 S16LE 单声道 PCM，采样率与播放端一致。"""
    fade = max(1, int(rate * FADE_SEC))
    samples = array("h")
    for freq, seconds in NOTES:
        total = int(rate * seconds)
        if freq <= 0:
            samples.extend(array("h", [0]) * total)
            continue
        for index in range(total):
            envelope = 1.0
            if index < fade:
                envelope = index / fade
            elif index > total - fade:
                envelope = (total - index) / fade
            value = AMPLITUDE * envelope * math.sin(2 * math.pi * freq * index / rate)
            samples.append(int(value))
    return samples.tobytes()


def spoken_path(voice: str) -> Path:
    """人声提示的缓存文件（裸 PCM，播放路径与 S2S 一致）。"""
    tag = "".join(ch for ch in voice if ch.isalnum()) or "default"
    return CACHE_DIR / f"chime-{tag}.pcm"


def _write_pcm(path: Path, pcm: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pcm)


async def ensure_spoken_chime(voice: str, rate: int = 24000) -> Path | None:
    """预生成人声提示（前置一段静音）；已缓存则直接返回。"""
    path = spoken_path(voice)
    if path.exists():
        return path
    try:
        import edge_tts
    except ModuleNotFoundError:
        return None
    try:
        buffer = bytearray()
        async for chunk in edge_tts.Communicate(SPOKEN_TEXT, voice).stream():
            if chunk["type"] == "audio":
                buffer.extend(chunk["data"])
        if not buffer:
            return None
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-ac",
            "1",
            "-ar",
            str(rate),
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        pcm, _ = await proc.communicate(bytes(buffer))
        if proc.returncode != 0 or not pcm:
            return None
        silence = b"\x00" * (rate * 2 * PREROLL_LONG_MS // 1000)
        _write_pcm(path, silence + scale_pcm16(pcm, SPOKEN_VOLUME))
        logger.info(
            "已生成唤醒人声提示：%s（%.2f s）",
            path,
            len(silence + pcm) / 2 / rate,
        )
        return path
    except Exception:  # noqa: BLE001 - 生成失败就回落到音阶
        logger.warning("生成人声提示失败，唤醒时使用音阶", exc_info=True)
        return None


async def play_chime(
    device: str = "default", volume: int = 100, voice: str = "", rate: int = 24000
) -> bool:
    """缓存已备好就播人声，否则立刻播音阶——唤醒这一步不能有等待。"""
    if voice:
        path = spoken_path(voice)
        if path.exists():
            try:
                data = path.read_bytes()
            except OSError:
                data = b""
            if data:
                logger.info("唤醒提示：人声 %s", path.name)
                if await play_pcm(data, device, rate=rate):
                    return True
                logger.warning("人声提示播放失败，改用音阶")
    logger.info("唤醒提示：三声音阶")
    tones = scale_pcm16(chime_pcm(rate), volume)
    return await play_pcm(
        tones, device, rate=rate, prime_ms=PREROLL_LONG_MS, timeout=3
    )
