"""异常路径的资源回收：播放器重建、会话释放、TTS 子进程。"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.sessions.s2s import pipeline as pipeline_module
from app.sessions.s2s.pipeline import S2SPipeline
from app.tts import edge as edge_module
from app.wake_router import SessionState, WakeRouter


class FakeStream:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, chunk: bytes) -> None:
        self.data.extend(chunk)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    async def read(self, size: int) -> bytes:
        return b""


class FakeProcess:
    def __init__(self) -> None:
        self.pid = 0
        self.stdin = FakeStream()
        self.stdout = FakeStream()
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int | None:
        return self.returncode


def _pipeline() -> S2SPipeline:
    pipeline = S2SPipeline(
        provider=SimpleNamespace(),
        settings=Settings(root=Path("."), common={}),
        on_event=lambda event: None,
    )
    pipeline._running = True
    pipeline._audio_queue = asyncio.Queue()
    return pipeline


def test_playback_closes_reopened_player_when_session_ends(monkeypatch):
    async def run() -> None:
        pipeline = _pipeline()
        spawned: list[FakeProcess] = []
        closed: list[object] = []

        async def fake_spawn(device, *, settle=0.0):
            player = FakeProcess()
            spawned.append(player)
            pipeline._running = False  # 模拟重建期间会话结束
            return player

        async def fake_prime(proc, ms):
            return None

        async def fake_close(proc):
            closed.append(proc)

        monkeypatch.setattr(pipeline_module, "spawn_player", fake_spawn)
        monkeypatch.setattr(pipeline_module, "prime_player", fake_prime)
        monkeypatch.setattr(pipeline_module, "close_player", fake_close)

        pipeline._respawn = True
        await pipeline._playback()

        assert len(spawned) == 1
        assert closed[-1] is spawned[0]
        assert pipeline._player is None

    asyncio.run(run())


class BrokenPipeline:
    async def stop(self) -> None:
        raise RuntimeError("编排停止失败")


class FakeProvider:
    def __init__(self) -> None:
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


def test_release_always_stops_provider_and_returns_to_idle():
    async def run() -> None:
        router = WakeRouter.__new__(WakeRouter)
        provider = FakeProvider()
        revoked: list[str] = []
        router._provider = provider
        router._pipeline = BrokenPipeline()
        router._session_id = "volcano_s2s:豆包"
        router._state = SessionState.IN_SESSION
        router._ctx = SimpleNamespace(tools=SimpleNamespace(revoke=revoked.append))

        await router._release()

        assert provider.stopped is True
        assert revoked == ["volcano_s2s:豆包"]
        assert router._state == SessionState.IDLE
        assert router._provider is None
        assert router._pipeline is None

    asyncio.run(run())


class FailingCommunicate:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def stream(self):
        raise RuntimeError("合成失败")
        yield b""  # 让 stream 成为异步生成器，首次迭代即抛错


def test_tts_reaps_child_processes_when_stream_fails(monkeypatch):
    async def run() -> None:
        created = [FakeProcess(), FakeProcess()]
        pending = list(created)

        async def fake_exec(*args, **kwargs):
            return pending.pop(0)

        monkeypatch.setattr(edge_module.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setitem(
            sys.modules, "edge_tts", SimpleNamespace(Communicate=FailingCommunicate)
        )

        tts = edge_module.EdgeTTS()
        with pytest.raises(RuntimeError, match="合成失败"):
            await tts.speak("你好")

        assert all(proc.terminated for proc in created)
        assert tts._procs == []

    asyncio.run(run())
