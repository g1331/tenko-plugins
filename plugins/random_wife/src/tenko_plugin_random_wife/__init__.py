"""随机老婆：群友每天抽一个老婆，抽到两次别人的老婆就抢过来。

移植自旧版 xiaomai-bot 的 random_wife。这里只声明命令和事件监听，取出参数后交给对应模块：

- :mod:`.daily` 抽老婆、离婚（规则在 :mod:`.marriage`）
- :mod:`.manage` 老婆库的添加、删除、投稿审核和老婆列表

老婆库（图片文件）在 :mod:`.library`，老婆列表的排版在 :mod:`.pages`。
"""

from __future__ import annotations

from typing import Any

from arclet.alconna import AllParam, Args, Option
from arclet.entari import MessageCreatedEvent, Session, plugin
from arclet.entari.command import Query
from tenko_api.features import declare, rate_limit
from tenko_api.perm import Permission, group_only, require
from tenko_api.render import RenderService

from . import daily, manage
from .config import RandomWifeConfig
from .entry import answer, commands, display_name
from .library import LibraryError

plugin.metadata(
    "随机老婆",
    author=["13"],
    version="0.2.0",
    description="群里每天抽一个老婆，抽到两次别人的老婆就抢过来；老婆库由管理员维护，群员可投稿。",
    urls={
        "homepage": "https://github.com/g1331/tenko-plugins/tree/main/plugins/random_wife",
        "repository": "https://github.com/g1331/tenko-plugins",
    },
    config=RandomWifeConfig,
)
declare(scope="group", default_enabled=True)


# —— 审核回复 ——


@plugin.listen(MessageCreatedEvent)
async def on_review_reply(session: Session[MessageCreatedEvent]):
    await manage.on_group_reply(session)


@plugin.listen(MessageCreatedEvent)
async def on_master_reply(session: Session[MessageCreatedEvent]):
    """没配审核群时投稿发给 Master，Master 引用那条通知回复。"""

    await manage.on_master_reply(session)


# —— 命令 ——


@commands(
    ["抽老婆", "随机老婆", "随机wife"],
    Args(),
    "抽一个今天的老婆；抽到别人的老婆两次就抢过来，每天零点重置",
    "抽老婆",
    "/抽老婆",
)
@group_only()
@rate_limit(3)
async def draw_wife(session: Session):
    return await daily.draw(session.event.channel.id, session.user.id, display_name(session))


@commands(["离婚"], Args(), "放弃今天的老婆，可以重新抽", "离婚", "/离婚")
@group_only()
async def give_up_wife(session: Session):
    return await daily.divorce(session.event.channel.id, session.user.id)


@commands(
    ["添加老婆", "添加wife"],
    Args["name", str]["rest?", AllParam],
    "把一张图片加进老婆库，名字就是老婆的名字；BOT 管理员直接加，群员投稿交给审核群或 Master",
    "添加老婆 <名字> [图片]",
    "/添加老婆 芙莉莲 [图片]",
)
async def add_wife_command(
    session: Session,
    name: Query[str] = Query("name"),  # noqa: B008
    rest: Query[Any] = Query("rest"),  # noqa: B008
):
    try:
        wife = manage.wife_name(name, rest)
    except LibraryError as error:
        return str(error)
    return await answer(manage.add_wife(session, wife))


@commands(
    ["删除老婆", "删除wife"],
    Args["name", str]["rest?", AllParam],
    "从老婆库里删掉一个老婆",
    "删除老婆 <名字>",
    "/删除老婆 芙莉莲",
)
@require(Permission.BotAdmin)
async def remove_wife_command(
    name: Query[str] = Query("name"),  # noqa: B008
    rest: Query[Any] = Query("rest"),  # noqa: B008
):
    try:
        wife = manage.wife_name(name, rest)
    except LibraryError as error:
        return str(error)
    return await manage.remove_wife(wife)


@commands(
    ["老婆列表"],
    Args["keyword?", str],
    "看看老婆库里有谁：缩略图、名字和文件大小；后面的文字都按名字搜，翻页用 -p",
    "老婆列表 [关键词] [-p 页码]",
    "/老婆列表 中野 -p 2",
    Option("-p|-page|-页", Args["page", int], dest="page", help_text="第几页"),
)
async def list_wives(
    keyword: Query[str] = Query("keyword"),  # noqa: B008
    page: Query[int] = Query("page.page"),  # noqa: B008
    *,
    render_service: RenderService,
):
    return await manage.wife_list(
        keyword.result if keyword.available else None,
        page.result if page.available else 1,
        render_service,
    )
