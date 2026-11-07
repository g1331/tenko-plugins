"""占卜：读写占卜记录，按次数规则决定抽新牌还是沿用上一次，再把结果排成回复。

查单张牌也在这里，和占卜共用出牌面图的办法。
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import date, datetime
from typing import Any

from arclet.entari import MessageChain
from satori import Image, Text
from sqlalchemy import JSON, String, select
from sqlalchemy.orm import Mapped, mapped_column
from tenko_api.storage import Model, get_session

from . import deck, festivals, render
from .config import config
from .spreads import SPREADS, Draw, Kind, Verdict, check, draw

HISTORY = 5
QUESTION_LENGTH = 60
_lock = asyncio.Lock()
rng = random.Random()


class Reading(Model):
    """一次占卜：谁、哪种、抽到的牌（[[牌, 是否逆位], ...]）、所问的事。"""

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    cards: Mapped[list] = mapped_column(JSON)
    question: Mapped[str | None] = mapped_column(String(QUESTION_LENGTH))
    created_at: Mapped[float]

    @property
    def draws(self) -> list[Draw]:
        return [(card, bool(reversed)) for card, reversed in self.cards]


# ---- 占卜记录 ----


async def _last(user: str, kind: Kind) -> Reading | None:
    async with get_session() as db:
        return await db.scalar(
            select(Reading)
            .where(Reading.user_id == user, Reading.kind == kind)
            .order_by(Reading.created_at.desc(), Reading.id.desc())
            .limit(1)
        )


async def read(
    kind: Kind, user: str, question: str | None = None, now: float | None = None
) -> tuple[Verdict, Reading]:
    """这次占卜的结果：能抽新的就抽一组存起来，否则沿用上一次。"""

    now = time.time() if now is None else now
    async with _lock:
        last = await _last(user, kind)
        verdict = check(
            kind,
            last.created_at if last else None,
            now,
            three_card_minutes=config.three_card_minutes,
            celtic_cross_daily=config.celtic_cross_daily,
        )
        if verdict.kind != "new":
            assert last is not None
            return verdict, last
        reading = Reading(
            user_id=user,
            kind=kind,
            cards=[[card, reversed] for card, reversed in draw(kind, rng)],
            question=question,
            created_at=now,
        )
        async with get_session() as db:
            db.add(reading)
            await db.commit()
            await db.refresh(reading)
        return verdict, reading


async def history(user: str, limit: int = HISTORY) -> list[Reading]:
    async with get_session() as db:
        found = await db.scalars(
            select(Reading)
            .where(Reading.user_id == user)
            .order_by(Reading.created_at.desc(), Reading.id.desc())
            .limit(limit)
        )
        return list(found)


# ---- 回复 ----


def _picture(draw: Draw) -> Image:
    return Image.of(raw=deck.picture(*draw), mime="image/jpeg")


def _note(reading: Reading) -> str | None:
    return festivals.note(date.fromtimestamp(reading.created_at), reading.draws)


def _summary(reading: Reading) -> str:
    """一行字的结果：“过去 星星（正位）、现在 ……”。"""

    spread = SPREADS[reading.kind]
    if reading.kind == "daily":
        return render.label(reading.draws[0])
    return "、".join(
        f"{position} {render.label(d)}"
        for position, d in zip(spread.positions, reading.draws, strict=True)
    )


async def daily(user: str, now: float | None = None) -> MessageChain:
    verdict, reading = await read("daily", user, now=now)
    card_id, reversed = reading.draws[0]
    card = deck.cards()[card_id]
    head = "你今天已经抽过了，今天的牌是" if verdict.kind == "again" else "你今天的塔罗牌是"
    lines = [f"\n{card.group} · {card.name_en}", card.meaning(reversed)]
    if note := _note(reading):
        lines.append(note)
    return MessageChain(
        [
            Text(f"{head}【{card.name}】（{deck.orientation(reversed)}）\n"),
            _picture(reading.draws[0]),
            Text("\n".join(lines)),
        ]
    )


async def spread(
    kind: Kind,
    user: str,
    user_name: str,
    question: str | None,
    service: Any = None,
    now: float | None = None,
) -> MessageChain | str:
    verdict, reading = await read(kind, user, question, now=now)
    name = SPREADS[kind].name
    if verdict.kind == "wait":
        asked = f"「{reading.question}」" if reading.question else ""
        return (
            f"{name}每 {config.three_card_minutes} 分钟只能占一次，"
            f"再等 {verdict.wait_minutes} 分钟吧。\n上次{asked}：{_summary(reading)}"
        )
    head = f"你今天已经占过{name}了，这是今天的结果" if verdict.kind == "again" else None
    page = render.context(
        kind,
        reading.draws,
        question=reading.question,
        user=user_name,
        at=reading.created_at,
        note=_note(reading),
        images=service is not None,
    )
    picture = await render.image(service, page)
    if picture is None:
        reply = render.text(page)
        return f"{head}：\n{reply}" if head else reply
    elements: list = [Image.of(raw=picture, mime="image/jpeg")]
    if head:
        elements.insert(0, Text(f"{head}：\n"))
    return MessageChain(elements)


def lookup(query: str) -> MessageChain | str:
    found = deck.find(query)
    if not found:
        return f"没有找到“{query}”这张牌，可以试试 /塔罗牌 星星、/塔罗牌 宝剑三"
    if len(found) > 1:
        names = "、".join(card.name for card in found[:10])
        more = "……" if len(found) > 10 else ""
        return f"名字带“{query}”的牌有 {len(found)} 张：{names}{more}\n说得再具体一点吧"
    card = found[0]
    return MessageChain(
        [
            Text(f"{card.name} {card.name_en} · {card.group}\n"),
            _picture((card.id, False)),
            Text(f"\n正位：{card.upright}\n逆位：{card.reversed}"),
        ]
    )


async def records(user: str) -> str:
    readings = await history(user)
    if not readings:
        return "你还没有占卜过，先来一张 /今日塔罗 吧"
    lines = [f"你最近的 {len(readings)} 次占卜："]
    for reading in readings:
        when = datetime.fromtimestamp(reading.created_at).strftime("%m-%d %H:%M")
        asked = f"「{reading.question}」" if reading.question else ""
        lines.append(f"{when} {SPREADS[reading.kind].name}{asked}：{_summary(reading)}")
    return "\n".join(lines)
