# 接入前检查

每个模型接入前必须独立验证通过，才允许写业务适配层。

- 体系、四步跑通路径、新增模型流程：[接入前检查](../docs/preflight.md)
- 环境搭建与排障：[安装与排障](../docs/setup.md)

本目录只放检查工具与各品牌自检脚本：

| 项 | 文件 |
| --- | --- |
| 机器 | `env-check.py` |
| 音频 | `audio-check.sh` |
| 唤醒 | `wake_check.py` |
| 服务 | [`s2s_doubao/`](s2s_doubao/README.md) [`llm_deepseek/`](llm_deepseek/README.md) [`asr_volcano/`](asr_volcano/README.md) [`tts_edge/`](tts_edge/README.md) |

每个品牌 README 结构相同：快速验证 / 通过判据 / 适用条件 / 预检排障 / 验证状态，链路类另加「主程序全链路」。报告落 `evidence/` 与各品牌目录的 `evidence/`，均不入库。
