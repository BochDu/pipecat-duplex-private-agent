# 全双工私有语音智能体

Linux 主机上的个人实时语音智能体：本地数据与编排私有可控、S2S 链路可随时打断。本地工具、笔记、定时任务与推送属阶段 5，当前工具池为空。

## 快速开始

```bash
git clone https://github.com/BochDu/pipecat-duplex-private-agent.git
cd pipecat-duplex-private-agent

# 1. 系统依赖与虚拟环境
sudo apt install -y ffmpeg alsa-utils python3-full python3-venv
python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -r requirements.txt

# 2. 配置：只填三个 key
cp .env.example .env && chmod 600 .env
nano .env

# 3. 体检
.venv/bin/python preflight/env-check.py --link s2s
bash preflight/audio-check.sh

# 4. 启动
.venv/bin/python -m app.main
```

`.env` 只需要这三行：

```env
S2S_DOUBAO_API_KEY=...
LLM_DEEPSEEK_API_KEY=...
ASR_VOLCANO_API_KEY=...
```

音色、音量、唤醒、超时、白名单等默认值都在 [config/defaults.env](config/defaults.env)，随代码入库、已调好；要改就在 `.env` 写一行同名键覆盖。

**设备通常要自己写**：`default` 在多数 USB 声卡上不支持采集。先跑第 3 步的 `audio-check.sh`，把它报告里「设备结论」那两行抄进 `.env`：

```env
AUDIO_IN_DEVICE=plughw:3,0
AUDIO_OUT_DEVICE=plughw:3,0
```

启动后待机，说「豆包」或「大肥鱼」进会话。

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `.venv/bin/python -m app.main --list` | 看唤醒词、配置组与凭据是否就位 |
| `.venv/bin/python -m app.main --wake 豆包` | 不喊唤醒词，直接进一次会话 |
| `.venv/bin/python -m app.main --volume 60` | 本次运行的播放端音量 |
| `.venv/bin/python -m app.main --reset-db` | 删库重建（会话历史清空，配置不动） |

## 开发

```bash
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m ruff check app tests
```

## 文档

- [安装](docs/setup.md)：从零装到能跑
- [维护与排障](docs/maintenance.md)：更新代码、服务管理、改配置后重启、声音问题
- [配置参考](docs/config.md)：音量、唤醒、会话参数与覆盖规则
- [接入前检查](docs/preflight.md)：自检体系与新增模型流程
- [技术方案](docs/architecture.md)：架构、目录规范、协议约束
- [阶段计划](docs/plan.md)
- [项目提案](docs/proposal.md)
- [项目复盘](docs/retrospective.md)：设计取舍、接口选型与调优历程

## 许可证

[MIT](LICENSE)
