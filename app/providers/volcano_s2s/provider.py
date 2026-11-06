"""豆包 Seeduplex 适配层。

协议依据 `preflight/s2s_doubao/`：`session.create` 建连，
`input_audio_buffer.append`（Base64 PCM16/16 kHz/20 ms）上行，
`response.output_audio.delta`（Base64 PCM16/24 kHz）下行。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import struct
from collections.abc import Callable

from app.audio.gain import MAX_BOOST_DB, OnsetBoost
from app.config import Settings, WakewordEntry
from app.events import SessionEvent
from app.providers.base import BaseProvider, ProviderError
from app.tools import ToolRegistry

logger = logging.getLogger(__name__)

SEND_TIMEOUT = 5.0  # 单次上行发送；链路半死时不会无限等
RECV_TIMEOUT = 120.0  # 下行空闲上限，超过即视为断连
CLOSE_TIMEOUT = 3.0
OUTPUT_RATE = 24000  # 下行 PCM16 采样率，协议固定
SEG_LOG_SEC = 0.25  # 段首电平日志的窗口长度
SEG_LOG_WINDOWS = 8  # 只记每段开头这么多窗


def _window_db(mean_square: float) -> float:
    """均方值 → dBFS；全静音按 -120 记。"""
    if mean_square <= 0:
        return -120.0
    return 20.0 * math.log10(math.sqrt(mean_square) / 32768.0)


class VolcanoS2SProvider(BaseProvider):
    link = "s2s"

    def __init__(
        self,
        *,
        config: dict[str, str],
        tools: ToolRegistry,
        on_event: Callable[[SessionEvent], None],
        on_audio: Callable[[bytes], None] | None = None,
    ) -> None:
        super().__init__(config=config, tools=tools, on_event=on_event, on_audio=on_audio)
        self._ws = None
        self._receiver: asyncio.Task | None = None
        self._running = False
        self._session_id = ""
        self._seq = 0
        self._tool_defs: list[dict] = []
        self._onset = OnsetBoost(
            boost_db=self._onset_boost_db(),
            seconds=self._onset_sec(),
            rate=OUTPUT_RATE,
        )
        self._onset_used = False  # 本场会话是否已补偿过一段
        self._seg_window = int(OUTPUT_RATE * SEG_LOG_SEC)
        self._seg_raw: list[float] = []
        self._seg_out: list[float] = []
        self._seg_acc_raw = 0.0
        self._seg_acc_out = 0.0
        self._seg_count = 0

    async def start(self) -> None:
        key = self.config.get("API_KEY", "")
        voice = self.config.get("VOICE", "")
        model = self.config.get("MODEL", "")
        url = self.config.get("WS_URL", "")
        if not key or not voice or not url:
            raise ProviderError("S2S_DOUBAO 缺少 API_KEY / VOICE / WS_URL")
        if not key.isascii() or not voice.isascii():
            # 头字段只能是 ASCII，中文占位符在这里会变成晦涩的编码错误
            raise ProviderError(
                "API_KEY / VOICE 含非 ASCII 字符：检查 .env 是否把占位符或说明一起粘进去了"
            )

        try:
            from websockets.asyncio.client import connect
        except ModuleNotFoundError as exc:
            raise ProviderError("未安装 websockets：pip install websockets") from exc

        self._ws = await connect(
            url,
            additional_headers={"X-Api-Key": key},
            open_timeout=10,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=CLOSE_TIMEOUT,
        )
        output: dict = {
            "format": {"type": "pcm_s16le", "rate": 24000},
            "voice": voice,
        }
        offset = self._loudness_offset()
        if offset:
            output["loudness"] = offset
        session: dict = {
            "model": model or "1.2.6.1",
            "audio": {
                "input": {"format": {"type": "pcm", "rate": 16000}},
                "output": output,
            },
        }
        if self._tool_defs:
            session["tools"] = self._tool_defs
        dialog: dict = {}
        if self._loudness_norm():
            dialog["enable_loudness_norm"] = True
        await self._ws.send(
            json.dumps(
                {
                    "type": "session.create",
                    "session": session,
                    "extension": {"dialog": dialog},
                }
            )
        )

        for _ in range(20):
            event = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=10))
            etype = event.get("type", "")
            if etype == "session.created":
                session = event.get("session") or {}
                self._session_id = session.get("id", "") or event.get("id", "")
                break
            if etype == "error":
                raise ProviderError(f"建连失败：{event.get('error') or event}")
        else:
            raise ProviderError("未收到 session.created")

        self._running = True
        logger.info(
            "豆包会话已建立：session.id=%s，音量 %d%%（loudness %+d），响度均衡=%s",
            self._session_id or "(空)",
            self.loudness_percent,
            offset,
            "开" if self._loudness_norm() else "关",
        )
        if self._onset.enabled:
            logger.info(
                "段首补偿：%.0f dB / %.2f s（%s）",
                self._onset_boost_db(),
                self._onset_sec(),
                "每段" if self._onset_every() else "每场首段",
            )
        self._receiver = asyncio.create_task(self._receive_loop())
        await self._send_greeting()

    @property
    def loudness_percent(self) -> int:
        """服务端音量百分比 0~200：100 为原始，缺省不额外放大。"""
        raw = (self.config.get("LOUDNESS", "") or "").strip()
        try:
            percent = int(float(raw)) if raw else 100
        except ValueError:
            percent = 100
        return max(0, min(200, percent))

    def _loudness_offset(self) -> int:
        """换算成官方 `loudness`：-50~100，100 为 2 倍；0 表示不下发。"""
        return max(-50, min(100, self.loudness_percent - 100))

    def _loudness_norm(self) -> bool:
        """服务端响度均衡：压平句子之间的音量差，缺省开启。"""
        raw = (self.config.get("LOUDNESS_NORM", "") or "").strip().lower()
        return raw not in ("0", "false", "off", "no")

    def _onset_boost_db(self) -> float:
        """段首补偿的分贝数；0 关闭，缺省 12。"""
        raw = (self.config.get("ONSET_BOOST_DB", "") or "").strip()
        try:
            value = float(raw) if raw else 12.0
        except ValueError:
            value = 12.0
        return max(0.0, min(MAX_BOOST_DB, value))

    def _onset_sec(self) -> float:
        """段首补偿的时长（秒），缺省 0.6。"""
        raw = (self.config.get("ONSET_SEC", "") or "").strip()
        try:
            value = float(raw) if raw else 0.6
        except ValueError:
            value = 0.6
        return max(0.0, min(4.0, value))

    def _onset_every(self) -> bool:
        """是否每段都补偿；缺省只补每场会话的第一段。"""
        raw = (self.config.get("ONSET_EVERY", "") or "").strip().lower()
        return raw in ("1", "true", "on", "yes")

    def _clear_segment_levels(self) -> None:
        self._seg_raw.clear()
        self._seg_out.clear()
        self._seg_acc_raw = 0.0
        self._seg_acc_out = 0.0
        self._seg_count = 0

    def _track_segment(self, raw: bytes, out: bytes) -> None:
        """记每段开头几窗的 RMS，供段尾输出原始与补偿后的对比。"""
        if len(self._seg_raw) >= SEG_LOG_WINDOWS:
            return
        count = min(len(raw), len(out)) // 2
        if count <= 0:
            return
        raw_samples = struct.unpack(f"<{count}h", raw[: count * 2])
        out_samples = struct.unpack(f"<{count}h", out[: count * 2])
        for index in range(count):
            self._seg_acc_raw += raw_samples[index] ** 2
            self._seg_acc_out += out_samples[index] ** 2
            self._seg_count += 1
            if self._seg_count < self._seg_window:
                continue
            self._seg_raw.append(_window_db(self._seg_acc_raw / self._seg_count))
            self._seg_out.append(_window_db(self._seg_acc_out / self._seg_count))
            self._seg_acc_raw = 0.0
            self._seg_acc_out = 0.0
            self._seg_count = 0
            if len(self._seg_raw) >= SEG_LOG_WINDOWS:
                return

    def _log_segment_levels(self) -> None:
        """段结束：一行对比开头各窗的电平，判断补偿是否真的抬起句首。"""
        if not self._seg_raw:
            return
        logger.info(
            "段首电平（%.2fs 窗，dBFS）：原始 %s / 补偿后 %s",
            SEG_LOG_SEC,
            " ".join(f"{value:.0f}" for value in self._seg_raw),
            " ".join(f"{value:.0f}" for value in self._seg_out),
        )
        self._clear_segment_levels()

    async def _send_greeting(self) -> None:
        """唤醒后让豆包先开口：官方「打招呼」事件，文本由服务端合成。"""
        text = (self.config.get("GREETING", "") or "").strip()
        if not text or self._ws is None:
            return
        try:
            await asyncio.wait_for(
                self._ws.send(
                    json.dumps(
                        {
                            "event_id": "evt_greeting",
                            "type": "speech_text_buffer.commit",
                            "text": text,
                        }
                    )
                ),
                timeout=SEND_TIMEOUT,
            )
            logger.info("已发送打招呼：%s", text)
        except Exception as exc:  # noqa: BLE001 - 打招呼失败不影响会话
            logger.warning("打招呼发送失败：%s", exc)

    async def stop(self) -> None:
        self._running = False
        if self._receiver is not None:
            self._receiver.cancel()
            await asyncio.gather(self._receiver, return_exceptions=True)
            self._receiver = None
        if self._ws is not None:
            try:
                await asyncio.wait_for(
                    self._ws.send(json.dumps({"type": "session.close"})),
                    timeout=CLOSE_TIMEOUT,
                )
            except Exception:  # noqa: BLE001 - 关闭期不阻断资源释放
                pass
            try:
                await asyncio.wait_for(self._ws.close(), timeout=CLOSE_TIMEOUT)
            except Exception:  # noqa: BLE001
                pass
            self._ws = None

    async def send_audio_frame(self, frame: bytes) -> None:
        if not self._running or self._ws is None:
            return
        self._seq += 1
        payload = json.dumps(
            {
                "event_id": f"evt_{self._seq:06d}",
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(frame).decode("ascii"),
            }
        )
        try:
            await asyncio.wait_for(self._ws.send(payload), timeout=SEND_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("上行发送超时 %.0fs，按断连处理", SEND_TIMEOUT)
            self._running = False
        except Exception as exc:  # noqa: BLE001 - 发送失败等同连接已死
            logger.warning("上行中断：%s", exc)
            self._running = False

    def register_tools(self, tools: ToolRegistry) -> None:
        """统一 Schema → session.tools（标准 JSON Schema，name 平级）。"""
        self._tool_defs = [
            {
                "type": "function",
                "name": spec["function"]["name"],
                "description": spec["function"]["description"],
                "parameters": spec["function"]["parameters"],
            }
            for spec in tools.schemas()
        ]

    def is_running(self) -> bool:
        return self._running

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            while self._running:
                try:
                    raw = await asyncio.wait_for(self._ws.recv(), timeout=RECV_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.warning("%.0fs 无下行数据，按断连处理", RECV_TIMEOUT)
                    break
                await self._handle_event(json.loads(raw))
        except Exception as exc:  # noqa: BLE001 - 断连即结束会话
            logger.warning("豆包下行中断：%s", exc)
        finally:
            self._running = False

    async def _handle_event(self, event: dict) -> None:
        etype = event.get("type", "")

        if etype == "response.output_audio.started":
            # 段边界：缺省逐段补偿，ONSET_EVERY=0 时只补本场第一段
            self._clear_segment_levels()
            if self._onset_every() or not self._onset_used:
                self._onset.reset()
                self._onset_used = True
            else:
                self._onset.finish()
            return

        if etype == "response.output_audio.done":
            self._log_segment_levels()
            return

        if etype in {"response.output_audio.delta", "response.audio.delta"}:
            payload = event.get("delta") or event.get("audio") or ""
            if payload:
                raw_pcm = base64.b64decode(payload)
                out_pcm = self._onset.apply(raw_pcm)
                self._track_segment(raw_pcm, out_pcm)
                self.emit_audio(out_pcm)
            return

        if etype in {
            "response.audio_transcript.delta",
            "response.output_text.delta",
            "response.text.delta",
        }:
            text = event.get("delta") or event.get("text") or ""
            if text:
                self.emit(
                    SessionEvent(
                        session_id=self._session_id,
                        link="s2s",
                        role="assistant",
                        text=text,
                    )
                )
            return

        if etype == "conversation.item.input_audio_transcription.delta":
            logger.debug("识别中：%s", event.get("delta") or "")
            return

        if etype == "conversation.item.input_audio_transcription.completed":
            # 服务端 ASR 结果：判断「麦克风有没有被听见」最直接的证据
            text = event.get("transcript") or ""
            if text:
                logger.info("用户说：%s", text)
                self.emit(
                    SessionEvent(
                        session_id=self._session_id,
                        link="s2s",
                        role="user",
                        text=text,
                    )
                )
            return

        if etype == "conversation.item.input_audio_transcription.failed":
            logger.warning("识别失败：%s", event.get("error") or event)
            return

        if etype in {"response.output_text.done", "input_audio_buffer.committed"}:
            logger.debug("下行事件：%s", etype)
            return

        if etype == "response.function_call_arguments.done":
            await self._handle_tool_calls(event)
            return

        if etype == "error":
            logger.warning("豆包 error：%s", event.get("error") or event)
            return

        if etype == "session.closed":
            self._running = False
            return

        logger.debug("未处理的下行事件：%s %s", etype, event)

    async def _handle_tool_calls(self, event: dict) -> None:
        items = event.get("items") or event.get("calls") or []
        results = []
        for item in items:
            function = item.get("function") or {}
            name = item.get("name") or function.get("name")
            raw_args = item.get("arguments") or function.get("arguments") or "{}"
            call_id = item.get("call_id") or item.get("id")
            if not name:
                continue
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except ValueError:
                args = {}
            output = await self._tools.call(
                name, args, session_id=self._session_id, link="s2s", call_id=call_id
            )
            # 官方示例：items 只带 call_id / role=tool / content，content.type 固定 input_text
            results.append(
                {
                    "call_id": call_id,
                    "role": "tool",
                    "content": [{"type": "input_text", "text": output}],
                }
            )
        if results and self._ws is not None:
            try:
                await asyncio.wait_for(
                    self._ws.send(
                        json.dumps(
                            {"type": "conversation.item.create", "items": results}
                        )
                    ),
                    timeout=SEND_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning("工具结果回传超时，按断连处理")
                self._running = False


def create_provider(
    *,
    entry: WakewordEntry,
    settings: Settings,
    tools: ToolRegistry,
    on_event: Callable[[SessionEvent], None],
    on_audio: Callable[[bytes], None] | None = None,
) -> BaseProvider:
    return VolcanoS2SProvider(
        config=settings.group(entry.config_group),
        tools=tools,
        on_event=on_event,
        on_audio=on_audio,
    )
