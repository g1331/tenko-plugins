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
_DAY = 86400
# 新收的先留几天：刚收进来还没轮到发，按用量淘汰的话下一张一进来就把它挤掉了。
_FRESH = 3 * _DAY
_BANS = 500  # 删过的图记多少张，多了丢最早的


def _clean(text: str, limit: int, drop: str = r"[\s\[\]:：,，]+") -> str:
    return re.sub(drop, "", text)[:limit]


class Stickers:
    """索引：{名字: {"file", "mood", "qq", "saved", "used", "last"}}；``qq`` 是 QQ 给的
    图片文件名（内容的 md5），用来认出群友又发了收藏里的同一张。

    ``bans`` 记删过的图 [{"sha", "qq", "name"}]：被人要求删掉的，群友再发也不收回来。
    """

    def __init__(self, store: Any, limit: int = 100, bans: Any = None) -> None:
        self.store = store
        self.limit = limit
        self.bans = bans

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

    def add(
        self, name: str, mood: str, data: bytes, qq_file: str, replace: str = ""
    ) -> tuple[str, list[str]] | None:
        """收进一张，返回 (最终的名字, 腾位置丢掉的名字)。

        名字不合法、格式不认识或已经收过就返回 ``None``。

        满了先丢模型挑的 ``replace``；没挑或挑的不在收藏里，丢收了几天以上的里头平均每天发得最少的，
        一样少就丢最久没发的；全是新收的才在全部里挑。
        """

        name, mood = _clean(name, 12), _clean(mood, 60, r"[\s\[\]]+")
        kind = next((k for magic, k in _KINDS.items() if data.startswith(magic)), None)
        if kind is None and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            kind = "webp"
        if not name or kind is None:
            return None
        if self.banned(data, qq_file):
            return None
        file = f"{hashlib.sha256(data).hexdigest()[:16]}.{kind}"
        index = self.index()
        if any(s["file"] == file or (qq_file and s["qq"] == qq_file) for s in index.values()):
            return None
        final, n = name, 2
        while final in index:
            final, n = f"{name}{n}", n + 1
        dropped = []
        now = time.time()
        while index and len(index) >= self.limit:
            if replace in index:
                stale = replace
            else:
                old = [k for k in index if now - index[k]["saved"] >= _FRESH] or list(index)
                stale = min(old, key=lambda k: (self._rate(index[k], now), index[k]["last"]))
            self._drop(index, stale)
            dropped.append(stale)
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / file).write_bytes(data)
        index[final] = {
            "file": file,
            "mood": mood,
            "qq": qq_file,
            "saved": now,
            "used": 0,
            "last": now,
        }
        self.store.save(index)
        return final, dropped

    def edit(self, name: str, mood: str, new_name: str = "") -> str | None:
        """改备注，``new_name`` 合法且没被占用时顺带改名。

        返回最终名字；收藏里没有或备注为空返回 ``None``。
        """

        index = self.index()
        mood, new_name = _clean(mood, 60, r"[\s\[\]]+"), _clean(new_name, 12)
        if name not in index or not mood:
            return None
        index[name]["mood"] = mood
        if new_name and new_name not in index:
            index = {(new_name if k == name else k): v for k, v in index.items()}
            name = new_name
        self.store.save(index)
        return name

    def remove(self, name: str) -> bool:
        """删掉一张并记进 ``bans``；收藏满了腾位置丢的不算，那些只是没地方放。"""

        index = self.index()
        if name not in index:
            return False
        entry = index[name]
        self._drop(index, name)
        self.store.save(index)
        if self.bans is not None:
            ban = {"sha": entry["file"].partition(".")[0], "qq": entry["qq"], "name": name}
            self.bans.save([*self.bans.load(default=[]), ban][-_BANS:])
        return True

    def banned(self, data: bytes, qq_file: str) -> bool:
        """这张图删过：内容一样，或者 QQ 文件名（内容的 md5）一样。"""

        if self.bans is None:
            return False
        sha = hashlib.sha256(data).hexdigest()[:16]
        return any(
            b["sha"] == sha or (qq_file and b["qq"] == qq_file) for b in self.bans.load(default=[])
        )

    def _drop(self, index: dict[str, dict[str, Any]], name: str) -> None:
        (self.folder / index.pop(name)["file"]).unlink(missing_ok=True)

    def use(self, name: str) -> None:
        index = self.index()
        if name in index:
            index[name]["used"] += 1
            index[name]["last"] = time.time()
            self.store.save(index)

    def rate(self, name: str) -> float:
        """平均每天发几次，收进来不满一天按一天算；不在收藏里是 0。"""

        entry = self.index().get(name)
        return self._rate(entry, time.time()) if entry else 0.0

    @staticmethod
    def _rate(entry: dict[str, Any], now: float) -> float:
        return entry["used"] / max(1.0, (now - entry["saved"]) / _DAY)

    def moods(self) -> dict[str, str]:
        """名字 → 备注，会话拿它当目录快照。"""

        return {name: s["mood"] for name, s in self.index().items()}

    def catalog(self, moods: dict[str, str]) -> str:
        """给模型看的收藏目录，按会话的快照 ``moods`` 写，收藏变了也不跟着变，不打断前缀缓存。"""

        if not moods:
            return "你的表情包收藏还是空的。"
        items = "\n".join(f"{name}：{mood}" for name, mood in moods.items())
        return f"{self._full(moods)}你收藏的表情包（名字：图上的字、梗和什么时候发）：\n{items}"

    def changes(self, moods: dict[str, str]) -> str:
        """快照之后收藏的变化，写进每次都变的末尾提示；下次压缩时会话换新快照，这里就清空了。"""

        now = self.moods()
        added = [f"{name}：{mood}" for name, mood in now.items() if moods.get(name) != mood]
        gone = [name for name in moods if name not in now]
        parts = []
        if added:
            parts.append("目录之后新收藏或改了备注的表情包：\n" + "\n".join(added))
        if gone:
            parts.append(f"这些已经不在收藏里，别再发：{'、'.join(gone)}")
        if parts and not self._full(moods):
            parts.append(self._full(now).rstrip())
        return "\n".join(p for p in parts if p)

    def _full(self, moods: dict[str, str]) -> str:
        if len(moods) < self.limit:
            return ""
        return (
            f"收藏已经满了（{self.limit} 张）：再收新的要在 save_sticker 的 replace 里"
            "填一张你最不想要的，腾出位置。\n"
        )
