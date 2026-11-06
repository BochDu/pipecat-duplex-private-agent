#!/usr/bin/env python3
"""上行规格自检：20 ms / 640 B PCM 帧按实时节奏连发 60 s；无 error 且节奏达标即通过。"""

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from websockets.asyncio.client import connect

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
EVID = HERE / "evidence"
DEFAULT_WS = "wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue"
DEFAULT_MODEL = "1.2.6.1"
FRAME_MS = 20
FRAME_BYTES = 640  # 16000 Hz × 16 bit × 20 ms
DEFAULT_SECONDS = 60


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


def write_evidence(stamp, lines, frames):
    EVID.mkdir(parents=True, exist_ok=True)
    report = EVID / f"upstream-{stamp}.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    csv_path = None
    if frames:
        csv_path = EVID / f"upstream-frames-{stamp}.csv"
        with csv_path.open("w", encoding="utf-8") as handle:
            handle.write("frame,target_ms,actual_ms,drift_ms\n")
            for index, target, actual, drift in frames:
                handle.write(f"{index},{target:.2f},{actual:.2f},{drift:.2f}\n")
    return report, csv_path


def error_summary(err):
    if isinstance(err, dict):
        code = err.get("code", "")
        message = err.get("message", "")
        return redact(f"code={code} message={message}" if code or message else err)
    return redact(err)


async def receive(ws, events, errors, stop):
    while not stop.is_set():
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
        except asyncio.TimeoutError:
            continue
        except Exception:
            return
        try:
            event = json.loads(raw)
        except (ValueError, TypeError):
            continue
        etype = event.get("type", "")
        events[etype] += 1
        if etype == "error":
            errors.append(error_summary(event.get("error") or event))


async def check(seconds):
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

    payload = {
        "type": "session.create",
        "session": {
            "model": model,
            "audio": {
                "input": {"format": {"type": "pcm", "rate": 16000}},
                "output": {"format": {"type": "pcm_s16le", "rate": 24000}, "voice": voice},
            },
        },
        "extension": {"dialog": {}},
    }

    total = int(seconds * 1000 / FRAME_MS)
    frame = base64.b64encode(bytes(FRAME_BYTES)).decode("ascii")
    events = Counter()
    errors = []
    frames = []
    sent = 0
    logid = ""
    session_id = ""
    created = False
    conclusion = "未完成"
    detail = ""
    elapsed = 0.0
    max_drift = 0.0
    drift_frames = 0
    per_second = Counter()

    try:
        async with connect(ws_url, additional_headers={"X-Api-Key": key}, open_timeout=10) as ws:
            response = getattr(ws, "response", None)
            logid = response.headers.get("X-Tt-Logid", "") if response else ""
            await ws.send(json.dumps(payload))

            for _ in range(20):
                event = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                etype = event.get("type", "")
                events[etype] += 1
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
                stop = asyncio.Event()
                task = asyncio.create_task(receive(ws, events, errors, stop))
                start = time.monotonic()
                for index in range(total):
                    target = start + index * FRAME_MS / 1000
                    now = time.monotonic()
                    drift = (now - target) * 1000
                    max_drift = max(max_drift, abs(drift))
                    if abs(drift) > FRAME_MS:
                        drift_frames += 1
                        per_second[int(index * FRAME_MS / 1000)] += 1
                    frames.append((index + 1, index * FRAME_MS, (now - start) * 1000, drift))
                    await ws.send(json.dumps({
                        "event_id": f"evt_{index + 1:05d}",
                        "type": "input_audio_buffer.append",
                        "audio": frame,
                    }))
                    sent += 1
                    if errors:
                        break
                    delay = start + (index + 1) * FRAME_MS / 1000 - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)
                elapsed = time.monotonic() - start
                stop.set()
                try:
                    await asyncio.wait_for(task, timeout=2)
                except asyncio.TimeoutError:
                    task.cancel()
                try:
                    await ws.send(json.dumps({"type": "session.close"}))
                except Exception:
                    pass

                worst = max(per_second.values(), default=0)
                if errors:
                    conclusion = "收到 error"
                    detail = errors[0]
                elif sent != total:
                    conclusion = f"帧数不足（{sent}/{total}）"
                elif worst > 1:
                    conclusion = f"节奏偏差超限（单秒 {worst} 帧）"
                else:
                    conclusion = "通过"
    except Exception as exc:
        conclusion = f"建连失败：{type(exc).__name__}"
        detail = redact(exc)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    worst = max(per_second.values(), default=0)
    lines = [
        "# 上行规格自检",
        "",
        f"- 时间：{datetime.now().astimezone().isoformat()}",
        f"- 端点：{ws_url}",
        f"- 模型：{model}　音色：{voice}",
        f"- 帧规格：PCM16 / 16 kHz / 单声道 / {FRAME_BYTES} B / {FRAME_MS} ms，Base64 放 JSON 文本帧（静音帧）",
        f"- 计划：{seconds} s / {total} 帧　实发：{sent} 帧　耗时：{elapsed:.2f} s",
        f"- 节奏：最大漂移 {max_drift:.2f} ms　超 {FRAME_MS} ms 帧数 {drift_frames}　单秒最多 {worst} 帧（限 1）",
        f"- 下行事件：{', '.join(f'{k}={v}' for k, v in events.most_common(10)) or '无'}",
        f"- error：{len(errors)}" + (f"　首条：{errors[0]}" if errors else ""),
        f"- 响应头 X-Tt-Logid：{logid}",
        f"- session.id：{session_id}",
        "",
        f"**结论：{conclusion}**",
    ]
    if detail and not errors:
        lines.insert(-1, f"- 失败原因：{detail}")
    report, csv_path = write_evidence(stamp, lines, frames)
    if csv_path:
        print(f"逐帧留证：{csv_path}")
    print(f"结论：{conclusion}　报告：{report}")
    if errors:
        print(f"原因：{errors[0]}")
    elif detail:
        print(f"原因：{detail}")
    return 0 if conclusion == "通过" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="上行音频帧节奏自检")
    parser.add_argument("--seconds", type=int, default=DEFAULT_SECONDS, help="连发时长（秒），默认 60")
    args = parser.parse_args()
    load_env()
    sys.exit(asyncio.run(check(max(1, args.seconds))))
