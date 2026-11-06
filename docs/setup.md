# 安装与排障

从零装到能跑。所有命令在仓库根目录执行。可调参数见 [配置参考](config.md)。

## 前置

- Python 3.11+
- 麦克风与扬声器各一（USB 会议音箱可同时充当两者）
- 出站网络：火山引擎、DeepSeek

## ① 系统依赖

```bash
sudo apt install -y ffmpeg alsa-utils python3-full python3-venv
which ffmpeg ffplay arecord aplay amixer
```

## ② 虚拟环境

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -U pip setuptools wheel
.venv/bin/python -V
```

## ③ 只读子仓库

```bash
git submodule update --init --depth 1 pipecat
ls pipecat/pyproject.toml
```

只跑 S2S（豆包）可跳过：链路不经过 pipecat，也不需要 `-e ./pipecat`。

## ④ 依赖安装

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
```

只跑 S2S 的最小依赖：

```bash
.venv/bin/python -m pip install "websockets>=13.1"
```

## ⑤ 音频设备

`default` 在多数 USB 声卡上打不开采集，必须显式写设备名。`arecord -l` 列采集设备，`aplay -l` 列播放设备，设备名写成 `plughw:<card>,<device>`：

```bash
arecord -l
aplay -l
```

| 输出里的 | 设备名 | 说明 |
| --- | --- | --- |
| `card 3: MCP01 [USB Audio]` | `plughw:3,0` | USB 麦克风 / 会议音箱，采集与播放通常都是它 |
| `card 2: Headphones [bcm2835 Headphones]` | `plughw:2,0` | 树莓派 3.5mm 耳机口，通常只能放 |
| `card 0/1: vc4hdmi*` | 不要用 | HDMI 输出，不接显示器就没有声 |

card 编号以命令输出为准（换 USB 口或重启后可能变）。能不能录、能不能放由下一个体检步骤统一验，不在别处重复。

## ⑥ 配置

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

只填两个 key，其余默认值已随代码入库：

```env
S2S_DOUBAO_API_KEY=...
LLM_DEEPSEEK_API_KEY=...
```

设备名几乎都要自己写（`default` 在多数 USB 声卡上打不开采集），照 ⑤ 或下一个体检步骤的报告填：

```env
AUDIO_IN_DEVICE=plughw:3,0
AUDIO_OUT_DEVICE=plughw:3,0
```

## ⑦ 体检

```bash
.venv/bin/python preflight/env-check.py --link s2s   # python / 依赖 / 时间同步 / 注册表 / 凭据 / 设备名
bash preflight/audio-check.sh                        # 真录真放，人工确认
.venv/bin/python -m app.main --list                  # 唤醒词、配置组、凭据是否就位
```

`env-check.py` 不通过项按报告里的「提示」列修完重跑；`audio-check.sh` 是音频验收的唯一入口，报告里的「设备结论」可直接抄进 `.env`。

## ⑧ 唤醒词模型（默认引擎 `kws`，约 5 分钟）

```bash
.venv/bin/python -m pip install sherpa-onnx

mkdir -p data/models/kws && cd data/models/kws
aria2c -c -x 16 -s 16 "https://gh-proxy.com/https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2"
tar xf *.tar.bz2 --strip-components=1 && rm -f *.tar.bz2
cd ../../..

printf 'd òu b āo @豆包\nd à f éi y ú @大肥鱼\n' > data/models/kws/keywords.txt

.venv/bin/python preflight/wake_check.py --seconds 30
```

模型目录、关键词路径、灵敏度都在 [配置参考](config.md)，一般不用改。

## ⑨ 启动

```bash
.venv/bin/python -m app.main
```

待机后说「豆包」或「大肥鱼」进会话。

## ⑩ 开机自启（可选）

仓库自带 [deploy/pipecat-agent.service](../deploy/pipecat-agent.service)，装成 systemd 服务后开机自动进入待机：

```bash
cd /opt/pipecat-duplex-private-agent
sudo sed -e "s|__USER__|$(id -un)|g" -e "s|__REPO__|$PWD|g" \
  deploy/pipecat-agent.service | sudo tee /etc/systemd/system/pipecat-agent.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now pipecat-agent
```

常用命令：

```bash
systemctl status pipecat-agent       # 是否在跑
journalctl -u pipecat-agent -f       # 实时看日志（等同前台运行）
sudo systemctl stop pipecat-agent    # 停止
sudo systemctl disable pipecat-agent # 取消开机自启
```

注意两点：服务里写的是仓库的**绝对路径**，换目录后要重新装一次；`USB 声卡可能比服务晚就绪`，服务设置了失败 5 秒后自动重启，所以开机时日志里出现一两次重试属正常。

## 跑不起来时

| 现象 | 处理 |
| --- | --- |
| 没声音 / 放到 HDMI 或耳机口 | 设备改成 `plughw:N,0`，重跑 `bash preflight/audio-check.sh` |
| 声音很小 | `amixer -c 3 sget PCM` 看出厂值，`sset PCM 100%` 拉满 |
| `Device or resource busy` | 残留的 `arecord` 占着设备：`fuser -v /dev/snd/*` |
| 云端连不上 | `.venv/bin/python preflight/env-check.py --link s2s`，再按 [接入前检查](preflight.md) 逐层定位 |
| 唤醒不灵 / 误唤醒 | 阈值调整见 [配置参考](config.md) 的唤醒词一节 |

装不上依赖、缺系统包、`pip check` 报冲突时，先确认 Python ≥ 3.11 且子仓库已初始化。
