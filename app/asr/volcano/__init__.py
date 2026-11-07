"""火山一句话识别适配层（麦克风采集 → 整句上传 → 文本）。

协议与 `preflight/asr_volcano/api-check.py` 一致：全双工二进制协议、
`X-Api-Key` 鉴权、`bigmodel_nostream` 端点。三段式默认用它
（`ASR_PROVIDER=volcano`），预检脚本用于回归验证。
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import math
import struct
import sys
import time
import uuid
import wave
from array import array
from pathlib import Path

from app.asr.base import ASR
from app.audio import MicRecorder
from app.config import Settings

logger = logging.getLogger(__name__)

DEFAULT_WS = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream"
DEFAULT_RESOURCE = "volc.seedasr.sauc.duration"
SAMPLE_RATE = 16000
RETRY = 2  # 没听清时再听一次，避免一句话没说好就结束会话
MAX_GAIN_DB = 12.0  # 归一化时最多放大这么多，避免把底噪抬起来
DEBUG_WAV = Path("data") / "asr-last.wav"  # 最近一次录音，回放确认用

# ---- 火山全双工协议常量 ----
FULL_CLIENT_REQUEST = 0b0001
AUDIO_ONLY = 0b0010
SERVER_ERROR = 0b1111
JSON_SER = 0b0001
RAW_SER = 0b0000
GZIP = 0b0001
FLAG_NO_SEQ = 0b0000
FLAG_WITH_SEQ = 0b0001  # POS_SEQUENCE：正序号
FLAG_LAST_SEQ = 0b0011  # NEG_WITH_SEQUENCE：末包，显式负序号
# 注意 0b0010（NEG_SEQUENCE）表示末包且不带序号字段，用它会让服务端把负数当长度


def _pack_header(msg_type: int, flags: int, serial: int, compression: int) -> bytes:
    return bytes(
        [
            (0b0001 << 4) | 0b0001,  # 协议版本 1，头长 1
            (msg_type << 4) | flags,
            (serial << 4) | compression,
            0x00,
        ]
    )


async def _recv_json(ws) -> dict:
    while True:
        head = await ws.recv()
        if isinstance(head, str):
            return json.loads(head)
        msg_type = (head[1] >> 4) & 0x0F
        flags = head[1] & 0x0F
        compression = head[2] & 0x0F
        offset = 4
        if flags & FLAG_WITH_SEQ:  # 低位为 1 才带序号字段
            offset += 4
        size = struct.unpack(">i", head[offset : offset + 4])[0]
        offset += 4
        payload = head[offset : offset + size]
        if compression == GZIP:
            payload = gzip.decompress(payload)
        if msg_type == SERVER_ERROR:
            raise RuntimeError(f"火山 ASR 服务端错误帧：{payload!r}")
        return json.loads(payload)


def save_debug_wav(pcm: bytes, path: Path) -> None:
    """把这次录到的原始音频存盘，便于回放确认录进去的是什么。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(pcm)
    except OSError:
        logger.debug("保存调试音频失败", exc_info=True)


def normalize_pcm16(pcm: bytes, target_dbfs: float = -6.0) -> bytes:
    """把整句峰值压到 target_dbfs；过高电平是识别为空的主因。

    只降电平，无法还原已经削平的波形，所以麦克风增益仍应调低。
    """
    usable = len(pcm) // 2 * 2
    if not usable:
        return pcm
    samples = array("h")
    samples.frombytes(pcm[:usable])
    if sys.byteorder == "big":
        samples.byteswap()
    peak = max((abs(value) for value in samples), default=0)
    if not peak:
        return pcm
    peak_db = 20 * math.log10(peak / 32768.0)
    gain_db = min(MAX_GAIN_DB, target_dbfs - peak_db)
    if abs(gain_db) < 0.5:
        return pcm
    gain = 10 ** (gain_db / 20.0)
    out = array(
        "h", (max(-32768, min(32767, int(value * gain))) for value in samples)
    )
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes() + pcm[usable:]


def peak_label(pcm: bytes) -> str:
    """整段峰值 dBFS，用来区分“没录到声音”和“录到了但没识别出来”。"""
    usable = len(pcm) // 2 * 2
    if not usable:
        return "无音频"
    samples = array("h")
    samples.frombytes(pcm[:usable])
    if sys.byteorder == "big":
        samples.byteswap()
    peak = max((abs(value) for value in samples), default=0)
    if not peak:
        return "-120 dBFS（静音）"
    return f"{20 * math.log10(peak / 32768.0):.1f} dBFS"


class VolcanoASR(ASR):
    """一句一次识别：麦克风静音判停录制，整句上传火山，返回文本。

    空字符串表示本轮无人说话（结束会话）。
    """

    def __init__(self, settings: Settings) -> None:
        self._api_key = settings.get("ASR_VOLCANO_API_KEY", "") or ""
        self._resource = settings.get("ASR_VOLCANO_RESOURCE_ID", "") or DEFAULT_RESOURCE
        self._url = settings.get("ASR_VOLCANO_WS_URL", "") or DEFAULT_WS
        self._device = settings.get("AUDIO_IN_DEVICE", "") or "default"
        raw_silence = (settings.get("ASR_SILENCE_MS", "") or "").strip()
        try:
            self._silence_ms = int(float(raw_silence)) if raw_silence else 1200
        except ValueError:
            self._silence_ms = 1200
        self._silence_ms = max(300, min(3000, self._silence_ms))
        # 等起音的上限，与 S2S 的静默超时共用同一个键；会话层还会用 deadline 收紧
        raw_wait = (settings.get("SILENCE_TIMEOUT_SEC", "") or "").strip()
        try:
            self._wait_sec = float(raw_wait) if raw_wait else 10.0
        except ValueError:
            self._wait_sec = 10.0
        self._wait_sec = max(3.0, min(120.0, self._wait_sec))
        # 单句上限：超过就强制判停上传，避免一句录太久
        raw_max = (settings.get("ASR_MAX_UTTERANCE_SEC", "") or "").strip()
        try:
            self._max_ms = int(float(raw_max) * 1000) if raw_max else 15000
        except ValueError:
            self._max_ms = 15000
        self._max_ms = max(3000, min(60000, self._max_ms))
        raw_threshold = (settings.get("AUDIO_IN_SILENCE_RMS", "") or "").strip()
        try:
            self._threshold = float(raw_threshold) if raw_threshold else 900.0
        except ValueError:
            self._threshold = 900.0
        self._last_payload: dict = {}
        if "replace-me" in self._api_key or not self._api_key:
            raise RuntimeError("先在 .env 填好 ASR_VOLCANO_API_KEY（ASR_PROVIDER=volcano）")

    async def transcribe(self, *, deadline: float | None = None) -> str:
        """录一句送识别；识别为空说明没听清，再听一次，仍为空才结束会话。

        `deadline` 限制等起音的额度，重试复用同一额度、不翻倍；已经录到声音后
        识别必须跑完，否则这句白说。
        """
        for attempt in range(1, RETRY + 1):
            remaining = self._remaining(deadline)
            if remaining <= 0:
                logger.info("等起音额度用尽，结束会话")
                return ""
            try:
                audio = await MicRecorder(device=self._device).record_utterance(
                    start_timeout=max(1.0, min(remaining, self._wait_sec)),
                    silence_ms=self._silence_ms,
                    max_ms=self._max_ms,
                    threshold=self._threshold,
                )
            except RuntimeError as exc:
                # 声卡刚被播放占用过会短暂报 I/O error，等一会儿再抢
                logger.warning("采集失败：%s，稍后重试", exc)
                await asyncio.sleep(0.5)
                continue
            if not audio:
                return ""  # 起音超时：确实没人说话
            seconds = len(audio) / (SAMPLE_RATE * 2)
            before = peak_label(audio)
            save_debug_wav(audio, DEBUG_WAV)
            audio = normalize_pcm16(audio)
            logger.info(
                "录到 %.2fs / %d B，峰值 %s → %s（原始存 %s）",
                seconds,
                len(audio),
                before,
                peak_label(audio),
                DEBUG_WAV,
            )
            started = time.monotonic()
            text = await self._recognize(audio)
            logger.info(
                "火山 ASR 识别：%s（识别 %.0f ms）",
                text or "(空)",
                (time.monotonic() - started) * 1000,
            )
            if not text:
                logger.info("响应内容：%s", str(self._last_payload)[:300])
            if text:
                return text
            if attempt < RETRY:
                logger.info("这次没听清，继续说（%d/%d）", attempt, RETRY)
        return ""

    def _remaining(self, deadline: float | None) -> float:
        """还能等多久起音；没有 deadline 就用配置值。"""
        if deadline is None:
            return self._wait_sec
        return deadline - time.monotonic()

    async def _recognize(self, audio: bytes) -> str:
        try:
            from websockets.asyncio.client import connect
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise RuntimeError("未安装 websockets：pip install 'websockets>=13.1'") from exc

        headers = {
            "X-Api-Key": self._api_key,
            "X-Api-Resource-Id": self._resource,
            "X-Api-Request-Id": str(uuid.uuid4()),
        }
        config = {
            "user": {"uid": str(uuid.uuid4())},
            "audio": {"format": "pcm", "rate": 16000, "bits": 16, "channel": 1},
            "request": {
                "model_name": "bigmodel",
                "enable_itn": True,
                "enable_punc": True,
                "enable_ddc": True,
                "show_utterances": True,
            },
        }
        async with connect(self._url, additional_headers=headers, open_timeout=10) as ws:
            cfg = gzip.compress(json.dumps(config).encode())
            # config 占序号 1：服务端按到达顺序自动编号，音频必须从 2 起
            await ws.send(
                _pack_header(FULL_CLIENT_REQUEST, FLAG_WITH_SEQ, JSON_SER, GZIP)
                + struct.pack(">ii", 1, len(cfg))
                + cfg
            )
            # 200 ms 一包（16 kHz × 2 B × 0.2 s）
            chunk_size = 6400
            chunks = [
                audio[i : i + chunk_size] for i in range(0, len(audio), chunk_size)
            ] or [b""]
            hdr = _pack_header(AUDIO_ONLY, FLAG_WITH_SEQ, RAW_SER, GZIP)
            hdr_last = _pack_header(AUDIO_ONLY, FLAG_LAST_SEQ, RAW_SER, GZIP)
            responses: list[dict] = []

            async def drain() -> None:
                """边发边收；只有 is_last_package 那条带最终文本。"""
                while len(responses) < 500:
                    try:
                        resp = await asyncio.wait_for(_recv_json(ws), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    except Exception:  # noqa: BLE001 - 服务端收流后正常关闭连接
                        return
                    responses.append(resp)
                    if resp.get("is_last_package"):
                        return

            reader = asyncio.create_task(drain())
            seq = 2
            for index, chunk in enumerate(chunks, start=1):
                # 末包：flags 用 NEG_WITH_SEQUENCE（0b0011）并携带负数值
                final = index == len(chunks)
                body = gzip.compress(chunk)
                head = hdr_last if final else hdr
                number = -seq if final else seq
                await ws.send(head + struct.pack(">ii", number, len(body)) + body)
                seq += 1
                await asyncio.sleep(0)
            try:
                await asyncio.wait_for(reader, timeout=20)
            except asyncio.TimeoutError:
                reader.cancel()
            except Exception:  # noqa: BLE001 - 关闭连接不算失败
                pass
            if not responses:
                raise RuntimeError("服务端未返回任何响应（连接已关闭）")
            # 优先取带文本的那条，其次取最后一条
            last = responses[-1]
            for resp in reversed(responses):
                payload = resp.get("payload_msg") or {}
                if ((payload.get("result") or {}).get("text") or "").strip():
                    last = resp
                    break
        code = last.get("code")
        if code not in (None, 0):
            # 成功响应带 code=0，error 帧才是非 0
            raise RuntimeError(f"火山 ASR 失败：code={code} {last.get('payload_msg')}")
        self._last_payload = last
        # 响应可能带外层 payload_msg，也可能就是扁平结构，两种都认
        return extract_text(last.get("payload_msg") or last)


def extract_text(payload: dict) -> str:
    """取识别文本：优先 result.text，缺失时拼 utterances 里的分句。"""
    result = payload.get("result") or {}
    text = (result.get("text") or "").strip()
    if text:
        return text
    parts = [
        (item.get("text") or "").strip()
        for item in result.get("utterances") or []
        if (item.get("text") or "").strip()
    ]
    return "".join(parts).strip()
