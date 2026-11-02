"""塔罗牌：今日塔罗、三张牌阵、凯尔特十字，查单张牌和塔罗冷知识。

重做自旧版 xiaomai-bot 的 tarot。牌库在 :mod:`.deck`，牌阵和次数规则在 :mod:`.spreads`，
出图在 :mod:`.render`；这里解析命令、读写占卜记录，并把结果发回去。
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import date, datetime
from typing import Any

from arclet.alconna import Alconna, AllParam, Args, CommandMeta
from arclet.entari import BasicConfModel, MessageChain, Session, command, plugin
from arclet.entari.command import Query
from satori import Image, Text
from sqlalchemy import JSON, String, select
from sqlalchemy.orm import Mapped, mapped_column
from tenko_api.features import declare, rate_limit
from tenko_api.render import RenderService
from tenko_api.storage import Model, get_session

from . import deck, festivals, lore, render
from .spreads import SPREADS, Draw, Kind, Verdict, check, draw


class TarotConfig(BasicConfModel):
    """对应 config/tenko.toml 中的 ``[plugin_config.tarot]``。"""

    three_card_minutes: int = 60
    """三张牌阵两次之间至少隔多少分钟，从上次占卜起算；0 表示不限。"""
    celtic_cross_daily: bool = True
    """凯尔特十字每人每天一次，当天再占还是那一组；关掉后不限次数。"""


plugin.metadata(
    "塔罗牌",
    author=["13"],
    version="0.1.0",
    description="今日塔罗、三张牌阵和凯尔特十字占卜，牌面是 B 站幻星集；可以查单张牌和塔罗冷知识。",
    urls={
        "homepage": "https://github.com/g1331/tenko-plugins/tree/main/plugins/tarot",
        "repository": "https://github.com/g1331/tenko-plugins",
    },
    config=TarotConfig,
)
declare(scope="group", default_enabled=True)

config = plugin.get_config(TarotConfig)

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


def _display_name(session: Session) -> str:
    """群名片优先：OneBot 适配器把它放在 member.nick 里。"""

    member = getattr(session.event, "member", None)
    return getattr(member, "nick", None) or session.user.name or session.user.id


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


# ---- 命令 ----


def _words(first: Query[str], rest: Query[Any]) -> str:
    """命令名后面的文字，图片之类的不算。"""

    words = [first.result] if first.available else []
    if rest.available:
        words += [e.text for e in rest.result if isinstance(e, Text)]
    return " ".join(w.strip() for w in words if w.strip())


def _question(first: Query[str], rest: Query[Any]) -> str | None:
    return _words(first, rest)[:QUESTION_LENGTH] or None


def _commands(names: list[str], args: Args, description: str, usage: str, example: str):
    """entari 按命令名派发，别名到不了处理函数，每个名字单独注册。"""

    def register(handler):
        main, aliases = names[0], names[1:]
        note = f"；也可以用 {'、'.join(aliases)}" if aliases else ""
        meta = CommandMeta(description + note, usage=usage, example=example)
        command.on(Alconna(main, args, meta=meta))(handler)
        for alias in aliases:
            meta = CommandMeta(description, usage=usage.replace(main, alias, 1), hide=True)
            command.on(Alconna(alias, args, meta=meta))(handler)
        return handler

    return register


TEXT_ARGS = Args["first?", str]["rest?", AllParam]


@_commands(
    ["今日塔罗"],
    Args(),
    "从 22 张大阿卡那里抽一张今天的牌，每天零点重置；/塔罗牌 不带牌名也是抽这个",
    "今日塔罗",
    "/今日塔罗",
)
async def daily_command(session: Session):
    return await daily(session.user.id)


@_commands(
    ["塔罗牌"],
    TEXT_ARGS,
    "查一张牌的牌面和正逆位牌义；不带牌名就是抽今天的牌",
    "塔罗牌 [牌名]",
    "/塔罗牌 宝剑三",
)
async def card_command(
    session: Session,
    first: Query[str] = Query("first"),  # noqa: B008
    rest: Query[Any] = Query("rest"),  # noqa: B008
):
    query = _words(first, rest)
    return lookup(query) if query else await daily(session.user.id)


@_commands(
    ["占卜"],
    TEXT_ARGS,
    "三张牌阵：从 78 张里抽过去、现在、未来三张，可以带上想问的事",
    "占卜 [问题]",
    "/占卜 这周的面试",
)
@rate_limit(2)
async def three_card_command(
    session: Session,
    first: Query[str] = Query("first"),  # noqa: B008
    rest: Query[Any] = Query("rest"),  # noqa: B008
    *,
    render_service: RenderService,
):
    return await spread(
        "three",
        session.user.id,
        _display_name(session),
        _question(first, rest),
        render_service,
    )


@_commands(
    ["凯尔特十字"],
    TEXT_ARGS,
    "凯尔特十字牌阵：从 78 张里抽 10 张排成十字，每天一次",
    "凯尔特十字 [问题]",
    "/凯尔特十字 今年的工作",
)
@rate_limit(3)
async def celtic_command(
    session: Session,
    first: Query[str] = Query("first"),  # noqa: B008
    rest: Query[Any] = Query("rest"),  # noqa: B008
    *,
    render_service: RenderService,
):
    return await spread(
        "celtic",
        session.user.id,
        _display_name(session),
        _question(first, rest),
        render_service,
    )


@_commands(["塔罗记录"], Args(), "看看自己最近 5 次占卜的结果", "塔罗记录", "/塔罗记录")
async def records_command(session: Session):
    return await records(session.user.id)


@_commands(["塔罗知识"], Args(), "随机一条塔罗冷知识", "塔罗知识", "/塔罗知识")
async def lore_command():
    return rng.choice(lore.facts())
