"""百科和小贴士：游戏里的典籍条目和载入画面小贴士。

数据取自旧 bot（游戏内繁体文本，已转成简体），存在 assets/codex.json；百科序号沿用旧 bot 的 1~310。
这里不碰 Entari，只管查。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import Any

from .names import MAPS, MODES
from .pages import ASSETS

CATEGORIES = ("武器", "载具", "战略", "战争", "全世界")


@dataclass(frozen=True)
class Entry:
    no: int
    category: str
    name: str
    header: str
    subheader: str
    unlock: str
    description: str
    image: str

    @property
    def title(self) -> str:
        """典籍标题不一定带名字（A7V 坦克的是“首款德式坦克”），这时用名字当标题。"""

        return self.header if _norm(self.name) in _norm(self.header) else self.name

    @property
    def taglines(self) -> list[str]:
        """标题下面的小字：没当标题用的典籍标题，和副标题。"""

        return [t for t in (self.header, self.subheader) if t and t != self.title]


@dataclass(frozen=True)
class Tip:
    text: str
    tags: tuple[str, ...]  # 地图、模式的中文名


def _load() -> tuple[list[Tip], list[Entry]]:
    data: dict[str, Any] = json.loads((ASSETS / "codex.json").read_text(encoding="utf-8"))
    tips = [
        Tip(t["text"], tuple(MAPS.get(tag) or MODES.get(tag) or tag for tag in t["tags"]))
        for t in data["tips"]
    ]
    wiki = [
        Entry(no, **{k: v for k, v in w.items() if k != "code"})
        for no, w in enumerate(data["wiki"], 1)
    ]
    return tips, wiki


TIPS, WIKI = _load()


def _norm(text: str) -> str:
    return "".join(text.split()).casefold()


def random_tip() -> Tip:
    return random.choice(TIPS)


def random_entry() -> Entry:
    return random.choice(WIKI)


def pick() -> Tip | Entry:
    """被戳时发的：小贴士和百科放在一起随机抽。"""

    return random.choice((*TIPS, *WIKI))


def search_wiki(keyword: str) -> list[Entry]:
    """序号、分类、条目名；名字里没有就搜正文（比如搜“托尔金”能找到“名人”）。"""

    key = _norm(keyword)
    if key.isdigit():
        no = int(key)
        return [WIKI[no - 1]] if 1 <= no <= len(WIKI) else []
    if key in CATEGORIES:
        return [e for e in WIKI if e.category == key]
    exact = [e for e in WIKI if key in (_norm(e.name), _norm(e.header))]
    if exact:
        return exact
    titled = [e for e in WIKI if key in _norm(f"{e.name}{e.header}{e.subheader}")]
    return titled or [e for e in WIKI if key in _norm(e.description)]


def search_tips(keyword: str) -> list[Tip]:
    """正文或地图、模式名里带关键词的小贴士。"""

    key = _norm(keyword)
    return [t for t in TIPS if key in _norm(t.text) or any(key in _norm(tag) for tag in t.tags)]
