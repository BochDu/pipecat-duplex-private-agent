"""笔记：`NOTES_DIR` 只读建索引，原文不入库、不复制、不改写。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from app.storage import Storage

logger = logging.getLogger(__name__)

INDEXABLE = {".md", ".txt"}


class NotesIndex:
    def __init__(self, notes_dir: Path | None, storage: Storage) -> None:
        self._notes_dir = Path(notes_dir) if notes_dir else None
        self._storage = storage

    @property
    def enabled(self) -> bool:
        return self._notes_dir is not None and self._notes_dir.exists()

    def reindex(self) -> int:
        """只读扫描并写索引；返回索引条数。原文不落库。"""
        if not self.enabled:
            logger.info("未配置 NOTES_DIR，跳过笔记索引")
            return 0
        count = 0
        now = datetime.now(timezone.utc).isoformat()
        for path in sorted(self._notes_dir.rglob("*")):
            if path.suffix.lower() not in INDEXABLE or not path.is_file():
                continue
            self._storage.conn.execute(
                """
                INSERT INTO notes_index (path, title, sensitivity, updated_at)
                VALUES (?, ?, 'local-only', ?)
                """,
                (str(path.relative_to(self._notes_dir)), path.stem, now),
            )
            count += 1
        self._storage.conn.commit()
        logger.info("笔记索引完成：%d 条", count)
        return count
