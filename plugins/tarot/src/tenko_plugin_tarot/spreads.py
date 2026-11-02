"""牌阵和抽牌规则，不碰消息和数据库，方便单独测试。

- 今日塔罗：只抽大阿卡那，每人每天一张，当天再抽还是那张；
- 三张牌阵：78 张里抽 3 张，两次之间至少隔一段时间（默认 60 分钟，从上次占卜起算）；
- 凯尔特十字：78 张里抽 10 张，每人每天一次，当天再占还是那一组。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from . import deck

Kind = Literal["daily", "three", "celtic"]
Draw = tuple[str, bool]
"""(牌的 id, 是否逆位)。"""


@dataclass(frozen=True)
class Spread:
    name: str
    positions: tuple[str, ...]
    major_only: bool = False


SPREADS: dict[str, Spread] = {
    "daily": Spread("今日塔罗", ("今日",), major_only=True),
    "three": Spread("三张牌阵", ("过去", "现在", "未来")),
    "celtic": Spread(
        "凯尔特十字",
        (
            "现状",
            "阻碍",
            "根基",
            "过去",
            "目标",
            "近未来",
            "自我",
            "环境",
            "希望与恐惧",
            "结果",
        ),
    ),
}


def draw(kind: Kind, rng: random.Random) -> list[Draw]:
    spread = SPREADS[kind]
    pool = deck.major() if spread.major_only else list(deck.cards().values())
    picked = rng.sample(pool, len(spread.positions))
    return [(card.id, rng.random() < 0.5) for card in picked]


@dataclass(frozen=True)
class Verdict:
    kind: Literal["new", "again", "wait"]
    """new：抽新的；again：沿用上一次的结果；wait：还没到时间，wait_minutes 后再来。"""
    wait_minutes: int = 0


def _same_day(a: float, b: float) -> bool:
    return datetime.fromtimestamp(a).date() == datetime.fromtimestamp(b).date()


def check(
    kind: Kind,
    last_at: float | None,
    now: float,
    *,
    three_card_minutes: int = 60,
    celtic_cross_daily: bool = True,
) -> Verdict:
    """这次能不能抽新的。``last_at`` 是同一种占卜上一次的时间，没有就是 ``None``。"""

    if last_at is None:
        return Verdict("new")
    if kind == "daily":
        return Verdict("again" if _same_day(last_at, now) else "new")
    if kind == "celtic":
        return Verdict("again" if celtic_cross_daily and _same_day(last_at, now) else "new")
    left = last_at + three_card_minutes * 60 - now
    if left > 0:
        return Verdict("wait", math.ceil(left / 60))
    return Verdict("new")
