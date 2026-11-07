"""打断词判定：整句命中、多字包含、单字重复三条路径，以及反例。"""

import pytest

from app.providers.volcano_s2s.provider import VolcanoS2SProvider
from app.tools import ToolRegistry


def _provider(stop_words: str = "停,停下,停止,别说了"):
    return VolcanoS2SProvider(
        config={},
        tools=ToolRegistry(),
        on_event=lambda event: None,
        stop_words=stop_words,
    )


@pytest.mark.parametrize("text", ["停", "停。", "停下", "别说了！", "停停停"])
def test_stop_phrases_hit(text):
    assert _provider()._is_stop_word(text) is True


@pytest.mark.parametrize("text", ["停车场在哪", "停车", "不要", "停不下来了"])
def test_normal_questions_do_not_hit(text):
    assert _provider()._is_stop_word(text) is False


def test_without_stop_words_never_hits():
    assert _provider(stop_words="")._is_stop_word("停") is False
