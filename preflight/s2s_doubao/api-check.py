#!/usr/bin/env python3
"""最小建连自检：发 session.create，确认收到 session.created；抓帧脱敏留证。"""

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from websockets.asyncio.client import connect

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
EVID = HERE / "evidence"
DEFAULT_WS = "wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue"
DEFAULT_MODEL = "1.2.6.1"


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


def write_report(lines):
    EVID.mkdir(parents=True, exist_ok=True)
    path = EVID / f"api-connect-{datetime.now():%Y%m%d-%H%M%S}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def redact(value):
    """失败原因去换行、限长，并抹掉可能意外带出的密钥。"""
    text = " ".join(str(value).split())
    key = os.environ.get("S2S_DOUBAO_API_KEY", "")
    if key and key != "replace-me":
        text = text.replace(key, "***")
    return text[:200]


async def check():
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

    events = []
    session_id = ""
    logid = ""
    conclusion = "未收到 session.created"
    detail = ""

    try:
        async with connect(ws_url, additional_headers={"X-Api-Key": key}, open_timeout=10) as ws:
            response = getattr(ws, "response", None)
            logid = response.headers.get("X-Tt-Logid", "") if response else ""
            await ws.send(json.dumps(payload))
            for _ in range(20):
                event = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                events.append(event.get("type", ""))
                if event.get("type") == "session.created":
                    session = event.get("session", {})
                    session_id = session.get("id", "") or event.get("id", "")
                    conclusion = "通过"
                    break
                if event.get("type") == "error":
                    conclusion = "收到 error"
                    err = event.get("error") or event
                    if isinstance(err, dict):
                        code = err.get("code", "")
                        message = err.get("message", "")
                        detail = redact(f"code={code} message={message}" if code or message else err)
                    else:
                        detail = redact(err)
                    break
            await ws.send(json.dumps({"type": "session.close"}))
    except Exception as exc:
        conclusion = f"建连失败：{type(exc).__name__}"
        detail = redact(exc)

    lines = [
        "# 最小建连自检",
        "",
        f"- 时间：{datetime.now().astimezone().isoformat()}",
        f"- 端点：{ws_url}",
        f"- 模型：{model}　音色：{voice}",
        f"- 响应头 X-Tt-Logid：{logid}",
        f"- 事件序列：{' → '.join(events) or '无'}",
        f"- session.id：{session_id}",
        "",
        f"**结论：{conclusion}**",
    ]
    if detail:
        lines.insert(-1, f"- 失败原因：{detail}")
    report = write_report(lines)
    print(f"结论：{conclusion}　报告：{report}")
    if detail:
        print(f"原因：{detail}")
    return 0 if conclusion == "通过" else 1


if __name__ == "__main__":
    load_env()
    sys.exit(asyncio.run(check()))
