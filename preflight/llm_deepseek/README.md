# LLM_DEEPSEEK 接入前检查

DeepSeek Chat Completions 自检：一条命令完成验证。环境见 [安装与排障](../../docs/setup.md)。

## 快速验证

`.env` 里只需有 `LLM_DEEPSEEK_API_KEY`（端点与模型名已由 `config/defaults.env` 提供）。本项额外依赖 `httpx`，`requirements.txt` 已包含。

```bash
.venv/bin/python preflight/llm_deepseek/api-check.py
```

打印 `结论：通过` 且退出码 0 即通过；报告自动落本目录 `evidence/`（不入库），密钥不进日志。

## 通过判据

脚本一次验证三件事，与 [技术方案](../../docs/architecture.md) 的门禁判据一一对应：

| 检查项 | 做法 | 通过判据 |
| --- | --- | --- |
| 流式输出 | 发一条消息收 SSE 流 | 收到非空流式文本 |
| `tool_calls` | 注册测试工具并要求调用 | 返回结构完整：`id` / `name` / `arguments` |
| `role=tool` 回传 | 回填工具结果再请求 | 拿到引用工具结果的最终回答 |

## 适用条件

- 只需出站 443（`api.deepseek.com`）；不需要音频设备，不需要 pipecat 子仓库
- 脚本对报错文本做脱敏，密钥不会进日志与报告
- 官方依据：[deepseek-chat-completions-api.md](deepseek-chat-completions-api.md)（官方文档摘录）

自检通过并留证后，`app/providers/openai_compat/provider.py` 才算验收通过。

## 预检排障

| 现象 | 处理 |
| --- | --- |
| 401 / `authentication` | 核对 `LLM_DEEPSEEK_API_KEY` |
| 404 / 模型不存在 | 核对 `LLM_DEEPSEEK_BASE_URL` 与 `LLM_DEEPSEEK_MODEL` |
| 流式无输出 | 出站 443 是否可达；`MODEL` 是否支持 `/chat/completions` 流式 |
| `tool_calls` 结构不全 | 换支持 FunctionCall 的模型；报告里看原始返回 |
| `未安装 httpx` | `.venv/bin/python -m pip install httpx` |

## 验证状态

| 检查项 | 状态 |
| --- | --- |
| 流式输出 / `tool_calls` / `role=tool` 回传 | 已通过：2026-11-06，首字 799 ms，`tool_calls` 与 `role=tool` 结构完整 |
