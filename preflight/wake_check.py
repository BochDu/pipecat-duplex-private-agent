#!/usr/bin/env python3
"""唤醒词自检：用生产同一条监听代码跑一段，统计每个唤醒词的命中次数。

用法：
  .venv/bin/python preflight/wake_check.py --seconds 30

判据：每个唤醒词至少命中 2 次（脚本会提示你每个词说 3 次）。
"""

import argparse
import asyncio
import logging
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
EVID = HERE / "evidence"
sys.path.insert(0, str(ROOT))

from app.config import Registry, Settings  # noqa: E402
from app.wake import build_listener  # noqa: E402


def load_env() -> None:
    """config/defaults.env 打底，.env 覆盖，真实环境变量最优先。"""
    import os

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


class Override:
    """在只读 Settings 上盖几个命令行参数。"""

    def __init__(self, base, **values) -> None:
        self._base = base
        self._values = {k: v for k, v in values.items() if v}

    def get(self, key, default=None):
        return self._values.get(key) or self._base.get(key, default)


async def consume(listener, hits: Counter, deadline: float) -> None:
    async for word in listener.listen():
        hits[word] += 1
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{stamp}] 命中第 {hits[word]} 次：{word}", flush=True)
        if time.monotonic() >= deadline:
            return


async def run(args) -> int:
    registry = Registry.load(ROOT)
    settings = Settings.load(ROOT, registry.group_names)
    words = [entry.word for entry in registry.wakewords]
    device = args.device or settings.in_device
    model = args.model or settings.get("WAKEWORD_KWS_DIR", "") or "data/models/kws"
    config = Override(settings, AUDIO_IN_DEVICE=device, WAKEWORD_KWS_DIR=model)

    print("引擎：kws")
    print(f"模型：{model}")
    print(f"设备：{device}")
    print(f"唤醒词：{'、'.join(words)}", flush=True)

    listener = build_listener(config, words)
    listener.load()  # 先把模型读进来，别把开头几句话喂给初始化
    print("2 秒后开始录音…", flush=True)
    await asyncio.sleep(2)
    print(f"开始：{args.seconds} 秒内每个词说 3 次，词间停 1 秒", flush=True)

    hits: Counter = Counter()
    deadline = time.monotonic() + args.seconds
    task = asyncio.create_task(consume(listener, hits, deadline))
    try:
        await asyncio.sleep(args.seconds)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    ok_words = [w for w in words if hits[w] >= args.need]
    conclusion = "通过" if len(ok_words) == len(words) else "存在漏检"
    lines = [
        "# 唤醒词自检",
        "",
        f"- 时间：{datetime.now().astimezone().isoformat()}",
        "- 引擎：kws",
        f"- 环境：{args.seconds} s 内每个词说 3 次，判据 ≥ {args.need} 次命中",
        f"- 设备：{device}",
        f"- 模型：{model}",
        f"- 唤醒词：{'、'.join(words)}",
    ]
    lines += [
        "",
        "| 唤醒词 | 命中 | 结果 |",
        "| --- | --- | --- |",
    ]
    for word in words:
        lines.append(
            f"| {word} | {hits[word]} | {'通过' if hits[word] >= args.need else '漏检'} |"
        )
    lines += ["", f"**结论：{conclusion}**", ""]

    EVID.mkdir(parents=True, exist_ok=True)
    report = EVID / f"wake-{datetime.now():%Y%m%d-%H%M%S}.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"结论：{conclusion}　报告：{report}")
    return 0 if conclusion == "通过" else 1


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="唤醒词自检（离线）")
    parser.add_argument("--seconds", type=int, default=30, help="监听时长（秒）")
    parser.add_argument("--need", type=int, default=2, help="每个词至少命中几次算通过")
    parser.add_argument("--model", default="", help="模型目录，默认读 .env")
    parser.add_argument("--device", default="", help="采集设备，默认读 .env")
    args = parser.parse_args()
    load_env()
    try:
        return asyncio.run(run(args))
    except (RuntimeError, ValueError) as exc:
        print(f"失败：{exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
