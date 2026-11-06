#!/usr/bin/env python3
"""下行音频自检：不开麦，让服务端合成一句话，确认音频能收齐、能放出声。

流程：session.create → input_audio_mute.commit（不开麦时按协议保活）
      → speech_text_buffer.commit（打招呼事件，服务端直接合成）
      → 收 response.output_audio.delta，落 evidence/downlink-*.wav

判据：收到 delta 且音频时长 ≥ 1 s、无 error；加 --play 还能听到声音。
音量按 `.env` 的 `S2S_DOUBAO_LOUDNESS` / `S2S_DOUBAO_LOUDNESS_NORM` 下发，
报告里的「句间音量极差」可对比响度均衡开关前后的稳定性，
「句首 0.6 s 与之后均值差」为正值说明这句话开头几个字偏小。
用法：python preflight/s2s_doubao/downlink-check.py [--play] [--device plughw:1,0]
"""

import argparse
import asyncio
import base64
import json
import math
import os
import struct
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

from websockets.asyncio.client import connect

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
EVID = HERE / "evidence"
DEFAULT_WS = "wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue"
DEFAULT_MODEL = "1.2.6.1"
DEFAULT_TEXT = "你好，我是豆包。这是一次下行音频自检。"
OUT_RATE = 24000
IDLE_SEC = 8.0


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
    """失败原因去换行、限长，并抹掉可能意外带出的密钥。"""
    text = " ".join(str(value).split())
    key = os.environ.get("S2S_DOUBAO_API_KEY", "")
    if key and key != "replace-me":
        text = text.replace(key, "***")
    return text[:200]


def error_summary(err):
    if isinstance(err, dict):
        code = err.get("code", "")
        message = err.get("message", "")
        return redact(f"code={code} message={message}" if code or message else err)
    return redact(err)


def peak_dbfs(raw):
    count = len(raw) // 2
    if count == 0:
        return -120.0
    samples = struct.unpack(f"<{count}h", raw[: count * 2])
    peak = max((abs(s) for s in samples), default=0)
    return 20 * math.log10(peak / 32768.0) if peak else -120.0


def loudness_percent():
    """音量百分比 0~200（100 为原始），读 S2S_DOUBAO_LOUDNESS。"""
    try:
        percent = int(float(os.environ.get("S2S_DOUBAO_LOUDNESS", "") or 100))
    except ValueError:
        percent = 100
    return max(0, min(200, percent))


def loudness_norm():
    """响度均衡开关，读 S2S_DOUBAO_LOUDNESS_NORM，缺省开。"""
    raw = (os.environ.get("S2S_DOUBAO_LOUDNESS_NORM", "") or "").strip().lower()
    return raw not in ("0", "false", "off", "no")


def window_levels(raw, window_sec=0.5, floor_dbfs=-60.0):
    """逐窗口 RMS（dBFS），跳过静音窗口；用于看开头是否偏小。"""
    step = int(OUT_RATE * window_sec) * 2
    levels = []
    for start in range(0, len(raw) - step + 1, step):
        seg = raw[start : start + step]
        count = len(seg) // 2
        if count == 0:
            continue
        samples = struct.unpack(f"<{count}h", seg)
        rms = math.sqrt(sum(sample * sample for sample in samples) / count)
        if rms <= 0:
            continue
        dbfs = 20 * math.log10(rms / 32768.0)
        if dbfs > floor_dbfs:
            levels.append(dbfs)
    return levels


def window_spread_db(levels):
    """有声段窗口的 RMS 极差（dB）：数值越小，句与句的音量越稳。"""
    if len(levels) < 2:
        return None
    return max(levels) - min(levels)


def onset_gap_db(levels, window_sec=0.5, onset_sec=2.0):
    """开头 onset_sec 与之后的有声窗口均值差（dB）：正值说明开头偏小。"""
    onset = int(onset_sec / window_sec)
    if len(levels) <= onset:
        return None
    head = sum(levels[:onset]) / onset
    tail = levels[onset:]
    return sum(tail) / len(tail) - head


async def play(path, device):
    proc = await asyncio.create_subprocess_exec(
        "aplay",
        "-q",
        "-D",
        device,
        str(path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        return False, "播放超时"
    if proc.returncode != 0:
        return False, redact((err or b"").decode("utf-8", "replace") or f"aplay 退出码 {proc.returncode}")
    return True, ""


async def check(text, device, do_play):
    key = os.environ.get("S2S_DOUBAO_API_KEY", "")
    voice = os.environ.get("S2S_DOUBAO_VOICE", "")
    model = os.environ.get("S2S_DOUBAO_MODEL", DEFAULT_MODEL)
    ws_url = os.environ.get("S2S_DOUBAO_WS_URL", DEFAULT_WS)

    if "replace-me" in (key, voice) or not key or not voice:
        print("先在 .env 填好 S2S_DOUBAO_API_KEY 和 S2S_DOUBAO_VOICE")
        return 1
    if not key.isascii() or not voice.isascii():
        print("API_KEY / VOICE 含非 ASCII 字符：多半是把占位符或中文说明一起粘进去了")
        return 1

    output = {"format": {"type": "pcm_s16le", "rate": OUT_RATE}, "voice": voice}
    offset = max(-50, min(100, loudness_percent() - 100))
    if offset:
        output["loudness"] = offset
    dialog = {}
    if loudness_norm():
        dialog["enable_loudness_norm"] = True
    payload = {
        "type": "session.create",
        "session": {
            "model": model,
            "audio": {
                "input": {"format": {"type": "pcm", "rate": 16000}},
                "output": output,
            },
        },
        "extension": {"dialog": dialog},
    }

    events = []
    errors = []
    chunks = []
    session_id = ""
    logid = ""
    created = False
    played = False
    play_detail = ""
    conclusion = "未完成"
    detail = ""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    wav_path = EVID / f"downlink-{stamp}.wav"

    try:
        async with connect(ws_url, additional_headers={"X-Api-Key": key}, open_timeout=10) as ws:
            response = getattr(ws, "response", None)
            logid = response.headers.get("X-Tt-Logid", "") if response else ""
            await ws.send(json.dumps(payload))

            for _ in range(20):
                event = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                etype = event.get("type", "")
                events.append(etype)
                if etype == "session.created":
                    session = event.get("session", {})
                    session_id = session.get("id", "") or event.get("id", "")
                    created = True
                    break
                if etype == "error":
                    errors.append(error_summary(event.get("error") or event))
                    break

            if not created:
                conclusion = "收到 error" if errors else "未收到 session.created"
            else:
                # 不开麦：按协议发静音事件保活，随后让服务端直接合成一句
                await ws.send(json.dumps({"type": "input_audio_mute.commit"}))
                await ws.send(
                    json.dumps(
                        {
                            "type": "speech_text_buffer.commit",
                            "text": text,
                        }
                    )
                )
                deadline = time.monotonic() + IDLE_SEC
                while time.monotonic() < deadline:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        event = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    etype = event.get("type", "")
                    events.append(etype)
                    if etype in {"response.output_audio.delta", "response.audio.delta"}:
                        payload_b64 = event.get("delta") or event.get("audio") or ""
                        if payload_b64:
                            chunks.append(base64.b64decode(payload_b64))
                    elif etype == "error":
                        errors.append(error_summary(event.get("error") or event))
                        break
                    elif etype == "response.output_audio.done":
                        break
                try:
                    await ws.send(json.dumps({"type": "session.close"}))
                except Exception:
                    pass
    except Exception as exc:
        conclusion = f"建连失败：{type(exc).__name__}"
        detail = redact(exc)

    raw_audio = b"".join(chunks)
    seconds = len(raw_audio) / (OUT_RATE * 2)
    level = peak_dbfs(raw_audio)
    levels = window_levels(raw_audio)
    fine = window_levels(raw_audio, window_sec=0.2)
    spread = window_spread_db(levels)
    gap = onset_gap_db(levels)
    gap_fine = onset_gap_db(fine, window_sec=0.2, onset_sec=0.6)

    if conclusion == "未完成":
        if errors:
            conclusion = "收到 error"
            detail = errors[0]
        elif seconds < 1.0:
            conclusion = f"下行音频不足（{seconds:.2f}s）"
        else:
            conclusion = "通过"

    if raw_audio:
        EVID.mkdir(parents=True, exist_ok=True)
        with wave.open(str(wav_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(OUT_RATE)
            handle.writeframes(raw_audio)

    if do_play and raw_audio:
        played, play_detail = await play(wav_path, device)
        if not played:
            conclusion = "播放失败"
            detail = detail or play_detail

    lines = [
        "# 下行音频自检",
        "",
        f"- 时间：{datetime.now().astimezone().isoformat()}",
        f"- 端点：{ws_url}",
        f"- 模型：{model}　音色：{voice}",
        f"- 触发文本：{text}",
        f"- 音量：loudness {offset:+d}（{loudness_percent()}%）　响度均衡：{'开' if dialog else '关'}",
        f"- 音频规格：PCM16 / {OUT_RATE} Hz / 单声道（audio.format.type=pcm_s16le）",
        f"- 事件序列：{' → '.join(events[:20]) or '无'}",
        f"- 音频块数：{len(chunks)}　字节：{len(raw_audio)}　时长：{seconds:.2f} s　峰值：{level:.1f} dBFS",
        f"- 句间音量极差（0.5 s 窗口 RMS）：{spread:.1f} dB" if spread is not None else "- 句间音量极差：样本不足",
        f"- 句首 0.6 s 与之后均值差：{gap_fine:+.1f} dB（正值说明句首偏小）" if gap_fine is not None else "- 句首 0.6 s 与之后均值差：样本不足",
        f"- 前 2 s 与之后均值差：{gap:+.1f} dB" if gap is not None else "- 前 2 s 与之后均值差：样本不足",
        f"- 逐 0.2 s RMS（前 20 窗）：{' '.join(f'{value:.0f}' for value in fine[:20]) or '无'}",
        f"- 落盘：{wav_path.name if raw_audio else '无'}",
    ]
    if do_play:
        lines.append(f"- 播放（aplay -D {device}）：{'成功' if played else '失败'}")
    lines += [
        f"- 响应头 X-Tt-Logid：{logid}",
        f"- session.id：{session_id}",
        f"- error：{len(errors)}" + (f"　首条：{errors[0]}" if errors else ""),
        "",
        f"**结论：{conclusion}**",
    ]
    if detail and not errors:
        lines.insert(-1, f"- 失败原因：{detail}")

    EVID.mkdir(parents=True, exist_ok=True)
    report = EVID / f"downlink-{stamp}.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"结论：{conclusion}　报告：{report}")
    if errors:
        print(f"原因：{errors[0]}")
    elif detail:
        print(f"原因：{detail}")
    if raw_audio:
        print(f"音频：{wav_path}（{seconds:.2f}s，峰值 {level:.1f} dBFS）")
    if gap_fine is not None:
        print(f"句首 0.6 s 与之后均值差：{gap_fine:+.1f} dB（正值说明句首偏小）")
    return 0 if conclusion == "通过" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="下行音频自检（不开麦）")
    parser.add_argument("--text", default=DEFAULT_TEXT, help="触发合成的文本")
    parser.add_argument("--play", action="store_true", help="收到音频后用 aplay 播放一遍")
    parser.add_argument(
        "--device",
        default="",
        help="播放设备（默认读 AUDIO_OUT_DEVICE / AUDIO_DEVICE，否则 default）",
    )
    args = parser.parse_args()
    load_env()
    device = (
        args.device
        or os.environ.get("AUDIO_OUT_DEVICE")
        or os.environ.get("AUDIO_DEVICE")
        or "default"
    )
    sys.exit(asyncio.run(check(args.text, device, args.play)))
