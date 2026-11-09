"""战地六查询：生涯战绩、武器载具配备排行和对局估算。

这里只声明命令，每条命令取出参数后交给对应模块：

- :mod:`.stats` 生涯战绩、武器载具排行、对局估算
- :mod:`.binding` 绑定 EA 账号

数据来自 bf-manager 的公开查询接口（:mod:`.api`），名字和图片查 :mod:`.catalog`，
图片排版在 :mod:`.pages`，渲染不了时回文字。
"""

from __future__ import annotations

from typing import Any

from arclet.alconna import Args, Option
from arclet.entari import Session, plugin
from arclet.entari.command import Query
from tenko_api.features import declare
from tenko_api.render import RenderService

from . import binding, stats
from .config import Bf6Config
from .entry import (
    card_of,
    commands,
    group_and_name,
    items_commands,
    layout_of,
    name_of,
    player_commands,
    service_for,
)
from .reply import answer

plugin.metadata(
    "战地六查询",
    author=["13"],
    version="0.1.0",
    description="经 bf-manager 查询战地六玩家的生涯战绩、武器、载具、配备排行和对局估算。",
    urls={
        "homepage": "https://github.com/g1331/tenko-plugins/tree/main/plugins/bf6",
        "repository": "https://github.com/g1331/tenko-plugins",
    },
    config=Bf6Config,
)
declare(scope="group", default_enabled=True)


@player_commands(["bf6s", "bf6战绩", "bf6生涯"], "生涯战绩")
async def bf6s(
    session: Session,
    name: Query[str] = Query("name"),  # noqa: B008
    text: Query[Any] = Query("text"),  # noqa: B008
    *,
    render_service: RenderService,
):
    service = service_for(text, render_service)
    return await answer(
        stats.stats_reply(name_of(name), service, session.user.id, card_of(session))
    )


async def _items(
    kind: str,
    session: Session,
    first: Query[str],
    second: Query[str],
    text: Query[Any],
    cols: Query[int],
    rows: Query[int],
    search: Query[str],
    sort: Query[str],
    render_service: RenderService,
):
    group, name = group_and_name(kind, first, second)
    keyword = name_of(search)
    return await answer(
        stats.items_reply(
            kind,
            name,
            service_for(text, render_service),
            session.user.id,
            group,
            layout_of(cols, rows),
            keyword.strip("\"'“”") if keyword else None,
            name_of(sort),
            card_of(session),
        )
    )


@items_commands("weapons", ["bf6w", "bf6武器"])
async def bf6w(
    session: Session,
    first: Query[str] = Query("first"),  # noqa: B008
    second: Query[str] = Query("second"),  # noqa: B008
    text: Query[Any] = Query("text"),  # noqa: B008
    cols: Query[int] = Query("cols.cols"),  # noqa: B008
    rows: Query[int] = Query("rows.rows"),  # noqa: B008
    search: Query[str] = Query("search.keyword"),  # noqa: B008
    sort: Query[str] = Query("sort.sort"),  # noqa: B008
    *,
    render_service: RenderService,
):
    return await _items(
        "weapons", session, first, second, text, cols, rows, search, sort, render_service
    )


@items_commands("vehicles", ["bf6v", "bf6载具"])
async def bf6v(
    session: Session,
    first: Query[str] = Query("first"),  # noqa: B008
    second: Query[str] = Query("second"),  # noqa: B008
    text: Query[Any] = Query("text"),  # noqa: B008
    cols: Query[int] = Query("cols.cols"),  # noqa: B008
    rows: Query[int] = Query("rows.rows"),  # noqa: B008
    search: Query[str] = Query("search.keyword"),  # noqa: B008
    sort: Query[str] = Query("sort.sort"),  # noqa: B008
    *,
    render_service: RenderService,
):
    return await _items(
        "vehicles", session, first, second, text, cols, rows, search, sort, render_service
    )


COUNT = Option(
    "-n|-num|-局数",
    Args["count", int],
    dest="count",
    help_text=f"列几条，最多 {stats.MAX_RECENT}",
)


@player_commands(
    ["bf6r", "bf6对局", "bf6最近"],
    f"对局估算（按两次读取之间的生涯变化推算），默认 {stats.RECENT} 条，-n 指定条数",
    " [-n 条数]",
    COUNT,
)
async def bf6r(
    session: Session,
    name: Query[str] = Query("name"),  # noqa: B008
    text: Query[Any] = Query("text"),  # noqa: B008
    count: Query[int] = Query("count.count"),  # noqa: B008
    *,
    render_service: RenderService,
):
    rounds = max(1, min(count.result, stats.MAX_RECENT)) if count.available else stats.RECENT
    service = service_for(text, render_service)
    return await answer(
        stats.recent_reply(name_of(name), service, session.user.id, rounds, card_of(session))
    )


@commands(
    ["bf6绑定", "bf6b"],
    Args["name?", str],
    "把自己的 EA 名字记下来，之后查战地六可以不带名字；不带名字查看当前绑定",
    "bf6绑定 [EA 名字]",
    "/bf6绑定 JackFrags",
)
async def bf6bind(session: Session, name: Query[str] = Query("name")):  # noqa: B008
    return await answer(binding.bind_reply(name_of(name), session.user.id))
