# ASR_VOLCANO 接入前检查

火山一句话识别（`bigmodel_nostream`）自检：整句音频上传，返回文本。

## 快速验证

`.env` 里需要 `ASR_VOLCANO_API_KEY`（与豆包 S2S 通常是同一个 key），资源 ID 与端点取 `config/defaults.env`。

```bash
.venv/bin/python preflight/asr_volcano/api-check.py                    # edge-tts 合成测试句喂识别
.venv/bin/python preflight/asr_volcano/api-check.py --file 录音.wav     # 或喂自己的音频（wav/mp3/ogg/pcm）
```

`结论：通过` 即通过，报告落本目录 `evidence/`（密钥已脱敏，同时会把这次送出的音频存成 `test-*.wav`）。

## 通过判据

- 握手成功：响应 `code=0`
- 识别准确：返回非空文本
- 时长达标：`audio_info.duration` 与实际音频时长一致（3.5 s 的音频应返回 3400 ms 左右）

## 协议要点（踩过的坑，改动前先读）

| 项 | 做法 | 错的后果 |
| --- | --- | --- |
| 序号起点 | config 包占 `seq=1`，音频从 `2` 递增 | config 不带序号会被判 `autoAssignedSequence (2) mismatch sequence in request (1)` |
| 末包标志 | `0b0011`（`NEG_WITH_SEQUENCE`）+ 负序号 `-N` | 用 `0b0001` + 负数会判 `19 != -19`；用 `0b0010` 会判 `declared body size does not match`（它表示末包**不带**序号字段） |
| 结束方式 | 最后一包音频直接标末包，不额外发空包 | 多一个空包等于多发一条消息，序号与内容都对不上 |
| 读取方式 | 边发边收，服务端每个音频包回一条 | 只读一条会拿到中间的 `duration=0` 假结果 |
| 连接关闭 | 收流后服务端回 `1000 (OK)` 关闭，视为正常结束 | 当异常抛出会丢掉已收到的全部响应 |
| 音频格式 | 统一转 PCM 16 kHz / 16 bit / 单声道，`format=pcm` | 直接塞 mp3 会解出 0 时长 |
| 分包 | 200 ms 一包 = 6400 B | — |

## 适用条件

- 只需出站 443（`openspeech.bytedance.com`），已在默认白名单；不需要音频设备
- 输入音频统一用 ffmpeg 转成 PCM 16 kHz / 单声道（需要 `ffmpeg`）
- 默认 `ASR_VOLCANO_RESOURCE_ID=volc.seedasr.sauc.duration`（小时版），并发版是另一个 ID
- 协议依据：[豆包语音_一句话识别_1793675403.pdf](豆包语音_一句话识别_1793675403.pdf)，官方 SDK 为 `sauc_python.zip`
- 适配层 `app/asr/volcano/` 与自检同一套实现，`ASR_PROVIDER=volcano` 为默认值

## 预检排障

| 现象 | 处理 |
| --- | --- |
| `HTTP 403` + body 含 `not activated` / `no permission` | 该账号或项目没开通「一句话识别」小时版，去控制台开通 |
| `HTTP 403` + body 含 `resource` | `ASR_VOLCANO_RESOURCE_ID` 与已购规格不符；小时版是 `volc.seedasr.sauc.duration` |
| `HTTP 403` + body 含 `quota` / `exceed` | 额度用尽，控制台看余量 |
| `HTTP 401` | API Key 无效，或不属于开通该服务的项目 |
| `autoAssignedSequence (...) mismatch` | 序号没从 2 起，或 config 包没带 `seq=1` |
| `declared body size does not match` | 末包用了 `0b0010`，改成 `0b0011` |
| 识别文本为空、`duration` 为 0 | 音频格式或分包不对，见「协议要点」 |
| `找不到音频文件` | `--file` 路径不对；不给 `--file` 则用 edge-tts 合成测试句 |
| `未安装 websockets` / `edge-tts` | `.venv/bin/python -m pip install 'websockets>=13.1' edge-tts` |

403 / 401 的报告里带 `logid`，提工单时附上这个值。

## 验证状态

| 检查项 | 状态 |
| --- | --- |
| 连通 / 识别准确 / 密钥脱敏 | 已通过：2026-11-06，3.48 s 合成语音识别为「今天天气真不错，我们出去散步吧。」，`duration=3480ms` |
| `--file` 真实人声 | 已通过：2026-11-06，3.58 s 麦克风录音识别为「喂喂喂，我能问你一个问题吗？」 |
| 主程序全链路（麦 → 文本） | 已通过：2026-11-07，说「大肥鱼」后识别为「电视机和冰箱谁更费电？」 |
