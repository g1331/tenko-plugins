"""塔罗牌：今日塔罗、三张牌阵、凯尔特十字，查单张牌和塔罗冷知识。

重做自旧版 xiaomai-bot 的 tarot。这里只声明命令，取出参数后交给 :mod:`.readings`
（占卜、占卜记录和查牌）。牌库在 :mod:`.deck`，牌阵和次数规则在 :mod:`.spreads`，
出图在 :mod:`.render`。
"""

from __future__ import annotations

from typing import Any

from arclet.alconna import Args
from arclet.entari import Session, plugin
from arclet.entari.command import Query
from tenko_api.features import declare, rate_limit
from tenko_api.render import RenderService

from . import lore, readings
from .config import TarotConfig
from .entry import TEXT_ARGS, commands, display_name, question, words

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


@commands(
    ["今日塔罗"],
    Args(),
    "从 22 张大阿卡那里抽一张今天的牌，每天零点重置；/塔罗牌 不带牌名也是抽这个",
    "今日塔罗",
    "/今日塔罗",
)
async def daily_command(session: Session):
    return await readings.daily(session.user.id)


@commands(
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
    query = words(first, rest)
    return readings.lookup(query) if query else await readings.daily(session.user.id)


@commands(
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
    return await readings.spread(
        "three",
        session.user.id,
        display_name(session),
        question(first, rest),
        render_service,
    )


@commands(
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
    return await readings.spread(
        "celtic",
        session.user.id,
        display_name(session),
        question(first, rest),
        render_service,
    )


@commands(["塔罗记录"], Args(), "看看自己最近 5 次占卜的结果", "塔罗记录", "/塔罗记录")
async def records_command(session: Session):
    return await readings.records(session.user.id)


@commands(["塔罗知识"], Args(), "随机一条塔罗冷知识", "塔罗知识", "/塔罗知识")
async def lore_command():
    return readings.rng.choice(lore.facts())
