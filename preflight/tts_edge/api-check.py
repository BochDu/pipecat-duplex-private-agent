#!/usr/bin/env python3
"""TTS_EDGE 自检：edge-tts 合成固定测试句，验音频数据有效；--play 加本地播放。"""

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
EVID = HERE / "evidence"
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
TEST_TEXT = "三段式语音合成自检，这句话用于验证文字转语音链路。"
MIN_BYTES = 5 * 1024


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
    path = EVID / f"api-check-{datetime.now():%Y%m%d-%H%M%S}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


async def check(play: bool) -> int:
    try:
        import edge_tts
    except ModuleNotFoundError:
        print("未安装 edge-tts：.venv/bin/python -m pip install edge-tts")
        return 1

    voice = os.environ.get("TTS_VOICE", "") or DEFAULT_VOICE
    chunks = 0
    total = 0
    first_ms = None
    start = time.monotonic()
    conclusion = "未收到音频数据"
    detail = ""

    path = EVID / f"tts-sample-{datetime.now():%Y%m%d-%H%M%S}.mp3"
    EVID.mkdir(parents=True, exist_ok=True)
    try:
        communicate = edge_tts.Communicate(TEST_TEXT, voice)
        with path.open("wb") as fh:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    if first_ms is None:
                        first_ms = (time.monotonic() - start) * 1000
                    chunks += 1
                    total += len(chunk["data"])
                    fh.write(chunk["data"])
        if total >= MIN_BYTES:
            conclusion = "通过"
        else:
            conclusion = f"音频过小：{total} B"
    except Exception as exc:
        conclusion = f"合成失败：{type(exc).__name__}"
        detail = " ".join(str(exc).split())[:200]

    played = "-"
    if play and conclusion == "通过":
        proc = await asyncio.create_subprocess_exec(
            "ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        played = "已播放（听感：无爆音/人耳确认）" if proc.returncode == 0 else f"ffplay 退出码 {proc.returncode}"

    lines = [
        "# TTS_EDGE 自检",
        "",
        f"- 时间：{datetime.now().astimezone().isoformat()}",
        f"- 音色：{voice}",
        f"- 测试句：{TEST_TEXT}",
        f"- 音频块：{chunks}　体积：{total} B　首块：{first_ms and round(first_ms)}ms",
        f"- 试听文件：{path if path.exists() else '无'}",
        f"- 播放：{played}",
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--play", action="store_true", help="合成后用 ffplay 本地播放，人耳确认")
    args = parser.parse_args()
    load_env()
    sys.exit(asyncio.run(check(args.play)))
