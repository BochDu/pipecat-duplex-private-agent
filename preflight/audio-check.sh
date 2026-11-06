#!/usr/bin/env bash
# 极简音频自检：① 满音量发声 → ② 50% 音量 → ③ 收音
# 同时给出声卡清单与结论：可直接粘进 .env 的 AUDIO_IN_DEVICE / AUDIO_OUT_DEVICE
# 用法：bash preflight/audio-check.sh
# 报告落 preflight/evidence/（不入库）

set -u
set -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" || exit 2
cd "$ROOT" || exit 2
EVID="$ROOT/preflight/evidence"
RUN_ID="$(date +%Y%m%d-%H%M%S)"
REPORT="$EVID/audio-min-${RUN_ID}.md"
TMP="$(mktemp -d /tmp/audio-min.XXXXXX)" || exit 2
mkdir -p "$EVID" || exit 2
chmod 700 "$EVID" 2>/dev/null || true
: > "$REPORT" || exit 2
trap 'rm -rf -- "$TMP"' EXIT

log() { printf '%s\n' "$*" | tee -a "$REPORT"; }
yes() {
  local a
  if [ ! -t 0 ]; then log "非交互运行：该项按未确认处理。"; return 1; fi
  read -r -p "$* [y/N] " a || return 1
  case "$a" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

MISS=""
for c in arecord aplay python3 timeout; do
  command -v "$c" >/dev/null 2>&1 || MISS="$MISS $c"
done
if [ -n "$MISS" ]; then
  log "# 极简音频自检"
  log "缺少依赖：$MISS"
  log "Debian / Ubuntu：sudo apt install -y alsa-utils coreutils python3"
  log "Fedora：sudo dnf install -y alsa-utils coreutils python3"
  printf '缺少依赖:%s\n' "$MISS" >&2
  exit 2
fi

# 每项测试时长（秒）：三次播放 / 录音统一用这个值；设备探测仍用短静音，不计入
SECS=10

log "# 极简音频自检"
log ""
log "- 时间：$(date -Is)　主机：$(hostname)　架构：$(uname -m)"
log "- 顺序：① 满音量发声 → ② 50% 音量 → ③ 收音　每项 ${SECS} 秒"

# 现场合成一段柔和的和弦琶音，振幅由参数控制；不依赖外部音频文件
music() {
  python3 - "$1" "$2" "${3:-$SECS}" <<'PY'
import math, array, wave, sys
rate = 16000
secs = int(sys.argv[3])
amp = float(sys.argv[2])
chords = [
    (261.63, 329.63, 392.00),   # C
    (220.00, 261.63, 329.63),   # Am
    (174.61, 220.00, 261.63),   # F
    (196.00, 246.94, 293.66),   # G
]
step = 0.25
total = int(rate * secs)
out = array.array('h')
for i in range(total):
    t = i / rate
    chord = chords[int(t / 1.0) % len(chords)]
    f = chord[int(t / step) % len(chord)]
    phase = t % step
    env = max(0.0, min(1.0, phase / 0.02) * min(1.0, (step - phase) / 0.06))
    pad = sum(math.sin(2 * math.pi * nf * t) for nf in chord) / 3.0
    s = 0.70 * math.sin(2 * math.pi * f * t) * env + 0.30 * pad
    out.append(int(max(-1.0, min(1.0, s * amp)) * 32767))
if sys.byteorder != 'little':
    out.byteswap()
with wave.open(sys.argv[1], 'wb') as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(out.tobytes())
PY
}

# 按录音峰值自动放大到可听电平（上限 6 倍，带限幅），避免麦克风电平低导致回放听不清
amplify() {
  python3 - "$1" "$2" <<'PY'
import wave, array, sys
with wave.open(sys.argv[1], 'rb') as w:
    n = w.getnframes(); rate = w.getframerate(); raw = w.readframes(n)
buf = array.array('h'); buf.frombytes(raw)
if sys.byteorder != 'little':
    buf.byteswap()
peak = max((abs(x) for x in buf), default=0)
target = int(0.7 * 32767)
gain = min(6.0, target / peak) if peak > 327 else 6.0
out = array.array('h', (max(-32768, min(32767, int(x * gain))) for x in buf))
if sys.byteorder != 'little':
    out.byteswap()
with wave.open(sys.argv[2], 'wb') as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(out.tobytes())
print("peak=%d gain=%.2f" % (peak, gain))
PY
}

MUSIC="$TMP/music.wav"
music "$MUSIC" 1.0 || { log "音乐合成失败"; exit 2; }

CARD_IDS="$( { arecord -l; aplay -l; } 2>/dev/null | grep -oE 'card [0-9]+' | awk '{print $2}' | sort -rn -u )"
# plughw 优先、default 兜底：写进 .env 的应该是明确的设备名
CANDS=""
for c in $CARD_IDS; do CANDS="$CANDS plughw:$c,0"; done
CANDS="$CANDS default"

can_play() {
  timeout 3s aplay -D "$1" -f S16_LE -r 16000 -c 1 -t raw /dev/zero >/dev/null 2>&1
  local r=$?
  [ "$r" -eq 0 ] || [ "$r" -eq 124 ]
}
can_cap() {
  timeout 5s arecord -D "$1" -f S16_LE -r 16000 -c 1 -d 1 "$TMP/probe.wav" >/dev/null 2>&1
}
cap_cards() { arecord -l 2>/dev/null | grep -oE 'card [0-9]+' | awk '{print $2}'; }
play_cards() { aplay -l 2>/dev/null | grep -oE 'card [0-9]+' | awk '{print $2}'; }
card_name() {
  { arecord -l; aplay -l; } 2>/dev/null | grep -E "^card $1:" | head -1 \
    | sed -E 's/^card [0-9]+: ([^ ]+) \[.*/\1/'
}
card_of() { printf '%s' "$1" | sed -nE 's/^plughw:([0-9]+),.*/\1/p'; }

log ""
log "## 声卡清单"
log ""
log "| card | 名称 | 采集 | 播放 | 设备名 |"
log "| --- | --- | --- | --- | --- |"
CAP_LIST="$(cap_cards)"
PLAY_LIST="$(play_cards)"
for c in $CARD_IDS; do
  cap="否"
  play="否"
  printf '%s\n' "$CAP_LIST" | grep -qx "$c" && cap="是"
  printf '%s\n' "$PLAY_LIST" | grep -qx "$c" && play="是"
  log "| $c | $(card_name "$c") | $cap | $play | plughw:$c,0 |"
done

log ""
log "正在探测可用设备（$CANDS）..."
CAP_DEV=""
PLAY_DEV=""
for d in $CANDS; do
  if can_cap "$d" && can_play "$d"; then CAP_DEV="$d"; PLAY_DEV="$d"; break; fi
done
if [ -z "$CAP_DEV" ]; then
  # 没有“既能录又能放”的设备：采集与播放分开找（如 USB 麦克风 + 3.5mm 输出）
  for d in $CANDS; do [ -z "$CAP_DEV" ] && can_cap "$d" && CAP_DEV="$d"; done
  for d in $CANDS; do [ -z "$PLAY_DEV" ] && can_play "$d" && PLAY_DEV="$d"; done
fi
if [ -z "$CAP_DEV" ] || [ -z "$PLAY_DEV" ]; then
  log "未找到可用设备（采集=${CAP_DEV:-无} 播放=${PLAY_DEV:-无}）。设备列表："
  arecord -l 2>&1 | tee -a "$REPORT"
  aplay -l 2>&1 | tee -a "$REPORT"
  log "结论：硬件链路不通，三项均不通过。检查 /dev/snd、USB 声卡连接与用户组（audio）。"
  printf '报告：%s\n结论：未找到可用设备\n' "$REPORT"
  exit 1
fi

PLAY_CARD="$(card_of "$PLAY_DEV")"
CAP_CARD="$(card_of "$CAP_DEV")"
AMIX=""
CAMIX=""
[ -n "$PLAY_CARD" ] && AMIX="-c $PLAY_CARD"
[ -n "$CAP_CARD" ] && CAMIX="-c $CAP_CARD"
VOL_CTRL=""
CAP_CTRL=""
if command -v amixer >/dev/null 2>&1; then
  CTRLS="$(amixer $AMIX scontrols 2>/dev/null | sed -n "s/.*control '\([^']*\)'.*/\1/p")"
  VOL_CTRL="$(printf '%s\n' "$CTRLS" | grep -iE 'PCM|Master|Speaker|Headphone|Playback|Volume' | head -1)"
  CCTRLS="$(amixer $CAMIX scontrols 2>/dev/null | sed -n "s/.*control '\([^']*\)'.*/\1/p")"
  CAP_CTRL="$(printf '%s\n' "$CCTRLS" | grep -iE 'Mic|Capture|Boost|Gain' | head -1)"
fi
set_vol() {
  [ -n "$2" ] || return 1
  amixer $AMIX sset "$2" "$1%" >/dev/null 2>&1
}
set_cap_vol() {
  [ -n "$2" ] || return 1
  amixer $CAMIX sset "$2" "$1%" >/dev/null 2>&1
}

log "选中设备：采集 $CAP_DEV　播放 $PLAY_DEV　音量控件：${VOL_CTRL:-无}　采集控件：${CAP_CTRL:-无}"
log ""
log "设备结论（可直接粘进 .env）："
log ""
log '```env'
log "AUDIO_IN_DEVICE=$CAP_DEV"
log "AUDIO_OUT_DEVICE=$PLAY_DEV"
log '```'
log ""

R_SOUND="不通过"
R_VOL="不通过"
R_REC="不通过"

log "## ① 满音量发声（能发声）"
set_vol 100 "$VOL_CTRL" || true
log "播放控件设 100%，播放 ${SECS} 秒满音量合成和弦片段..."
aplay -D "$PLAY_DEV" "$MUSIC" >/dev/null 2>&1
P1=$?
log "aplay 退出码 $P1"
if [ "$P1" -eq 0 ] && yes "听到音乐了吗？"; then R_SOUND="通过"; fi
log "结果：$R_SOUND"

log ""
log "## ② 50% 音量（能调节音量）"
if [ -n "$VOL_CTRL" ] && set_vol 50 "$VOL_CTRL"; then
  HALF="$MUSIC"
  MODE="播放控件 100% → 50%"
else
  HALF="$TMP/music_half.wav"
  music "$HALF" 0.5 || { log "50% 版本合成失败，改用满幅文件"; HALF="$MUSIC"; }
  MODE="软件振幅 1.0 → 0.5（未找到硬件音量控件）"
fi
log "再播一次（$MODE）..."
aplay -D "$PLAY_DEV" "$HALF" >/dev/null 2>&1
P2=$?
set_vol 100 "$VOL_CTRL" || true
log "aplay 退出码 $P2"
if [ "$P2" -eq 0 ] && yes "这次音量是否明显比第一次小（约一半）？"; then R_VOL="通过"; fi
log "结果：$R_VOL"

log ""
log "## ③ 收音（能收音）"
set_cap_vol 70 "$CAP_CTRL" || true
log "录 ${SECS} 秒，请对着麦克风说一句话，随后自动放大并回放..."
arecord -D "$CAP_DEV" -f S16_LE -r 16000 -c 1 -d "$SECS" "$TMP/rec.wav" >/dev/null 2>&1
R1=$?
LEVEL="$(python3 - "$TMP/rec.wav" <<'PY'
import sys, wave, struct, math
try:
    with wave.open(sys.argv[1], 'rb') as w:
        n = w.getnframes(); raw = w.readframes(n)
    s = struct.unpack('<%dh' % n, raw)
    peak = max((abs(x) for x in s), default=0)
    dbfs = 20 * math.log10(peak / 32768.0) if peak else -120.0
    print("peak=%.1f dBFS verdict=%s" % (dbfs, "OK" if peak > 327 else "SILENT"))
except Exception as e:
    print("读取失败: %s" % e)
PY
)"
AMP_INFO="$(amplify "$TMP/rec.wav" "$TMP/rec_amp.wav")" || AMP_INFO="放大失败"
set_vol 100 "$VOL_CTRL" || true
aplay -D "$PLAY_DEV" "$TMP/rec_amp.wav" >/dev/null 2>&1
P3=$?
log "arecord 退出码 $R1；aplay 退出码 $P3；原始电平：$LEVEL；回放放大：$AMP_INFO"
if [ "$R1" -eq 0 ] && [ "$P3" -eq 0 ] && printf '%s' "$LEVEL" | grep -q 'OK' \
   && yes "放大后的回放里听到自己的声音了吗？"; then R_REC="通过"; fi
log "结果：$R_REC"

log ""
log "| 步骤 | 项 | 结果 |"
log "| --- | --- | --- |"
log "| ① | 能发声 | $R_SOUND |"
log "| ② | 能调节音量 | $R_VOL |"
log "| ③ | 能收音 | $R_REC |"
log ""
log "设备：采集 $CAP_DEV / 播放 $PLAY_DEV"
log ""
if [ "$R_SOUND" = "通过" ] && [ "$R_VOL" = "通过" ] && [ "$R_REC" = "通过" ]; then
  log "**结论：三项全部通过**"
  printf '\n报告：%s\n结论：三项全部通过\n' "$REPORT"
  exit 0
fi
log "**结论：存在不通过项**"
printf '\n报告：%s\n结论：存在不通过项\n' "$REPORT"
exit 1
