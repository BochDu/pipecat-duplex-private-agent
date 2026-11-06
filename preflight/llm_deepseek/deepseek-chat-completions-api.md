# Chat Completions API（DeepSeek 官方文档摘录）

> 来源：DeepSeek 官方 API 文档，2026-11 摘录。根据输入的上下文，让模型补全对话内容。

## 接口

```http
POST /chat/completions
```

operationId: `createChatCompletion`

### 服务地址

- `https://api.deepseek.com`

## 身份验证

**bearerAuth**

```
Authorization: Bearer <API_KEY>
```

## 模型

`model` 仅支持：

- `deepseek-flash`
- `deepseek-v4-pro`

## 请求体

**必填**：`model`、`messages`

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `messages` | 是 | 消息列表，元素为 system / user / assistant / tool 四种消息之一 |
| `model` | 是 | `deepseek-flash` 或 `deepseek-v4-pro` |
| `thinking` | 否 | `{"type": "enabled" \| "disabled"}`；省略且未传 `reasoning_effort` 时**默认开启思考模式** |
| `reasoning_effort` | 否 | `none` 关思考；`low`/`high`/`max` 开思考，默认强度 `high` |
| `max_tokens` | 否 | 1–384K；未设置时非思考默认 8K、思考默认 64K（`max` 时 128K） |
| `response_format` | 否 | `text` 或 `json_object`；JSON 模式须同时在消息里指示模型输出 JSON |
| `stop` | 否 | string 或最多 16 个 string |
| `stream` | 否 | `true` 时以 SSE 流式返回，以 `data: [DONE]` 结尾，默认 `false` |
| `stream_options` | 否 | `{"include_usage": true}` 时最后一个块携带整体 usage |
| `temperature` | 否 | 0–2，默认 1；思考模式下不生效 |
| `top_p` | 否 | (0, 1]；`deepseek-flash` 思考模式低于 0.95 按 0.95 处理、非思考恒为 1.0；`deepseek-v4-pro` 思考模式下有 tools 恒为 0.95 |
| `tools` | 否 | function 工具列表，tool 名称必须唯一 |
| `tool_choice` | 否 | `none` / `auto` / `required` / 指定具体 function；**思考模式下不支持 `required` 和指定具体 tool，会返回 400** |
| `logprobs` / `top_logprobs` | 否 | 返回 token 对数概率，top_logprobs 0–20 |
| `user_id` | 否 | 业务侧用户标识；可用于内容安全、KVCache 隔离、调度隔离 |
| `frequency_penalty` / `presence_penalty` | 否 | **已废弃，传入无效果** |

### 消息类型

- **system / user**：`content` 为字符串或内容块数组（text / image_url / file）。
- **assistant**：`content` 可为 null；可选 `name`、`prefix`（Beta，须用 `base_url="https://api.deepseek.com/beta"`）、`reasoning_content`（Beta，对话前缀续写用）。
- **tool**：必须带 `role: "tool"`、`tool_call_id`、`content`。

## 响应

### 200 非流式

返回 `chat.completion` 对象：

```json
{
  "id": "88beaa90-17fd-45fc-939c-2ef75e2d913c",
  "object": "chat.completion",
  "created": 1793084842,
  "model": "deepseek-flash",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! 👋 How can I help you today?"
      },
      "logprobs": null,
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 7,
    "completion_tokens": 11,
    "total_tokens": 18,
    "prompt_tokens_details": {"cached_tokens": 0},
    "prompt_cache_hit_tokens": 0,
    "prompt_cache_miss_tokens": 7
  },
  "system_fingerprint": "e72d3c7f663d3e1383b00b3884cb4501"
}
```

### 200 流式（text/event-stream）

返回一系列 `chat.completion.chunk`；`finish_reason` 为 null 的内容增量块，最后一个块 `finish_reason` 非 null 且 `choices` 只含一个元素（统计信息附在该块上，不单独下发 usage 块），以 `data: [DONE]` 结尾。

`delta` 中 `tool_calls` 的分片规则：**每个 tool 调用的第一个 chunk 携带 `index`、`id`、`type`、`function.name`，后续 chunk 只携带 `index` 与 `function.arguments` 增量**，需按 `index` 聚合。

## finish_reason

| 值 | 含义 |
| --- | --- |
| `stop` | 自然停止，或命中 `stop` 序列 |
| `length` | 达到上下文长度或 `max_tokens` 限制，可能截断 |
| `content_filter` | 触发过滤策略 |
| `tool_calls` | 模型进行了工具调用 |
| `insufficient_system_resource` | 系统推理资源不足，生成被打断 |
| `aborted` | 生成过程被中断 |

## usage

`prompt_tokens = prompt_cache_hit_tokens + prompt_cache_miss_tokens`；`completion_tokens_details.reasoning_tokens` 为思考模式产生的思维链 token 数。

## FunctionObject（tools 元素）

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | `a-zA-Z0-9_-`，最长 128 字符 |
| `description` | 否 | 供模型理解何时调用 |
| `parameters` | 否 | JSON Schema 对象；省略即空参数列表 |
| `strict` | 否 | `true` 时启用 strict 模式（Beta） |

## 代码示例

### curl

```bash
# Set DEEPSEEK_API_KEY to your API key before running this example.
curl --silent --show-error --max-time 60 "https://api.deepseek.com/chat/completions" \
  --header "Authorization: Bearer ${DEEPSEEK_API_KEY}" \
  --header "Content-Type: application/json" \
  --data '{
  "model": "deepseek-flash",
  "messages": [
    {
      "role": "user",
      "content": "Say hello."
    }
  ],
  "max_tokens": 32,
  "temperature": 0,
  "thinking": {
    "type": "disabled"
  },
  "stream": false
}'
```

### Python — OpenAI SDK

```python
# Set DEEPSEEK_API_KEY to your API key before running this example.
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
    timeout=60.0,
)

response = client.chat.completions.create(
    model="deepseek-flash",
    messages=[{"role": "user", "content": "Say hello."}],
    max_tokens=32,
    temperature=0,
    stream=False,
    extra_body={"thinking": {"type": "disabled"}},
)
print(response.model_dump_json(indent=2))
```

## 对本项目的注意点

- 语音场景延迟敏感，自检脚本与主链路应显式传 `"thinking": {"type": "disabled"}`（省略时默认开思考模式）。
- 思考模式下 `tool_choice` 只能用 `auto`；本仓库适配层固定传 `"auto"`，兼容。
- `.env` 的 `MODEL` 应填 `deepseek-flash`（文档已不含 `deepseek-chat`）。
- 新增 `finish_reason: insufficient_system_resource` / `aborted`，语音场景可能遇到。
