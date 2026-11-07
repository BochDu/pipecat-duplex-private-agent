"""静默额度：10 s 默认值、区间夹紧与重试复用同一份额度。"""

import asyncio
import time
from pathlib import Path

import app.asr.volcano as volcano
from app.asr.volcano import VolcanoASR
from app.config import Settings
from app.sessions.llm.pipeline import LLMPipeline
from app.tools import ToolRegistry


def _settings(**common: str) -> Settings:
    return Settings(
        root=Path("."), common={"ASR_VOLCANO_API_KEY": "test-key", **common}
    )


def _asr(**common: str) -> VolcanoASR:
    return VolcanoASR(_settings(**common))


def test_default_is_ten_seconds_for_both_links():
    assert _asr()._wait_sec == 10.0
    assert _llm_pipeline(_settings())._wait_sec() == 10.0


def test_wait_sec_is_clamped():
    assert _asr(SILENCE_TIMEOUT_SEC="999")._wait_sec == 120.0
    assert _asr(SILENCE_TIMEOUT_SEC="1")._wait_sec == 3.0
    assert _llm_pipeline(_settings(SILENCE_TIMEOUT_SEC="0.5"))._wait_sec() == 3.0


def test_remaining_follows_deadline():
    asr = _asr(SILENCE_TIMEOUT_SEC="8")
    assert asr._remaining(None) == 8.0

    future = time.monotonic() + 5
    assert 4.0 < asr._remaining(future) <= 5.0
    assert asr._remaining(time.monotonic() - 1) < 0


def test_spent_budget_skips_recording(monkeypatch):
    calls = []

    class FakeRecorder:
        def __init__(self, device):
            self._device = device

        async def record_utterance(self, **kwargs):
            calls.append(kwargs)
            return b""

    monkeypatch.setattr(volcano, "MicRecorder", FakeRecorder)

    result = asyncio.run(_asr().transcribe(deadline=time.monotonic() - 1))

    assert result == ""
    assert calls == []


def test_retries_share_one_budget(monkeypatch):
    calls = []

    class FakeRecorder:
        def __init__(self, device):
            self._device = device

        async def record_utterance(self, **kwargs):
            calls.append(kwargs)
            return b"\x01\x00" * 320

    async def fake_recognize(audio):
        return ""

    monkeypatch.setattr(volcano, "MicRecorder", FakeRecorder)
    monkeypatch.setattr(volcano, "normalize_pcm16", lambda pcm: pcm)
    monkeypatch.setattr(volcano, "save_debug_wav", lambda audio, path: None)

    asr = _asr(SILENCE_TIMEOUT_SEC="10")
    monkeypatch.setattr(asr, "_recognize", fake_recognize)

    result = asyncio.run(asr.transcribe(deadline=time.monotonic() + 10))

    assert result == ""
    assert len(calls) == volcano.RETRY
    # 每次重试都从同一份 10 s 额度里取，不翻倍
    assert all(0 < call["start_timeout"] <= 10.0 for call in calls)
    assert calls[1]["start_timeout"] <= calls[0]["start_timeout"]


def _llm_pipeline(settings: Settings) -> LLMPipeline:
    return LLMPipeline(
        provider=None,
        settings=settings,
        on_event=lambda event: None,
        asr=None,
        tts=None,
        tools=ToolRegistry(),
    )
