"""配置三层覆盖、取值器夹区间与设备回退链。"""

from pathlib import Path

from app.config import Settings


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_env_overrides_defaults(tmp_path, monkeypatch):
    _write(tmp_path, "config/defaults.env", "AUDIO_IN_SILENCE_RMS=900\nWAKE_CHIME=1\n")
    _write(tmp_path, ".env", "AUDIO_IN_SILENCE_RMS=700\n")
    monkeypatch.setenv("WAKE_CHIME", "0")

    settings = Settings.load(tmp_path)

    assert settings.num("AUDIO_IN_SILENCE_RMS", 500.0) == 700.0
    assert settings.flag("WAKE_CHIME") is False


def test_getters_fall_back_and_clamp(tmp_path):
    _write(
        tmp_path,
        "config/defaults.env",
        "AUDIO_OUT_VOLUME=500\nSILENCE_TIMEOUT_SEC=abc\n",
    )

    settings = Settings.load(tmp_path)

    assert settings.out_volume == 200
    assert settings.num("SILENCE_TIMEOUT_SEC", 10.0, lo=3.0, hi=120.0) == 10.0


def test_device_fallback_chain(tmp_path):
    _write(tmp_path, "config/defaults.env", "AUDIO_DEVICE=plughw:2,0\n")

    settings = Settings.load(tmp_path)

    assert settings.in_device == "plughw:2,0"
    assert settings.out_device == "plughw:2,0"

    settings.common["AUDIO_IN_DEVICE"] = "plughw:3,0"
    assert settings.in_device == "plughw:3,0"
    assert settings.out_device == "plughw:2,0"
