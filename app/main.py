"""常驻入口：加载注册表与配置、组装全局单例、启动唤醒监听与路由。"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from app.audio import ensure_spoken_chime
from app.config import ROOT, Registry, Settings
from app.context import AppContext
from app.jobs import JobManager
from app.memory import MemoryManager
from app.notes import NotesIndex
from app.scheduler import Scheduler
from app.storage import Storage, reset_database
from app.tools import ToolRegistry
from app.wake import build_listener
from app.wake_router import WakeRouter

logger = logging.getLogger(__name__)


def load_config(root: Path = ROOT) -> tuple[Registry, Settings]:
    """先读注册表，再按注册表声明的配置组切分 .env。"""
    registry = Registry.load(root)
    settings = Settings.load(root, registry.group_names)
    return registry, settings


def build_context(registry: Registry, settings: Settings) -> AppContext:
    """组装全项目唯一的单例。"""
    storage = Storage(settings.data_dir).init()
    return AppContext(
        settings=settings,
        registry=registry,
        storage=storage,
        tools=ToolRegistry(),
        memory=MemoryManager(
            storage, scope=str(registry.defaults.get("memory_scope", "global"))
        ),
        jobs=JobManager(storage),
        scheduler=Scheduler(storage),
        notes=NotesIndex(settings.notes_dir, storage),
    )


def credentials_ready(settings: Settings, group: str) -> bool:
    """只看 API_KEY：默认值文件提供的模型名、音色、端点不算已配置。"""
    api_key = (settings.group(group).get("API_KEY", "") or "").strip()
    return bool(api_key) and api_key != "replace-me"


def format_registry(registry: Registry, settings: Settings) -> str:
    lines = ["唤醒词\t配置组\t链路\tProvider\t凭据已配置"]
    for entry in registry.wakewords:
        configured = "是" if credentials_ready(settings, entry.config_group) else "否"
        lines.append(
            f"{entry.word}\t{entry.config_group}\t{entry.link}\t"
            f"{entry.provider}\t{configured}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="私人双唤醒词语音终端")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--list", action="store_true", help="打印注册表与配置组状态后退出"
    )
    parser.add_argument("--wake", help="直接触发一次唤醒词（调试）")
    parser.add_argument(
        "--volume",
        type=int,
        metavar="0-200",
        help=(
            "播放端音量，覆盖 .env 的 AUDIO_OUT_VOLUME；"
            "豆包响度另由 S2S_DOUBAO_LOUDNESS 在服务端调"
        ),
    )
    parser.add_argument(
        "--reset-db",
        action="store_true",
        help="删除 data/agent.db 后重建（会话历史与索引清空，配置不动）",
    )
    args = parser.parse_args(argv)

    registry, settings = load_config(args.root)
    if args.volume is not None:
        settings.common["AUDIO_OUT_VOLUME"] = str(max(0, min(200, args.volume)))

    if args.list:
        print(format_registry(registry, settings))
        return 0

    if args.reset_db:
        removed = reset_database(settings.data_dir)
        logger.warning(
            "数据库已重置：%s",
            "、".join(path.name for path in removed) if removed else "无旧文件",
        )

    print(f"播放端音量：{settings.out_volume}%（100% 为原始）")
    context = build_context(registry, settings)
    router = WakeRouter(context)

    async def run() -> None:
        await context.scheduler.start()
        # 后台准备唤醒人声提示，不阻塞待机；准备好之前用音阶顶上
        asyncio.create_task(ensure_spoken_chime(settings.get("TTS_VOICE", "") or ""))
        try:
            if args.wake:
                ok = await router.route(args.wake)
                print("路由结果：", "已进入会话" if ok else "失败")
                return
            words = "、".join(entry.word for entry in registry.wakewords)
            print(f"待机中，说「{words}」进入会话（Ctrl-C 退出）：")
            await router.serve(
                build_listener(settings, [e.word for e in registry.wakewords])
            )
        finally:
            await router.stop()
            await context.scheduler.stop()
            context.storage.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
