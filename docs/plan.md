# 阶段计划（严格串行）

从 [技术方案](architecture.md) 拆出，单独维护；执行状态以本文件为准。

| 阶段 | 交付 | 进入下一阶段的门禁 |
| --- | --- | --- |
| 0 骨架 | `config/providers.toml`<br/>`app/providers/base.py`<br/>各 Provider 空目录<br/>`sessions/` 通用模板骨架<br/>preflight 目录骨架<br/>全局单例框架<br/>`main.py` 入口 | 契约完整（5 个抽象方法 + `llm` 的 `chat()` + 可选 `cancel_response`）<br/>目录 100% 合规<br/>无任何业务硬编码配置 |
| 1 豆包 S2S | `preflight/s2s_doubao/` 验证通过<br/>→ `volcano_s2s/provider.py`<br/>→ 唤醒 / 打断 / 静默 / 断网自测 | 接口连通与上行节奏留证<br/>喊停停声 < 0.4 s 且不累加回话<br/>资源释放无残留<br/>目录评审通过 |
| 2 DeepSeek LLM | `preflight/llm_deepseek/` 验证通过<br/>→ `openai_compat/provider.py` + `sessions/llm` 模板 | 流式格式兼容<br/>与 S2S 互斥 |
| 3 全局路由 | `wake_router.py` + `main.py` 完整落地<br/>注册表加载、懒加载、强互斥、统一生命周期 | 两组唤醒词任意顺序切换无冲突<br/>重复唤醒自动重启<br/>资源启停无泄漏 |
| 4 全量回归 | 双模型轮流唤醒、长时间静默销毁、网络异常回收、高频切换无泄漏<br/>建立版本锚点 | 全部通过<br/>锚点文件落地<br/>全局目录终评 |
| 5 后台能力 | 工具池（≥1 个 public 工具）、作业异步化与幂等、调度、记忆抽取、笔记索引、推送、审计<br/>三段式播报中打断、S2S 建连摘要注入 | 共享层复用门禁通过（落盘格式一致、幂等键生效、后台模块零改动）<br/>三段式插话停播 ≤ 0.5 s 且上下文不丢<br/>S2S 的 `dialog_context` 被服务端 ack |

> 千问 Omni S2S（`preflight/s2s_qwen/` + `qwen_omni_s2s/`）与 GPT（`preflight/llm_gpt/`）暂缓，**阶段 3 之后**再接；届时按「新增一个模型」流程恢复目录与注册表条目。

## 每阶段必须输出

- 验证日志
- 新增代码
- 目录评审报告

评审不通过禁止进入下一阶段。所有模型遵循「先预检、后适配、再验收」。
