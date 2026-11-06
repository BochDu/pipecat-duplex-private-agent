#!/usr/bin/env python3
"""LLM_DEEPSEEK 自检：流式输出、tool_calls、role=tool 回传，一条命令跑完并留证。"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
EVID = HERE / "evidence"
DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"

# 测试工具：只验证协议结构，不真查天气
TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查询某城市当前天气",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市名"}},
            "required": ["city"],
        },
    },
}
TOOL_RESULT = "晴，12°C，西北风 3 级"


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
    key = os.environ.get("LLM_DEEPSEEK_API_KEY", "")
    if key and key != "replace-me":
        text = text.replace(key, "***")
    return text[:300]


def write_report(lines):
    EVID.mkdir(parents=True, exist_ok=True)
    path = EVID / f"api-check-{datetime.now():%Y%m%d-%H%M%S}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


async def stream_once(client, url, headers, model, messages, tools=None):
    """一轮流式请求；返回 (文本, tool_calls 列表, chunk 数, 首字毫秒, 错误或 None)。"""
    payload: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    parts: list[str] = []
    calls: dict[int, dict[str, str]] = {}
    chunks = 0
    first_ms = None
    start = time.monotonic()

    async with client.stream("POST", url, headers=headers, json=payload) as resp:
        if resp.status_code >= 400:
            body = await resp.aread()
            return "", [], 0, None, f"HTTP {resp.status_code}：{redact(str(body[:300]))}"
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            chunks += 1
            delta = choices[0].get("delta") or {}
            content = delta.get("content")
            if content:
                if first_ms is None:
                    first_ms = (time.monotonic() - start) * 1000
                parts.append(content)
            for tc in delta.get("tool_calls") or []:
                index = tc.get("index", 0)
                slot = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                function = tc.get("function") or {}
                if function.get("name"):
                    slot["name"] = function["name"]
                if function.get("arguments"):
                    slot["arguments"] += function["arguments"]

    text = "".join(parts)
    tool_calls = [
        {
            "id": slot["id"] or f"call_{index}",
            "name": slot["name"],
            "arguments": slot["arguments"] or "{}",
        }
        for index, slot in calls.items()
    ]
    return text, tool_calls, chunks, first_ms, None


async def check():
    key = os.environ.get("LLM_DEEPSEEK_API_KEY", "")
    base = (os.environ.get("LLM_DEEPSEEK_BASE_URL", "") or DEFAULT_BASE).rstrip("/")
    model = os.environ.get("LLM_DEEPSEEK_MODEL", "") or DEFAULT_MODEL

    if "replace-me" in key or not key:
        print("先在 .env 填好 LLM_DEEPSEEK_API_KEY")
        return 1
    if not key.isascii():
        print("LLM_DEEPSEEK_API_KEY 含非 ASCII 字符：多半是把占位符或中文说明一起粘进去了")
        return 1

    try:
        import httpx
    except ModuleNotFoundError:
        print("未安装 httpx：.venv/bin/python -m pip install httpx")
        return 1

    url = base + "/chat/completions"
    headers = {"Authorization": f"Bearer {key}"}
    checks: list[tuple[str, bool, str]] = []
    detail = ""

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            # ① 流式输出
            text1, _, chunks, first_ms, err = await stream_once(
                client, url, headers, model,
                [{"role": "user", "content": "用一句话回答：1+1 等于几？"}],
            )
            checks.append(("流式输出", err is None and bool(text1.strip()),
                           f"chunks={chunks} 首字={first_ms and round(first_ms)}ms" if err is None else err))

            # ② tool_calls 结构
            msgs2 = [{"role": "user", "content": "调用工具查询北京的天气"}]
            _, calls2, _, _, err2 = await stream_once(
                client, url, headers, model, msgs2, tools=[TOOL],
            )
            call = calls2[0] if calls2 else {}
            try:
                args = json.loads(call.get("arguments") or "{}")
            except ValueError:
                args = {}
            ok2 = err2 is None and call.get("name") == "get_weather" and args.get("city")
            checks.append(("tool_calls", ok2,
                           f"name={call.get('name', '')} arguments={call.get('arguments', '')}"
                           if err2 is None else err2))

            # ③ role=tool 回传后续答
            messages = msgs2 + [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call.get("id", "call_0"),
                            "type": "function",
                            "function": {
                                "name": call.get("name", "get_weather"),
                                "arguments": call.get("arguments", "{}"),
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": call.get("id", "call_0"), "content": TOOL_RESULT},
            ]
            text3, _, _, _, err3 = await stream_once(client, url, headers, model, messages)
            ok3 = err3 is None and bool(text3.strip())
            checks.append(("role=tool 回传", ok3, f"回答={text3[:80]}" if err3 is None else err3))
    except Exception as exc:
        detail = redact(exc) or "请求中断"
        while len(checks) < 3:
            checks.append((("流式输出", "tool_calls", "role=tool 回传")[len(checks)], False, "未执行"))

    lines = [
        "# LLM_DEEPSEEK 自检",
        "",
        f"- 时间：{datetime.now().astimezone().isoformat()}",
        f"- 端点：{url}",
        f"- 模型：{model}",
        "",
        "| 检查项 | 结果 | 说明 |",
        "| --- | --- | --- |",
    ]
    for name, ok, note in checks:
        lines.append(f"| {name} | {'通过' if ok else '未通过'} | {note} |")
    lines.append("")
    conclusion = "通过" if all(ok for _, ok, _ in checks) else "未通过"
    lines.append(f"**结论：{conclusion}**")
    if detail:
        lines.insert(-1, f"- 失败原因：{detail}")

    report = write_report(lines)
    print(f"结论：{conclusion}　报告：{report}")
    for name, ok, note in checks:
        print(f"  {'✔' if ok else '✘'} {name}：{note}")
    if detail:
        print(f"原因：{detail}")
    return 0 if conclusion == "通过" else 1


if __name__ == "__main__":
    load_env()
    sys.exit(asyncio.run(check()))
