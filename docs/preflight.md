# 接入前检查

每个模型接入前必须独立验证通过，才允许写业务适配层。具体 API 的测试命令、判据与留证写在各自的品牌目录里，本文件只做体系与索引。所有命令在仓库根目录执行。

## 体系总览

由外到内四层，每层只回答一个问题，出问题先看是哪一层：

| 层 | 文件 | 回答的问题 |
| --- | --- | --- |
| 机器 | `preflight/env-check.py` | python / 依赖 / 时间同步 / 注册表 / 本轮凭据 / 设备名是否对得上 |
| 音频体验 | `preflight/audio-check.sh` | 唯一验收口：真录真放，能发声、能调音量、能收音 |
| 唤醒 | `preflight/wake_check.py` | 离线模型能不能听出「豆包」「大肥鱼」，误触与漏检各几次 |
| 服务 | `preflight/s2s_doubao/` `llm_deepseek/` `asr_volcano/` `tts_edge/` | 这家云能不能连、下行音频、上行节奏 |

报告统一落 `preflight/evidence/` 与各品牌目录的 `evidence/`，均不入库。看报告先看「端点」行：指向 `127.0.0.1` 的是本地 mock 联调，不代表真机通过。

四家服务均已接入运行时代码（S2S 豆包、DeepSeek、火山一句话识别、edge-tts），逐项判据与留证见各自目录的 `验证状态`。

## 通用前置

环境搭建见 [安装](setup.md)，一次即可。每次接入前重做：

| 项 | 做法 |
| --- | --- |
| 配置 | `.env` 只填本轮要测的那组凭据 |
| 体检 | `.venv/bin/python preflight/env-check.py --link s2s` |
| 音频 | 设备写进 `.env`，再用 `bash preflight/audio-check.sh` 验到三项全过 |

## API 检查索引

每个品牌目录自包含：自检脚本、`README.md`（命令与判据）、官方文档、`evidence/`。

- [`s2s_doubao/`](../preflight/s2s_doubao/README.md) — `S2S_DOUBAO`
- [`llm_deepseek/`](../preflight/llm_deepseek/README.md) — `LLM_DEEPSEEK`
- [`asr_volcano/`](../preflight/asr_volcano/README.md) — `ASR_VOLCANO`
- [`tts_edge/`](../preflight/tts_edge/README.md) — `TTS_EDGE`

## 一条链路的最小跑通路径

以 S2S（豆包）为例，**四步四个判据，哪步不过就修哪步，不要跳**：

| # | 命令 | 通过判据 |
| --- | --- | --- |
| ① 机器 | `.venv/bin/python preflight/env-check.py --link s2s` + `bash preflight/audio-check.sh` | env-check 通过，且音频三项全过 |
| ② 连通 | `.venv/bin/python preflight/s2s_doubao/api-check.py` | 收到 `session.created` |
| ③ 下行 | `.venv/bin/python preflight/s2s_doubao/downlink-check.py --play` | 听到合成语音；「句间音量极差」对比 `S2S_DOUBAO_LOUDNESS_NORM` 开/关，「句首 0.6 s 与之后均值差」> 3 dB 说明这句开头偏小 |
| ④ 上行 | `.venv/bin/python preflight/s2s_doubao/upstream-check.py --seconds 10` | 无 `error`，单秒偏帧 ≤ 1 |

四步全过才跑主程序（`<唤醒词>` 见 `config/providers.toml`）：

```bash
.venv/bin/python -m app.main --wake 豆包
```

## 唤醒词自检

待机期由唤醒监听独占麦克风、零云端连接；命中后先把设备交还，再建会话。模型安装见 [安装](setup.md)：

```bash
.venv/bin/python preflight/wake_check.py --seconds 30
```

判据：每个词至少命中 2 次，误触次数记入报告。已实测（2026-11-07，Raspberry Pi 4）：豆包 3/3、大肥鱼 4/4，加载 1~2 s。

## 新增一个模型时

1. 在 `config/providers.toml` 加一条 `[[wakewords]]`（必要时加 `[providers.*]`）。
2. 在 `config/defaults.env` 补该品牌的非凭据默认值（端点、模型名、音色…）。
3. 在 `.env.example` 只加该品牌的 API key 占位。
4. 在 `preflight/<链路>_<品牌>/` 建目录，`README.md` 五节齐全（标题就用这几个词）：
   - `快速验证`：凭据 + 要跑的命令
   - `通过判据`：脚本验了哪几件事、各自判据
   - `适用条件`：出站、设备、时间同步等前置
   - `预检排障`：现象 → 处理
   - `验证状态`：逐项 已通过 / 待跑，通过项写日期与留证
   - 另附自检脚本与官方文档；链路类在「通过判据」之后补一节 `主程序全链路`
5. 在「API 检查索引」加一行链接；报告落本目录 `evidence/`，已被 `.gitignore` 忽略。
6. 自检通过并留证后，才能新增 `app/providers/` 下的适配层。
