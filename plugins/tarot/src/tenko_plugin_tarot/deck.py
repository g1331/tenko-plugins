"""牌库：78 张牌的数据、牌图，以及按名字找牌。

牌义在 ``data/cards.json``，牌图是 ``data/cards/<id>.jpg``（B 站幻星集）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import cache, lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image

DATA = Path(__file__).parent / "data"
SUITS = {"wands": "权杖", "cups": "圣杯", "swords": "宝剑", "pentacles": "星币"}
ROMAN = "0 I II III IV V VI VII VIII IX X XI XII XIII XIV XV XVI XVII XVIII XIX XX XXI".split()

# 常见的别称：女皇也有人叫皇后，星币也叫钱币……只认整个名字，不做子串匹配
MAJOR_ALIASES = {
    "魔术师": ["魔法师"],
    "女皇": ["皇后"],
    "教皇": ["教宗"],
    "隐士": ["隐者"],
    "命运之轮": ["命运", "幸运之轮"],
    "倒吊人": ["吊人"],
    "死神": ["死亡"],
    "恶魔": ["魔鬼"],
    "高塔": ["塔"],
    "星星": ["星"],
    "月亮": ["月"],
    "太阳": ["日"],
}
SUIT_ALIASES = {
    "权杖": ["权杖", "棍棒", "权仗"],
    "圣杯": ["圣杯", "杯"],
    "宝剑": ["宝剑", "剑"],
    "星币": ["星币", "钱币", "金币", "五芒星"],
}
RANK_ALIASES = {
    1: ["王牌", "ace", "一", "1", "01"],
    11: ["侍从", "侍者", "随从", "page"],
    12: ["骑士", "knight"],
    13: ["王后", "皇后", "女王", "queen"],
    14: ["国王", "king"],
}
CN_DIGITS = "零一二三四五六七八九十"


@dataclass(frozen=True)
class Card:
    id: str
    name: str
    name_en: str
    arcana: str
    """major，或者花色 wands / cups / swords / pentacles。"""
    number: int
    """大阿卡那 0–21；小阿卡那 1–10，侍从 11、骑士 12、王后 13、国王 14。"""
    upright: str
    reversed: str

    @property
    def major(self) -> bool:
        return self.arcana == "major"

    @property
    def group(self) -> str:
        """“大阿卡那 XVII”“权杖 · 小阿卡那”。"""

        if self.major:
            return f"大阿卡那 {ROMAN[self.number]}"
        return f"小阿卡那 · {SUITS[self.arcana]}"

    def meaning(self, reversed: bool) -> str:
        return self.reversed if reversed else self.upright

    @property
    def path(self) -> Path:
        return DATA / "cards" / f"{self.id}.jpg"


def orientation(reversed: bool) -> str:
    return "逆位" if reversed else "正位"


@cache
def cards() -> dict[str, Card]:
    raw = json.loads((DATA / "cards.json").read_text(encoding="utf-8"))
    return {item["id"]: Card(**item) for item in raw}


def major() -> list[Card]:
    return [card for card in cards().values() if card.major]


def _key(text: str) -> str:
    text = re.sub(r"[\s·・_\-]+", "", text.casefold())
    return text.removeprefix("the")


def _names(card: Card) -> list[str]:
    names = [card.name, card.name_en]
    if card.major:
        names += MAJOR_ALIASES.get(card.name, [])
        return names
    ranks = RANK_ALIASES.get(card.number, [])
    if 2 <= card.number <= 10:
        ranks = [CN_DIGITS[card.number], str(card.number), f"{card.number:02d}"]
    for suit in SUIT_ALIASES[SUITS[card.arcana]]:
        names += [suit + rank for rank in ranks]
    return names


@cache
def _index() -> dict[str, Card]:
    index: dict[str, Card] = {}
    for card in cards().values():
        for name in _names(card):
            index.setdefault(_key(name), card)
    return index


def find(query: str) -> list[Card]:
    """先按全名和别称精确找；找不到再看哪些牌名里带这几个字。"""

    key = _key(query)
    if not key:
        return []
    if key in _index():
        return [_index()[key]]
    return [
        card for card in cards().values() if key in _key(card.name) or key in _key(card.name_en)
    ]


@lru_cache(maxsize=32)
def picture(card_id: str, reversed: bool = False) -> bytes:
    """牌图的 JPEG；逆位转 180°。"""

    path = cards()[card_id].path
    if not reversed:
        return path.read_bytes()
    with Image.open(path) as image:
        buffer = BytesIO()
        image.rotate(180).save(buffer, "JPEG", quality=85)
    return buffer.getvalue()
