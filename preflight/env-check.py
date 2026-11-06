#!/usr/bin/env python3
"""通用环境体检：一条命令查完 python / 依赖 / 时间同步 / 注册表 / 凭据 / 音频设备。

只做「机器能不能跑」这一层，不碰云 API、不录音（发声收音体验交给 audio-check.sh）。
报告落 `preflight/evidence/env-*.md`（不入库）；凭据只报已配置/未配置，不进日志。

用法：.venv/bin/python preflight/env-check.py [--link s2s|llm|all] [--net]
"""

import argparse
import importlib.util
import os
import re
import shutil
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
EVID = HERE / "evidence"
CARD_RE = re.compile(r"^card (\d+): (.+?) \[(.*?)\], device (\d+): (.+)$")
# (模块, 属于哪条链路, 说明)
DEPS = [
    ("websockets", "s2s", "S2S / ASR 云服务"),
    ("httpx", "llm", "LLM"),
    ("edge_tts", "llm", "TTS"),
]

sys.path.insert(0, str(ROOT))  # 从任意目录执行都能 import app.*


def load_env():
    """config/defaults.env 打底，.env 覆盖；返回本机是否存在 .env。"""
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
    return (ROOT / ".env").exists()


def run(cmd, timeout=15, stdin_bytes=None):
    return subprocess.run(
        cmd,
        input=stdin_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )


def devices():
    """解析 arecord -l / aplay -l 的 card 行。"""
    found = {"capture": [], "playback": []}
    for kind, cmd in (("capture", ["arecord", "-l"]), ("playback", ["aplay", "-l"])):
        if not shutil.which(cmd[0]):
            continue
        out = run(cmd).stdout.decode("utf-8", "replace")
        for line in out.splitlines():
            hit = CARD_RE.match(line.strip())
            if hit:
                card, name, _, dev, desc = hit.groups()
                found[kind].append(f"plughw:{card},{dev}（card {card}: {name} / {desc}）")
    return found


def check_python():
    version = ".".join(str(x) for x in sys.version_info[:3])
    ok = sys.version_info >= (3, 11)
    return ok, version, "需要 >= 3.11（tomllib 与 asyncio 子进程接口）"


def check_deps(target):
    rows = []
    ok = True
    for module, link, note in DEPS:
        present = importlib.util.find_spec(module) is not None
        if not present and target in (link, "all"):
            ok = False
        rows.append(f"{module}: {'已装' if present else '缺（' + note + ' 不可用）'}")
    return ok, "；".join(rows), "S2S 最小依赖只有 websockets"


def check_time():
    if not shutil.which("timedatectl"):
        return True, "无 timedatectl（非 systemd，跳过）", ""
    try:
        out = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"]).stdout
        value = out.decode().strip().lower()
    except Exception as exc:  # noqa: BLE001
        return False, f"读取失败：{exc}", "TLS 失败多半是时间没同步：sudo timedatectl set-ntp true"
    return value in {"yes", "1", "true"}, f"NTPSynchronized={value or '空'}", "没同步会导致 TLS 握手失败"


def check_registry():
    try:
        from app.config import Registry
    except Exception as exc:  # noqa: BLE001
        return False, f"导入失败：{type(exc).__name__}", "确认在仓库根目录执行、PYTHONPATH 含 ."
    try:
        registry = Registry.load(ROOT)
    except Exception as exc:  # noqa: BLE001
        return False, f"加载失败：{exc}", "config/providers.toml 结构不对"
    words = "、".join(f"{e.word}({e.link}/{e.provider})" for e in registry.wakewords)
    return True, f"{len(registry.wakewords)} 条唤醒词：{words}", ""


def check_groups(target):
    """只查目标链路那一组凭据是否填全；只报键名，不打印值。"""
    from app.config import Registry, Settings

    registry = Registry.load(ROOT)
    groups = [e.config_group for e in registry.wakewords if target in (e.link, "all")]
    settings = Settings.load(ROOT, registry.group_names)
    rows = []
    ok = True
    for group in dict.fromkeys(groups):
        values = settings.group(group)
        missing = [k for k, v in values.items() if not v or v == "replace-me"]
        if not values:
            ok = False
            rows.append(f"{group}: 无任何键")
        elif missing:
            ok = False
            rows.append(f"{group}: {len(values) - len(missing)}/{len(values)} 已填，缺 {','.join(missing)}")
        else:
            rows.append(f"{group}: {len(values)} 项已填")
    return ok, "；".join(rows) or "无", ".env 里只填本轮要测的那一组"


def check_devices():
    """只核对 `.env` 的设备名在不在 `arecord -l` / `aplay -l` 清单里。

    真录真放是 `audio-check.sh` 的职责，这里不重复做，避免两个地方各验一套。
    """
    from app.config import Registry, Settings

    registry = Registry.load(ROOT)
    settings = Settings.load(ROOT, registry.group_names)
    fallback = settings.get("AUDIO_DEVICE", "") or "default"
    in_dev = settings.get("AUDIO_IN_DEVICE", "") or fallback
    out_dev = settings.get("AUDIO_OUT_DEVICE", "") or fallback

    if not shutil.which("arecord") or not shutil.which("aplay"):
        return False, "缺 arecord/aplay", "sudo apt install -y alsa-utils"

    found = devices()
    detail = [f"采集 {in_dev} / 播放 {out_dev}"]
    ok = True
    for kind, dev, label in (("capture", in_dev, "采集"), ("playback", out_dev, "播放")):
        names = [item.split("（")[0] for item in found[kind]]
        if dev == "default":
            detail.append(f"{label}设备留了 default，建议按 ⑤ 改成 plughw:N,0")
        elif dev not in names:
            ok = False
            detail.append(f"{label}设备 {dev} 不在清单（{'、'.join(names) or '无'}）")

    return ok, "；".join(detail), "真录真放用 `bash preflight/audio-check.sh` 验"


def check_net():
    allow = [h.strip() for h in (os.environ.get("OUTBOUND_ALLOWLIST", "") or "").split(",") if h.strip()]
    if not allow:
        return True, "未配置 OUTBOUND_ALLOWLIST，跳过", ""
    rows = []
    ok = True
    for host in allow:
        try:
            with socket.create_connection((host, 443), timeout=5):
                rows.append(f"{host}:443 通")
        except Exception as exc:  # noqa: BLE001
            ok = False
            rows.append(f"{host}:443 不通（{type(exc).__name__}）")
    return ok, "；".join(rows), ""


def main():
    parser = argparse.ArgumentParser(description="通用环境体检")
    parser.add_argument("--link", choices=["all", "s2s", "llm"], default="all", help="只校验某条链路相关的依赖与凭据")
    parser.add_argument("--net", action="store_true", help="额外探测 OUTBOUND_ALLOWLIST 的 443 连通性")
    args = parser.parse_args()

    has_env = load_env()
    checks = [
        ("Python", check_python()),
        ("依赖", check_deps(args.link)),
        ("时间同步", check_time()),
        ("注册表", check_registry()),
        (f".env 凭据（{args.link}）", check_groups(args.link)),
        ("音频设备", check_devices()),
    ]
    if args.net:
        checks.append(("出站网络", check_net()))

    ok_all = all(item[1][0] for item in checks)
    lines = [
        "# 通用环境体检",
        "",
        f"- 时间：{datetime.now().astimezone().isoformat()}",
        f"- 主机：{socket.gethostname()}　架构：{os.uname().machine if hasattr(os, 'uname') else sys.platform}",
        f"- Python：{sys.executable}",
        f"- `.env`：{'已加载' if has_env else '不存在（cp .env.example .env）'}",
        "",
        "| 项 | 结果 | 说明 | 提示 |",
        "| --- | --- | --- | --- |",
    ]
    for name, (ok, detail, hint) in checks:
        lines.append(f"| {name} | {'通过' if ok else '不通过'} | {detail} | {hint} |")

    audio = devices()
    lines += ["", "## 看到的音频设备", ""]
    lines += [f"- 采集：{d}" for d in audio["capture"]] or ["- 采集：无"]
    lines += [f"- 播放：{d}" for d in audio["playback"]] or ["- 播放：无"]
    lines += ["", f"**结论：{'通过' if ok_all else '存在不通过项'}**", ""]

    EVID.mkdir(parents=True, exist_ok=True)
    report = EVID / f"env-{datetime.now():%Y%m%d-%H%M%S}.md"
    report.write_text("\n".join(lines), encoding="utf-8")

    for name, (ok, detail, _) in checks:
        print(f"[{'OK' if ok else '!!'}] {name}：{detail}")
    print(f"结论：{'通过' if ok_all else '存在不通过项'}　报告：{report}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
