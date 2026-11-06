#!/usr/bin/env python3
"""ASR_VOLCANO 自检：火山一句话识别（bigmodel_nostream）连通与识别。

音频来源二选一：
  默认      用 edge-tts 合成一句测试语音（mp3），喂给识别接口
  --file x  直接喂指定音频文件（wav / mp3 / ogg / pcm）

协议：火山全双工二进制协议内联实现（对应官方 sauc_python.zip 的 protocol 模块）。
"""

import argparse
import array
import asyncio
import gzip
import json
import math
import os
import struct
import sys
import time
import uuid
import wave
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
EVID = HERE / "evidence"
DEFAULT_WS = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream"
DEFAULT_RESOURCE = "volc.seedasr.sauc.duration"
TEST_TEXT = "今天天气真不错，我们出去散步吧。"

# ---- 火山全双工协议常量 ----
PROTO_VER = 0b0001
HEADER_SIZE = 0b0001
FULL_CLIENT_REQUEST = 0b0001
AUDIO_ONLY = 0b0010
FULL_SERVER_RESPONSE = 0b1001
SERVER_ERROR = 0b1111
JSON_SER = 0b0001
RAW_SER = 0b0000
GZIP = 0b0001
FLAG_NO_SEQ = 0b0000
FLAG_WITH_SEQ = 0b0001  # POS_SEQUENCE：正序号
FLAG_LAST_SEQ = 0b0011  # NEG_WITH_SEQUENCE：末包，显式负序号
# 注意 0b0010（NEG_SEQUENCE）表示末包且不带序号字段，用它会让服务端把负数当长度


def load_env():
    """config/defaults.env 打底，.env 覆盖，真实环境变量最优先。"""
    values = {}
    for path in (ROOT / "config" / "defaults.env", ROOT / ".env"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    for key, value in values.items():
        os.environ.setdefault(key, value)


def redact(value):
    text = " ".join(str(value).split())
    key = os.environ.get("ASR_VOLCANO_API_KEY", "")
    if key and key != "replace-me":
        text = text.replace(key, "***")
    return text[:300]


def save_wav(path: Path, pcm: bytes, rate: int) -> None:
    """落盘成标准 wav，便于用官方 sauc_python demo 跑同一段音频。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)


def pcm_peak_dbfs(pcm: bytes) -> float:
    """裸 PCM 的峰值电平；用来确认送出去的不是静音。"""
    usable = len(pcm) // 2 * 2
    if not usable:
        return -120.0
    samples = array.array("h")
    samples.frombytes(pcm[:usable])
    peak = max((abs(value) for value in samples), default=0)
    return 20 * math.log10(peak / 32768.0) if peak else -120.0


def http_hint(exc):
    """握手期 HTTP 状态 → (结论, 说明)；非 401/403 返回 None。"""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 403:
        resp = getattr(exc, "response", None)
        body = getattr(resp, "body", b"") or b""
        text_body = (
            body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
        )
        try:
            logid = (getattr(resp, "headers", {}) or {}).get("X-Tt-Logid", "")
        except Exception:  # noqa: BLE001 - 只为诊断
            logid = ""
        return "被拒绝：HTTP 403", redact(
            f"{text_body.strip()[:200] or '无 body'}　logid={logid or '无'}"
        )
    if status == 401:
        return "被拒绝：HTTP 401", "API Key 无效，或不属于开通该服务的账号/项目"
    return None


def pack_header(msg_type, flags, serial, compression):
    """4 字节头：版本/长度、类型/标志、(序列化 << 4) | 压缩、保留位。"""
    return bytes(
        [
            (PROTO_VER << 4) | HEADER_SIZE,
            (msg_type << 4) | flags,
            (serial << 4) | compression,
            0x00,
        ]
    )


async def recv_json(ws):
    """读一个完整服务端包并解析 JSON；自动处理二进制帧头。"""
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
            raise RuntimeError(f"服务端错误帧：{payload!r}")
        return json.loads(payload)


async def recognize(file_bytes: bytes, audio_format: str, rate: int) -> dict:
    """一句话识别：config(seq=1) → 音频 seq 2..N（末包取负）→ 边发边收。

    服务端按消息顺序自动编号并校验，config 占 1；逐包返回中间结果，
    只有 is_last_package 的那条才带文本。
    """
    import websockets

    key = os.environ["ASR_VOLCANO_API_KEY"]
    resource = os.environ.get("ASR_VOLCANO_RESOURCE_ID", "") or DEFAULT_RESOURCE
    url = os.environ.get("ASR_VOLCANO_WS_URL", "") or DEFAULT_WS

    headers = {
        "X-Api-Key": key,
        "X-Api-Resource-Id": resource,
        "X-Api-Request-Id": str(uuid.uuid4()),
    }
    config = {
        "user": {"uid": str(uuid.uuid4())},
        "audio": {"format": audio_format, "rate": rate, "bits": 16, "channel": 1},
        "request": {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "enable_ddc": True,
            "show_utterances": True,
        },
    }

    async with websockets.connect(url, additional_headers=headers, open_timeout=10) as ws:
        cfg = gzip.compress(json.dumps(config).encode())
        # config 占序号 1：服务端按到达顺序自动编号，音频必须从 2 起
        await ws.send(
            pack_header(FULL_CLIENT_REQUEST, FLAG_WITH_SEQ, JSON_SER, GZIP)
            + struct.pack(">ii", 1, len(cfg))
            + cfg
        )
        # 200 ms 一包：16 kHz × 2 B × 0.2 s = 6400 B
        chunk_size = 6400
        chunks = [
            file_bytes[i : i + chunk_size]
            for i in range(0, len(file_bytes), chunk_size)
        ] or [b""]
        hdr = pack_header(AUDIO_ONLY, FLAG_WITH_SEQ, RAW_SER, GZIP)
        hdr_last = pack_header(AUDIO_ONLY, FLAG_LAST_SEQ, RAW_SER, GZIP)
        responses: list[dict] = []

        async def drain() -> None:
            """边发边收：服务端每个音频包回一条，最后一条才是结果。"""
            while len(responses) < 500:
                try:
                    resp = await asyncio.wait_for(recv_json(ws), timeout=1.0)
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
        for resp in reversed(responses):
            payload = resp.get("payload_msg") or {}
            if ((payload.get("result") or {}).get("text") or "").strip():
                return resp
        return responses[-1]


async def make_test_audio():
    """用 edge-tts 合成测试句；返回 (音频字节, 格式)。"""
    try:
        import edge_tts
    except ModuleNotFoundError:
        raise RuntimeError(
            "默认模式需要 edge-tts 合成测试音频：pip install edge-tts，或改用 --file 指定音频"
        )
    voice = os.environ.get("TTS_VOICE", "") or "zh-CN-XiaoxiaoNeural"
    communicate = edge_tts.Communicate(TEST_TEXT, voice)
    buffer = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buffer.extend(chunk["data"])
    return bytes(buffer), "mp3"


async def to_pcm16k(data: bytes) -> bytes:
    """任意音频 → PCM s16le / 16 kHz / 单声道，与运行时采集格式一致。"""
    try:
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
            "16000",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("需要 ffmpeg 转码：sudo apt install -y ffmpeg") from exc
    out, err = await proc.communicate(data)
    if proc.returncode != 0 or not out:
        raise RuntimeError(
            "ffmpeg 转码失败："
            + (err.decode("utf-8", "replace").strip()[:200] or "无输出")
        )
    return out


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


def write_report(lines):
    EVID.mkdir(parents=True, exist_ok=True)
    path = EVID / f"api-check-{datetime.now():%Y%m%d-%H%M%S}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", help="直接喂指定音频文件（wav/mp3/ogg/pcm）")
    args = parser.parse_args()

    key = os.environ.get("ASR_VOLCANO_API_KEY", "")
    if "replace-me" in key or not key:
        print("先在 .env 填好 ASR_VOLCANO_API_KEY（控制台 > API Key 管理）")
        return 1
    if not key.isascii():
        print("ASR_VOLCANO_API_KEY 含非 ASCII 字符：多半是把占位符或中文说明一起粘进去了")
        return 1

    try:
        import websockets
    except ModuleNotFoundError:
        print("未安装 websockets：.venv/bin/python -m pip install 'websockets>=13.1'")
        return 1

    if args.file:
        audio_path = Path(args.file)
        if not audio_path.is_file():
            print(f"找不到音频文件：{audio_path}（不给 --file 就用 edge-tts 合成测试句）")
            return 1
        raw = audio_path.read_bytes()
        src_fmt = audio_path.suffix.lstrip(".").lower() or "wav"
        source = f"文件 {audio_path.name}"
    else:
        try:
            raw, src_fmt = await make_test_audio()
        except RuntimeError as exc:
            print(exc)
            return 1
        source = f"edge-tts 合成（{TEST_TEXT}）"

    # 统一转成运行时的采集格式：mp3 等压缩格式原样塞进 raw 帧会解不出时长
    try:
        audio = await to_pcm16k(raw)
    except RuntimeError as exc:
        print(exc)
        return 1
    rate = 16000
    fmt = "pcm"
    seconds = len(audio) / (rate * 2)
    peak = pcm_peak_dbfs(audio)
    print(f"音频：{len(audio)} B / {seconds:.2f} s / 峰值 {peak:.1f} dBFS（{src_fmt} → {fmt}）")
    if seconds < 0.3:
        print("音频不足 0.3 s：换 --file 给一段 3 秒以上的人声再试")
        return 1
    # 落盘同一段音频：换成官方 sauc_python demo 时用它，变量只剩代码
    wav_path = EVID / f"test-{datetime.now():%Y%m%d-%H%M%S}.wav"
    save_wav(wav_path, audio, rate)
    print(f"测试音频：{wav_path}")

    conclusion = "未收到识别结果"
    detail = ""
    text = ""
    duration_ms = ""
    log_id = ""
    started = time.monotonic()
    try:
        resp = await recognize(audio, fmt, rate)
        payload = resp.get("payload_msg") or resp
        text = extract_text(payload)
        duration_ms = (payload.get("audio_info") or {}).get("duration", "")
        log_id = ((payload.get("result") or {}).get("additions") or {}).get("log_id", "")
        conclusion = "通过" if text else "识别文本为空"
        if not text:
            detail = redact(payload or resp)
    except Exception as exc:  # noqa: BLE001 - 失败即结论，不再换组合重试
        hint = http_hint(exc)
        if hint is not None:
            conclusion, detail = hint
        else:
            conclusion = f"识别失败：{type(exc).__name__}"
            detail = redact(exc)
    elapsed = round((time.monotonic() - started) * 1000)

    lines = [
        "# ASR_VOLCANO 自检",
        "",
        f"- 时间：{datetime.now().astimezone().isoformat()}",
        f"- 端点：{os.environ.get('ASR_VOLCANO_WS_URL', '') or DEFAULT_WS}",
        f"- 模型：{os.environ.get('ASR_VOLCANO_RESOURCE_ID', '') or DEFAULT_RESOURCE}",
        f"- 音频：{source}（{src_fmt} → {fmt} / {rate} Hz / {len(audio)} B）",
        f"- 时长与电平：{seconds:.2f} s / 峰值 {peak:.1f} dBFS",
        f"- 测试音频：{wav_path.name}",
        f"- 耗时：{elapsed}ms　音频时长：{duration_ms}ms",
        f"- 识别文本：{text or '无'}",
        f"- logid：{log_id or '无'}",
        "",
        f"**结论：{conclusion}**",
    ]
    if detail:
        lines.insert(-1, f"- 失败原因：{detail}")
    report = write_report(lines)
    print(f"结论：{conclusion}　报告：{report}")
    print(f"识别文本：{text or '无'}")
    if detail:
        print(f"原因：{detail}")
    return 0 if conclusion == "通过" else 1


if __name__ == "__main__":
    load_env()
    sys.exit(asyncio.run(main()))
