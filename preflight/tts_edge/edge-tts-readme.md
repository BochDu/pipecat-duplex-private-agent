# edge-tts 官方文档摘录

> 来源：https://github.com/rany2/edge-tts （README，master 分支，2026-11 摘录）
> 本文件为摘录整理，最新用法以官方仓库为准。

## 项目定位

Python 模块 + 命令行工具，调用 **Microsoft Edge 的在线 TTS 服务**。两个 CLI：

- `edge-tts`：生成语音文件
- `edge-playback`：生成并立即播放（需要 `mpv` 播放器，Windows 除外）

## 安装

```bash
pip install edge-tts      # 作为 Python 模块使用（本项目方式）
pipx install edge-tts     # 仅当命令行工具用时推荐
```

## CLI 用法

```bash
# 生成音频 + 字幕
edge-tts --text "Hello, world!" --write-media hello.mp3 --write-subtitles hello.srt

# 列出所有音色（含名称、性别、语言）
edge-tts --list-voices

# 指定音色
edge-tts --voice zh-CN-XiaoxiaoNeural --text "你好" --write-media hello.mp3

# 语速 / 音量 / 音调（负值必须用等号：--rate=-50%）
edge-tts --rate=-50% --text "Hello" --write-media out.mp3
edge-tts --volume=-50% --text "Hello" --write-media out.mp3
edge-tts --pitch=-50Hz --text "Hello" --write-media out.mp3
```

## Python API

官方示例在仓库 `examples/` 目录，工具函数源码在 `src/edge_tts/util.py`。本项目用法
（见 `app/tts/edge.py`）：

```python
import edge_tts

communicate = edge_tts.Communicate(text, voice)   # voice 如 zh-CN-XiaoxiaoNeural
async for chunk in communicate.stream():
    if chunk["type"] == "audio":
        ...  # chunk["data"] 为音频字节流
```

## 注意事项

1. **自定义 SSML 已被移除**：微软服务端只接受单个 `<speak>` + 单个 `<voice>`，
   任何非 Edge 自身生成的 SSML 都会被拒绝；定制能力由库参数（rate/volume/pitch）提供。
2. **依赖微软在线服务**：需联网，接口行为受微软策略影响，非官方承诺、无 SLA；
   限流或失效时应在 `app/tts/factory.py` 换厂商实现，`.env` 改 `TTS_PROVIDER`。
3. `edge-playback` 播放功能依赖 `mpv`（本项目播放用 ffplay，不依赖此项）。
4. 进度条仅在 stderr 为终端时显示，可用 `--no-progress` 显式关闭。
