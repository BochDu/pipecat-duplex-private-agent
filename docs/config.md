# 配置参考

`.env` 只放凭据；调好的默认值在 `config/defaults.env`，随代码入库。

覆盖顺序：`config/defaults.env` < `.env` < 真实环境变量。在 `.env` 写一行同名键即可覆盖；**留空等于覆盖成空值**，不用的项请注释掉而不是留空。

## 设备

| 键 | 缺省 | 说明 |
| --- | --- | --- |
| `AUDIO_IN_DEVICE` | `default` | 采集设备，写 `plughw:<card>,<device>` |
| `AUDIO_OUT_DEVICE` | `default` | 播放设备，同上 |

设备名用 `arecord -l` / `aplay -l` 查，`bash preflight/audio-check.sh` 的报告里也可直接照抄。

## 音量与响度

单位统一为百分比，`100` 为原始音量。

| 键 | 作用层 | 范围 | 缺省 |
| --- | --- | --- | --- |
| `S2S_DOUBAO_LOUDNESS` | 豆包服务端合成端 | 0~200（200 = 官方上限 2 倍） | 140 |
| `S2S_DOUBAO_LOUDNESS_NORM` | 豆包服务端响度均衡 | 1 开 / 0 关 | 1 |
| `S2S_DOUBAO_ONSET_BOOST_DB` | 段首补偿（开头能量偏低） | 0~24，0 关 | 12 |
| `S2S_DOUBAO_ONSET_SEC` | 段首补偿时长 | 0~4 秒 | 0.6 |
| `S2S_DOUBAO_ONSET_EVERY` | 补偿范围 | 0 只补首段 / 1 逐段 | 0 |
| `AUDIO_OUT_VOLUME` | 本地播放端（所有链路 + 提示音） | 0~200（>100 才软件放大） | 100 |

**响度稳定**由 `LOUDNESS_NORM` 压平句与句之间的差；**段首偏弱**由 `ONSET_BOOST_DB` / `ONSET_SEC` 抬升：前 70% 时间保持增益、余下线性回落。整体响度交给音响按键，两层同时 >100 会叠乘，会话启动日志会告警。

## 唤醒词

| 键 | 缺省 | 说明 |
| --- | --- | --- |
| `WAKEWORD_ENGINE` | `kws` | `kws` 关键词检测 / `vosk` 离线 ASR |
| `WAKEWORD_KWS_DIR` | `data/models/kws` | kws 模型目录 |
| `WAKEWORD_KWS_KEYWORDS` | `data/models/kws/keywords.txt` | 关键词表，拼音 token + `@` + 中文 |
| `WAKEWORD_KWS_THRESHOLD` | `0.25` | 越低越易触发 |
| `WAKEWORD_KWS_SCORE` | `1.0` | 越高越易触发 |
| `WAKEWORD_KWS_THREADS` | `2` | 树莓派 4 核可设 4 |

| 现象 | 调整 |
| --- | --- |
| 叫不醒 | `WAKEWORD_KWS_THRESHOLD` 0.25 → 0.15 |
| 误唤醒多 | `WAKEWORD_KWS_THRESHOLD` 0.25 → 0.4 |

加唤醒词：在 `keywords.txt` 追加一行，拼音 token 必须存在于模型自带 `tokens.txt`（带声调）。

```
d òu b āo @豆包
d à f éi y ú @大肥鱼
```

备选引擎 `vosk`（离线 ASR + 文本匹配，自造词识别不准）：`WAKEWORD_ENGINE=vosk`，模型放 `data/models/wakeword`。会话期间不监听唤醒词；换引擎必须结束当前会话再唤醒。

## 会话与判定

| 键 | 缺省 | 说明 |
| --- | --- | --- |
| `SILENCE_TIMEOUT_SEC` | `20` | 静默多久结束会话，两条链路共用：豆包按“双方都安静”累计，三段式是等下句的时长 |
| `AUDIO_IN_SILENCE_RMS` | `900` | 说话判定阈值；底噪约 200~300、人声 600+，环境吵时再往上调 |
| `ASR_MAX_UTTERANCE_SEC` | `15` | 单句最长录制，超时强制送出识别 |
| `WAKE_CHIME` | `1` | 唤醒后播提示音 |

## 打断

S2S 播报中检测到人声连续 250 ms 过阈值，就清空待播队列、结束 aplay 并让服务端取消这一轮回答；服务端识别出用户开口时同样停播。用持续时长而不是帧数，吸气、爆音、回采残响过不了门限。停声延迟取决于本地检测，不取决于服务端判停。

| 键 | 缺省 | 说明 |
| --- | --- | --- |
| `S2S_BARGE_IN` | `1` | 本地起音打断开关；0 只按服务端开口事件停播 |
| `S2S_BARGE_IN_RMS` | `1200` | 打断阈值；必须高于 `AUDIO_IN_SILENCE_RMS`，否则扬声器回采会自己打断自己 |
| `S2S_STOP_WORDS` | `停,停下,停止,别说了,不要说了,闭嘴,打住` | 短句（≤6 字）命中就整轮不播报，直到用户下次开口 |

打断词只按“整句等于该词”“多字词包含”“单字词重复”三种情况命中，正常提问里出现的同字不触发。误打断就调高 `S2S_BARGE_IN_RMS` 或把 `S2S_BARGE_IN` 置 0，不要靠加长 250 ms 门限——那等于直接增加停声延迟。外放场景的残响会抬高持续 RMS，先处理回声（耳机、降 `S2S_DOUBAO_LOUDNESS`、AEC），再抬阈值。

## 豆包 S2S

| 键 | 缺省 |
| --- | --- |
| `S2S_DOUBAO_MODEL` | `1.2.6.1` |
| `S2S_DOUBAO_VOICE` | `zh_female_vv_jupiter_bigtts`（音色 ID，不是音色名） |
| `S2S_DOUBAO_WS_URL` | 官方实时对话端点 |
| `S2S_DOUBAO_GREETING` | `我在，请说`；注释掉则不主动开口 |

## 三段式

| 键 | 缺省 | 说明 |
| --- | --- | --- |
| `ASR_PROVIDER` | `volcano` | 火山一句话识别（麦克风整句），需要 `ASR_VOLCANO_API_KEY` |
| `ASR_SILENCE_MS` | `1200` | 一句说完后的静音判停（300~3000）；太短会把句子切碎 |
| `TTS_PROVIDER` | `edge` | edge-tts，免费无需凭据 |
| `TTS_VOICE` | `zh-CN-XiaoxiaoNeural` | |
| `LLM_GREETING` | 空 | 唤醒后先说一句；留空不播（最快），填短句会多一次合成 |
| `LLM_DEEPSEEK_MODEL` | `deepseek-chat` | |
| `LLM_DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | |
| `LLM_SYSTEM_PROMPT` | 简洁口语助手 | 系统提示词 |

## 出站与路径

| 键 | 缺省 |
| --- | --- |
| `OUTBOUND_ALLOWLIST` | `openspeech.bytedance.com,api.deepseek.com,speech.platform.bing.com` |
| `DATA_DIR` | `data` |
| `SCRIPTS_DIR` | `scripts` |
| `NOTES_DIR` / `ALLOWED_READ_DIRS` | 空（不启用） |

新增品牌时，非凭据默认值写进 `config/defaults.env`，凭据占位写进 `.env.example`。
