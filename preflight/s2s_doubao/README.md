# S2S_DOUBAO 接入前检查

豆包 Seeduplex 自检：三条预检命令 + 一条主程序命令，约 3 分钟。顺序不要跳——先证明能连上，再证明能听到，然后上行节奏，最后才上麦克风。

## 快速验证

`.env` 里只需有 `S2S_DOUBAO_API_KEY`（音色、端点、音量已由 `config/defaults.env` 提供）。环境与设备见 [安装与排障](../../docs/setup.md)。

```bash
.venv/bin/python preflight/s2s_doubao/api-check.py                   # ① 接口连通
.venv/bin/python preflight/s2s_doubao/downlink-check.py --play       # ② 下行音频（能听到声）
.venv/bin/python preflight/s2s_doubao/upstream-check.py --seconds 10 # ③ 上行规格
```

打印 `结论：通过` 且退出码 0 即通过；报告自动落本目录 `evidence/`（不入库），密钥不进日志。

## 通过判据

| 步骤 | 脚本 | 通过判据 | 报告 |
| --- | --- | --- | --- |
| 接口连通 | `api-check.py` | 收到 `session.created`，`session.id` 非空 | `evidence/api-connect-*.md` |
| 下行音频 | `downlink-check.py` | 收到 `response.output_audio.delta`，时长 ≥ 1 s；`--play` 能听到声音 | `evidence/downlink-*.md` + `.wav` |
| 上行规格 | `upstream-check.py` | 无 `error`，节奏偏差 ≤ 1 帧/s | `evidence/upstream-*.md` + 逐帧 CSV |

`downlink-check.py` 不开麦克风：按协议先发 `input_audio_mute.commit` 保活，再用 `speech_text_buffer.commit` 让服务端直接合成一句话。它只验「收到的音频能不能放出声」，麦克风侧留给第 ③ 步和主程序。

报告里另有两项音量指标，用于对比 `config/defaults.env` 里的响度设置：「句间音量极差」看句与句是否均匀，「句首 0.6 s 与之后均值差」> 3 dB 说明这句开头偏小。

## 主程序全链路（上面三步通过之后）

```bash
.venv/bin/python -m app.main --list       # 「豆包」那行应为 凭据已配置=是
.venv/bin/python -m app.main --wake 豆包   # 单次会话；说完等 SILENCE_TIMEOUT_SEC 自动收尾
.venv/bin/python -m app.main              # 常驻：说「豆包」进会话，Ctrl-C 退出
```

一次会话结束回到待机，再说唤醒词开新会话；同一时刻只有一条会话，切换先停后启。说完一句还没回应就被判停，调 `SILENCE_TIMEOUT_SEC`（见 [配置参考](../../docs/config.md)）。

说一句「你好，你能听到我吗？」，正常日志按这个顺序出现：

```text
豆包会话已建立：session.id=...，音量 140%（loudness +40），响度均衡=开
段首补偿：12 dB / 0.60 s（每场首段）
麦克风开始采集：plughw:3,0（16000 Hz）
检测到说话：RMS 1200（阈值 500）
用户说：你好你能听到我吗
上行 100 帧 / 5.0s：峰值 RMS 1800，当前 RMS 60；下行已播 2.4s
S2S 会话结束（下行共播放 3.1s）
```

| 日志行 | 它证明了什么 |
| --- | --- |
| `检测到说话：RMS ...` | 麦克风有声音且过了阈值 |
| `用户说：...` | 服务端 ASR 真的收到了上行音频——最关键的一行 |
| `下行已播 x.xs` 在增长 | 服务端回了音频，播放器在消费 |
| 一条 `检测到说话` 都没有 | 增益或阈值问题，见 [配置参考](../../docs/config.md) 的会话与判定 |

| 现象 | 处理 |
| --- | --- |
| `播放器启动失败（退出码 ...）` | `AUDIO_OUT_DEVICE` 错，回到 `aplay -l` |
| `arecord 未采到音频（设备 ...）：...` | 设备被占用或名字错；日志里已带 ALSA 原文 |
| 有 `用户说` 但 `下行已播 0.0s` | 音色 ID 无权限或模型没回音频，重跑 `downlink-check.py` |
| 有 `下行已播` 但听不到 | 播放链路问题：`amixer -c 3 sset PCM 100%` 或换 `plughw:2,0` |
| 没说话就结束会话 | `AUDIO_IN_SILENCE_RMS` 偏高，或 `SILENCE_TIMEOUT_SEC` 太短 |
| 它自己打断自己 | 扬声器回采，当前版本无本地 AEC：戴耳机或压低音量 |

## 适用条件

- Linux 主机（含树莓派 / ARM），Python ≥ 3.11，只需出站 443
- `timedatectl | grep -i synchronized` 需为 yes，否则 TLS 失败
- 第 ①② 步不需要麦克风；第 ② 步要能放声

## 预检排障

| 现象 | 处理 |
| --- | --- |
| 401 | 查 API key / 音色 ID |
| 403 | key 未开通豆包实时语音，或该音色无权限 |
| 404 | 查端点 |
| TLS 报错 | 系统时间未同步 |
| `下行音频不足` | 音色 ID 与账号权限不匹配；看报告事件序列里有没有 `response.output_audio.started` |
| 收到了 wav 但没声音 | 换 `--device plughw:N,0`（`aplay -l` 里选），或 `amixer sset PCM 100%` |

## 验证状态

| 检查项 | 状态 |
| --- | --- |
| 接口连通 | 已通过：2026-11-05 留证（树莓派 / Python 3.13.5 / websockets 17.4） |
| 下行音频 | 已通过：2026-11-06，`downlink-check.py --play` 收到音频并放出合成语音 |
| 上行规格 | 已通过：2026-11-06，10 s / 500 帧，最大漂移 2.14 ms，`error: 0` |
| 主程序全链路（麦 → 播放） | 已通过：2026-11-07，`--wake 豆包` 可连续对话，扬声器回声未触发误打断 |
| 保活 / 事件集 / 交互语义 | 脚本待补 |

实测环境：树莓派 / Raspberry Pi OS / Python 3.13.5 / MCP01 USB 声卡（采集与播放同为 `plughw:3,0`，音量控件 `PCM`、采集控件 `Mic`）。

官方依据：本目录 `豆包语音_端到端实时语音（Seeduplex）_1793085585.pdf`，字段与事件以该 PDF 为准。关键约束：上行 20 ms / 640 B 帧、输出 `pcm_s16le` 24 kHz、下行音频字段是 `delta`、不开麦要发 `input_audio_mute.commit` 保活。
