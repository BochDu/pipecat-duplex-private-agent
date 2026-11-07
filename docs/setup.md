# 安装

从零装到能跑。所有命令在仓库根目录执行。可调参数见 [配置参考](config.md)，装好后的更新、重启与排障见 [维护与排障](maintenance.md)。

## 前置

- Python 3.11+
- 麦克风与扬声器各一（USB 会议音箱可同时充当两者）
- 出站网络：火山引擎、DeepSeek

## ① 系统依赖

```bash
sudo apt install -y ffmpeg alsa-utils python3-full python3-venv
which ffmpeg arecord aplay amixer
```

## ② 虚拟环境

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -U pip setuptools wheel
.venv/bin/python -V
```

## ③ 只读子仓库（可选）

`pipecat/` 只作参考实现留档，运行时不 import，也不在 `requirements.txt` 里。要翻源码才初始化：

```bash
git submodule update --init --depth 1 pipecat
ls pipecat/pyproject.toml
```

不初始化不影响安装与运行。

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

注意两点：服务里写的是仓库的**绝对路径**，换目录后要重新装一次；`USB 声卡可能比服务晚就绪`，服务设置了失败 5 秒后自动重启，所以开机时日志里出现一两次重试属正常。

装了服务之后，启动、停止、看日志、更新代码与声音排障都用 [维护与排障](maintenance.md) 里的命令。
