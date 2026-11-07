"""打断状态机：正常打断、冷却拦截、未在播、未运行四条路径。"""

import asyncio
import time
from pathlib import Path

from app.config import Settings
from app.sessions.s2s.pipeline import S2SPipeline


class FakePlayer:
    """只保留打断用到的 terminate()；进程退出后 returncode 变成非空。"""

    stdin = None

    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15


class FakeProvider:
    current_response_id = "resp-1"
    last_downlink_at = 0.0
    loudness_percent = 100

    def __init__(self) -> None:
        self.cancelled = 0

    async def cancel_response(self) -> None:
        self.cancelled += 1


def _playing_pipeline() -> S2SPipeline:
    pipeline = S2SPipeline(
        provider=FakeProvider(),
        settings=Settings(root=Path("."), common={}),
        on_event=lambda event: None,
    )
    pipeline._running = True
    pipeline._written_total = 24000 * 2  # 还有 1 s 没放完
    pipeline._play_started_at = time.monotonic()
    pipeline._player = FakePlayer()
    return pipeline


def test_interrupt_stops_player_and_cancels_response():
    async def run() -> None:
        pipeline = _playing_pipeline()
        assert pipeline.interrupt("本地起音") is True
        assert pipeline._player.terminated is True
        assert pipeline._epoch == 1
        assert pipeline._respawn is True
        await asyncio.gather(*pipeline._cancel_tasks)
        assert pipeline._provider.cancelled == 1

    asyncio.run(run())


def test_second_interrupt_in_cooldown_is_ignored():
    async def run() -> None:
        pipeline = _playing_pipeline()
        assert pipeline.interrupt("本地起音") is True
        assert pipeline.interrupt("服务端开口") is False
        await asyncio.gather(*pipeline._cancel_tasks)

    asyncio.run(run())


def test_interrupt_without_playback_does_nothing():
    async def run() -> None:
        pipeline = _playing_pipeline()
        pipeline._written_total = 0  # 已经放完
        assert pipeline.interrupt("本地起音") is False
        assert pipeline._player.terminated is False

    asyncio.run(run())


def test_interrupt_when_idle_does_nothing():
    async def run() -> None:
        pipeline = _playing_pipeline()
        pipeline._running = False
        assert pipeline.interrupt("本地起音") is False

    asyncio.run(run())
