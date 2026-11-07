"""唤醒引擎只保留 kws：默认可用，其它值明确报错。"""

from pathlib import Path

import pytest

from app.config import Settings
from app.wake.factory import build_listener


def _settings(**common: str) -> Settings:
    return Settings(root=Path("."), common=common)


def test_default_engine_is_kws():
    listener = build_listener(_settings(AUDIO_IN_DEVICE="plughw:3,0"), ["豆包"])
    assert type(listener).__name__ == "KwsWakeListener"
    assert listener._device == "plughw:3,0"


def test_vosk_is_rejected():
    with pytest.raises(ValueError, match="kws"):
        build_listener(_settings(WAKEWORD_ENGINE="vosk"), ["豆包"])
