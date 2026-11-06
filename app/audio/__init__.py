"""本地音频 I/O：麦克风采集（arecord）与播放端音量缩放，两条链路共用。"""

from app.audio.chime import chime_pcm, ensure_spoken_chime, play_chime
from app.audio.gain import OnsetBoost, db_to_gain, scale_pcm16
from app.audio.recorder import FRAME_BYTES, FRAME_MS, MicRecorder, rms

__all__ = [
    "FRAME_BYTES",
    "FRAME_MS",
    "MicRecorder",
    "OnsetBoost",
    "db_to_gain",
    "rms",
    "scale_pcm16",
    "chime_pcm",
    "ensure_spoken_chime",
    "play_chime",
]
