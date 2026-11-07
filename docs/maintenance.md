# 维护与排障

装好之后的日常操作。命令默认在仓库根目录执行（如 `/opt/pipecat-duplex-private-agent`）。

## 更新代码

```bash
git pull --ff-only
.venv/bin/python -m pip install -r requirements.txt   # 仅当 requirements.txt 有变更
sudo systemctl restart pipecat-agent
systemctl status pipecat-agent --no-pager
```

| 判据 | 说明 |
| --- | --- |
| `git pull --ff-only` 无冲突 | 有本地改动先 `git stash`，Pi 上不直接改代码 |
| `status` 为 `active (running)` | 起不来会每 5 秒重试，长期停在 `activating` 就是失败循环 |
| 日志无连续重试 | `journalctl -u pipecat-agent -n 50 --no-pager` |

## 服务管理

| 命令 | 作用 |
| --- | --- |
| `systemctl status pipecat-agent` | 是否在跑 |
| `journalctl -u pipecat-agent -f` | 实时日志（等同前台运行） |
| `sudo systemctl restart pipecat-agent` | 重启，改配置后必做 |
| `sudo systemctl stop pipecat-agent` | 停止 |
| `sudo systemctl disable pipecat-agent` | 取消开机自启 |

前台调试必须先停服务：两者不能同时占用声卡，否则报 `Device or resource busy`。

```bash
sudo systemctl stop pipecat-agent
.venv/bin/python -m app.main
```

换仓库目录或换运行用户后，服务里的绝对路径与 `User=` 失效，按 [安装](setup.md) ⑩ 重装一次再 `daemon-reload`。

## 重建数据库

会话历史损坏、或改了表结构又不想写迁移时，删库重建；`.env` 与模型文件不受影响。

```bash
sudo systemctl stop pipecat-agent
.venv/bin/python -m app.main --reset-db
```

`--reset-db` 删掉 `data/agent.db` 与 WAL 边车文件后正常启动；只想删不启动就 `rm -f data/agent.db*`，下次启动自动建表。正常升级不需要它：表结构按 `PRAGMA user_version` 在启动时自动补齐。

## 改完什么要重启

`.env` 与 `config/` 的键在进程启动时读取一次，以下改动都要 `sudo systemctl restart pipecat-agent` 才生效：

- `.env` 任意键（设备名、凭据、音量、打断阈值）
- 新增依赖（`requirements.txt`）
- 唤醒词模型与 `keywords.txt`
- 代码更新

## 排障

| 现象 | 处理 |
| --- | --- |
| 没声音 / 放到 HDMI 或耳机口 | 设备改成 `plughw:N,0`，重跑 `bash preflight/audio-check.sh` |
| 声音很小 | `amixer -c <card> sget PCM` 看出厂值，`sset PCM 100%` 拉满（`<card>` 用 `aplay -l` 的编号） |
| `Device or resource busy` | 残留的 `arecord` 占着设备：`fuser -v /dev/snd/*` |
| 重启或换 USB 口后设备号变了 | `aplay -l` 取新编号，改 `.env` 的两个 `AUDIO_*_DEVICE` 后重启 |
| 云端连不上 | `.venv/bin/python preflight/env-check.py --link s2s`，再按 [接入前检查](preflight.md) 逐层定位 |
| 唤醒不灵 / 误唤醒 | 阈值调整见 [配置参考](config.md) 的唤醒词一节 |

装不上依赖、缺系统包、`pip check` 报冲突：确认 Python ≥ 3.11，处理完依赖再重启。
