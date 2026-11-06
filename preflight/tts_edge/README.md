# TTS_EDGE 接入前检查

edge-tts 合成自检，云、免费、无需凭据。

## 快速验证

```bash
.venv/bin/python preflight/tts_edge/api-check.py            # 验合成
.venv/bin/python preflight/tts_edge/api-check.py --play     # 合成 + 播放，人耳确认
```

`结论：通过` 即通过，报告落本目录 `evidence/`。

## 通过判据

- 合成：收到音频数据，MP3 ≥ 5 KB
- 播放（`--play`）：能听到、无爆音

## 适用条件

- 只需出站 443（`speech.platform.bing.com`），已在默认白名单
- 音色取 `config/defaults.env` 的 `TTS_VOICE`
- 非官方免费接口、无 SLA；换厂商改 `app/tts/factory.py` 与 `TTS_PROVIDER`
- 官方文档：[edge-tts-readme.md](edge-tts-readme.md)

## 预检排障

| 现象 | 处理 |
| --- | --- |
| `未安装 edge-tts` | `.venv/bin/python -m pip install edge-tts` |
| 合成超时 | 查出站 443；免费接口可能限流，稍后重试 |
| `--play` 无声 | `ffplay` 缺失（`apt install ffmpeg`）或输出设备不对，先跑 `bash preflight/audio-check.sh` |

## 验证状态

| 检查项 | 状态 |
| --- | --- |
| 合成 | 已通过：2026-11-06（树莓派） |
| `--play` 播放 | 已通过：2026-11-06，主程序播报「我在，请说」与回答均可听到（aplay 直出 `plughw:3,0`） |
