"""bot 自己的表情包收藏：所有群共用一份，图片放在插件数据目录，索引存 JSON。"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any

# 模型在 text 里写“[表情:名字]”就发一张收藏的表情包。
TOKEN = re.compile(r"\[表情[:：]\s*([^\[\]]{1,12}?)\s*\]")
_KINDS = {b"\xff\xd8\xff": "jpg", b"\x89PNG": "png", b"GIF8": "gif"}


def _clean(text: str, limit: int, drop: str = r"[\s\[\]:：,，]+") -> str:
    return re.sub(drop, "", text)[:limit]


class Stickers:
    """索引：{名字: {"file", "mood", "qq", "saved", "used", "last"}}；``qq`` 是 QQ 给的
    图片文件名（内容的 md5），用来认出群友又发了收藏里的同一张。"""

    def __init__(self, store: Any, limit: int = 100) -> None:
        self.store = store
        self.limit = limit

    @property
    def folder(self) -> Path:
        """图片放在索引文件旁边；跟着 Store 解析，沙盒里读写的是副本。"""

        return Path(self.store.path).parent / "stickers"

    def index(self) -> dict[str, dict[str, Any]]:
        return self.store.load(default={})

    def name_of(self, qq_file: str | None) -> str | None:
        if not qq_file:
            return None
        return next((name for name, s in self.index().items() if s["qq"] == qq_file), None)

    def path(self, name: str) -> Path | None:
        entry = self.index().get(name)
        path = entry and self.folder / entry["file"]
        return path if path and path.is_file() else None

    def add(self, name: str, mood: str, data: bytes, qq_file: str) -> str | None:
        """收进一张，返回最终的名字；名字不合法、格式不认识或已经收过就返回 ``None``。"""

        name, mood = _clean(name, 12), _clean(mood, 20, r"[\s\[\]]+")
        kind = next((k for magic, k in _KINDS.items() if data.startswith(magic)), None)
        if kind is None and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            kind = "webp"
        if not name or kind is None:
            return None
        file = f"{hashlib.sha256(data).hexdigest()[:16]}.{kind}"
        index = self.index()
        if any(s["file"] == file or (qq_file and s["qq"] == qq_file) for s in index.values()):
            return None
        final, n = name, 2
        while final in index:
            final, n = f"{name}{n}", n + 1
        while index and len(index) >= self.limit:
            # 满了先丢用得最少、最久没用的。
            stale = min(index, key=lambda k: (index[k]["used"], index[k]["last"]))
            self._drop(index, stale)
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / file).write_bytes(data)
        now = time.time()
        index[final] = {
            "file": file,
            "mood": mood,
            "qq": qq_file,
            "saved": now,
            "used": 0,
            "last": now,
        }
        self.store.save(index)
        return final

    def remove(self, name: str) -> bool:
        index = self.index()
        if name not in index:
            return False
        self._drop(index, name)
        self.store.save(index)
        return True

    def _drop(self, index: dict[str, dict[str, Any]], name: str) -> None:
        (self.folder / index.pop(name)["file"]).unlink(missing_ok=True)

    def use(self, name: str) -> None:
        index = self.index()
        if name in index:
            index[name]["used"] += 1
            index[name]["last"] = time.time()
            self.store.save(index)

    def catalog(self) -> str:
        """给模型看的收藏目录：只在收藏增删时变，不影响前缀缓存。"""

        index = self.index()
        if not index:
            return "你的表情包收藏还是空的。"
        items = "\n".join(f"{name}：{s['mood']}" for name, s in index.items())
        return f"你收藏的表情包（名字：适合的场合）：\n{items}"
