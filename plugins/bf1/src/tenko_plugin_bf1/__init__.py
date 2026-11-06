"""战地一查询：战绩、武器、载具、最近对局和服务器，行动和交换，百科和小贴士，以及入群审核。

这里只声明命令和事件监听，每条命令取出参数后交给对应模块：

- :mod:`.stats` 玩家战绩、武器载具、信息、最近对局、收藏
- :mod:`.servers` 搜服务器、服务器详情
- :mod:`.game` 全服概况、行动、交换
- :mod:`.wiki` 百科、小贴士、戳一戳
- :mod:`.binding` 绑定 EA 账号
- :mod:`.review` 入群审核

数据来自 bf-manager 的公开查询接口（:mod:`.api`），图片排版在 :mod:`.pages`，渲染不了时回文字。
"""

from __future__ import annotations

from typing import Any

from arclet.alconna import Alconna, AllParam, Args, CommandMeta, Option
from arclet.entari import MessageCreatedEvent, Session, command, plugin
from arclet.entari.command import Query
from arclet.entari.event.base import GuildMemberAddedEvent, InternalEvent
from tenko_api.features import declare, feature_required
from tenko_api.perm import Permission, require_perm
from tenko_api.render import RenderService

from . import binding, game, groups, review, servers, stats, wiki
from .api import Bf1Error
from .config import Bf1Config
from .entry import (
    ITEM_OPTIONS,
    TEXT,
    card_of,
    commands,
    group_and_name,
    items_answer,
    items_commands,
    keyword_of,
    name_of,
    player_commands,
    service_for,
)
from .reply import answer

plugin.metadata(
    "战地一查询",
    author=["13"],
    version="0.1.22",
    description=(
        "经 bf-manager 查询战地一玩家战绩、武器、载具、最近对局、玩家信息、收藏和服务器，"
        "以及行动和交换，带百科、小贴士（被戳时随机发）和入群审核。"
    ),
    urls={
        "homepage": "https://github.com/g1331/tenko-plugins/tree/main/plugins/bf1",
        "repository": "https://github.com/g1331/tenko-plugins",
    },
    config=Bf1Config,
)
declare(scope="group", default_enabled=True)


# —— 玩家 ——


@player_commands(["bf1s", "stat", "生涯", "战绩"], "生涯战绩")
async def bf1s(
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


@items_commands("weapons", ["bf1w", "wp", "weapon", "武器"])
async def bf1w(
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
    group, name = group_and_name("weapons", first, second)
    return await items_answer(
        "weapons", group, name, session, text, cols, rows, search, sort, render_service
    )


@items_commands("vehicles", ["bf1v", "vc", "vehicle", "载具"])
async def bf1v(
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
    group, name = group_and_name("vehicles", first, second)
    return await items_answer(
        "vehicles", group, name, session, text, cols, rows, search, sort, render_service
    )


def group_command(kind: str, group: groups.Group):
    """旧 bot 的 -步枪、-突击兵、-坦克 等：每个名字一条命令，不进帮助列表。

    处理函数得在插件自己的模块里定义，entari 不认子模块里注册的。
    """

    async def handler(
        session: Session,
        name: Query[str] = Query("name"),  # noqa: B008
        text: Query[Any] = Query("text"),  # noqa: B008
        cols: Query[int] = Query("cols.cols"),  # noqa: B008
        rows: Query[int] = Query("rows.rows"),  # noqa: B008
        search: Query[str] = Query("search.keyword"),  # noqa: B008
        sort: Query[str] = Query("sort.sort"),  # noqa: B008
        *,
        render_service: RenderService,
    ):
        return await items_answer(
            kind, group, name_of(name), session, text, cols, rows, search, sort, render_service
        )

    what = "武器" if kind == "weapons" else "载具"
    for word in group.names:
        meta = CommandMeta(
            f"查询战地一玩家的{group.label}{'' if group.label.endswith(what) else what}击杀排行",
            usage=f"{word} [EA 名字] [-n 名字] [-s 排序] [-c 列] [-r 行] [-t]",
            hide=True,
        )
        args = Args["name?", str]
        command.on(Alconna(word, args, *ITEM_OPTIONS, meta=meta))(handler)
    return handler


# 旧 bot 的 -步枪、-突击兵、-坦克 等，一个分组名一条隐藏命令
GROUP_HANDLERS = {
    (kind, group.label): group_command(kind, group)
    for kind, kind_groups in groups.GROUPS.items()
    for group in kind_groups
}


# 旧 bot 天眼查的 -o/-a/-v/-b：把这类身份的服全部列出来
FULL = {
    "owner": Option("-o|-owner|-服主", dest="owner"),
    "admin": Option("-a|-admin|-管理", dest="admin"),
    "vip": Option("-v|-vip", dest="vip"),
    "ban": Option("-b|-ban|-封禁", dest="ban"),
}


@player_commands(
    ["bf1info", "info", "信息", "tyc", "天眼查"],
    "信息：私服身份、最近玩过的服、战队和封禁标记；-o 服主 -a 管理 -v VIP -b 封禁，"
    "列出这类身份的全部服务器",
    " [-o|-a|-v|-b]",
    " -b",
    *FULL.values(),
)
async def bf1info(
    session: Session,
    name: Query[str] = Query("name"),  # noqa: B008
    text: Query[Any] = Query("text"),  # noqa: B008
    owner: Query[Any] = Query("owner"),  # noqa: B008
    admin: Query[Any] = Query("admin"),  # noqa: B008
    vip: Query[Any] = Query("vip"),  # noqa: B008
    ban: Query[Any] = Query("ban"),  # noqa: B008
    *,
    render_service: RenderService,
):
    flags = {"owner": owner, "admin": admin, "vip": vip, "ban": ban}
    full = tuple(key for key, flag in flags.items() if flag.available)
    service = service_for(text, render_service)
    return await answer(
        stats.info_reply(name_of(name), service, session.user.id, full, card_of(session))
    )


@player_commands(
    ["bf1c", "收藏", "勋章", "狗牌", "collection"], "收藏：佩戴的狗牌、勋章、狗牌和勋带"
)
async def bf1c(
    session: Session,
    name: Query[str] = Query("name"),  # noqa: B008
    text: Query[Any] = Query("text"),  # noqa: B008
    *,
    render_service: RenderService,
):
    service = service_for(text, render_service)
    return await answer(
        stats.collection_reply(name_of(name), service, session.user.id, card_of(session))
    )


# 旧 bot 的 -n：查几局；一局一个详情请求，所以设上限
MAX_RECENT = 12
COUNT = Option(
    "-n|-num|-局数", Args["count", int], dest="count", help_text=f"查几局，最多 {MAX_RECENT}"
)


@player_commands(
    ["bf1r", "recent", "match", "最近", "对局"],
    f"最近对局，默认 {stats.RECENT} 局，-n 指定局数（最多 {MAX_RECENT}）",
    " [-n 局数]",
    " -n 10",
    COUNT,
)
async def bf1r(
    session: Session,
    name: Query[str] = Query("name"),  # noqa: B008
    text: Query[Any] = Query("text"),  # noqa: B008
    count: Query[int] = Query("count.count"),  # noqa: B008
    *,
    render_service: RenderService,
):
    rounds = max(1, min(count.result, MAX_RECENT)) if count.available else stats.RECENT
    service = service_for(text, render_service)
    return await answer(
        stats.recent_reply(name_of(name), service, session.user.id, rounds, card_of(session))
    )


@commands(
    ["bf1绑定", "bf1b", "绑定", "bind"],
    Args["name?", str],
    "把自己的 EA 名字记下来，之后查战绩可以不带名字；不带名字查看当前绑定",
    "bf1绑定 [EA 名字]",
    "/bf1绑定 SHlSAN13",
)
async def bf1bind(session: Session, name: Query[str] = Query("name")):  # noqa: B008
    return await answer(binding.bind_reply(name_of(name), session.user.id))


# —— 服务器 ——


@commands(
    ["bf1ss", "ss", "搜服务器"],
    Args["keyword", AllParam],
    "按名字搜索有人的战地一私服",
    "bf1ss <服务器名关键字>",
    "/bf1ss sakula",
)
async def bf1ss(
    session: Session,
    keyword: Query[list] = Query("keyword"),  # noqa: B008
    *,
    render_service: RenderService,
):
    if not (text := keyword_of(keyword)):
        return "用法：/bf1ss <服务器名关键字>"
    return await answer(servers.servers_reply(text, render_service))


@commands(
    ["bf1pl", "ds", "详细服务器"],
    Args["keyword", AllParam],
    "查看战地一服务器的玩家列表；关键字匹配多个时显示人最多的",
    "bf1pl <服务器名关键字 | gameId>",
    "/bf1pl pigu",
)
async def bf1pl(
    session: Session,
    keyword: Query[list] = Query("keyword"),  # noqa: B008
    *,
    render_service: RenderService,
):
    if not (text := keyword_of(keyword)):
        return "用法：/bf1pl <服务器名关键字 | gameId>"
    return await answer(servers.server_reply(text, render_service))


# —— 全服 ——


@commands(["bf1", "bfstat"], Args(), "战地一全服在线人数、地区分布和热门地图", "bf1", "/bf1")
async def bf1(session: Session, *, render_service: RenderService):
    return await answer(game.overview_reply(render_service))


@commands(
    ["bf1行动", "行动", "战役", "op"],
    Args(),
    "战地一当前的行动战役：包含哪些行动、还剩多久、每日什么时候重置；加 -t 只回文字",
    "bf1行动 [-t]",
    "/bf1行动",
    TEXT,
)
async def bf1op(
    session: Session,
    text: Query[Any] = Query("text"),  # noqa: B008
    *,
    render_service: RenderService,
):
    return await answer(game.campaign_reply(service_for(text, render_service)))


# 交换的 -t 沿用旧 bot，是日期；只回文字用 -text
EXCHANGE_TEXT = Option("-text|--text", dest="text", help_text="只回文字，不出图")
EXCHANGE_DAY = Option("-t|-time|-日期", Args["day", str], dest="day", help_text="日期")


@commands(
    ["bf1交换", "交换", "exchange", "ex"],
    Args["when?", str],
    "战地一交换（废料兑换皮肤）：不带参数看最新一期，带日期看那天的一期，"
    "发 列表 按年列出全部期数；加 -text 只回文字",
    "bf1交换 [日期 | 列表] [-text]",
    "/bf1交换 2023.01.01",
    EXCHANGE_DAY,
    EXCHANGE_TEXT,
)
async def bf1exchange(
    session: Session,
    when: Query[str] = Query("when"),  # noqa: B008
    day: Query[str] = Query("day.day"),  # noqa: B008
    text: Query[Any] = Query("text"),  # noqa: B008
    *,
    render_service: RenderService,
):
    word = (name_of(day) or name_of(when) or "").strip()
    return await answer(game.exchange_query(word, service_for(text, render_service)))


# —— 百科和小贴士 ——


@commands(
    ["bf1百科", "百科", "wiki"],
    Args["keyword?", AllParam],
    "战地一游戏里的百科（典籍）；不带关键词随机看一条",
    "bf1百科 [序号 | 分类 | 关键词 | 目录]",
    "/bf1百科 MP18",
)
async def bf1wiki(
    session: Session,
    keyword: Query[list] = Query("keyword"),  # noqa: B008
    *,
    render_service: RenderService,
):
    text = keyword_of(keyword) if keyword.available else ""
    return await answer(wiki.wiki_reply(text, render_service))


@commands(
    ["bf1tip", "tip", "小贴士"],
    Args["keyword?", AllParam],
    "战地一载入画面的小贴士；不带关键词随机看一条，可以按地图、模式或正文搜",
    "bf1tip [关键词]",
    "/bf1tip 亚眠",
)
async def bf1tip(session: Session, keyword: Query[list] = Query("keyword")):  # noqa: B008
    text = keyword_of(keyword) if keyword.available else ""
    try:
        return wiki.tip_reply(text)
    except Bf1Error as error:
        return str(error)


@plugin.listen(InternalEvent)
async def on_poke(event: InternalEvent, *, render_service: RenderService):
    await wiki.on_poke(event, render_service)


# —— 入群审核 ——


@plugin.listen(GuildMemberAddedEvent)
async def on_join_request(
    session: Session[GuildMemberAddedEvent], *, render_service: RenderService
):
    await review.on_join(session, render_service)


@plugin.listen(MessageCreatedEvent)
@feature_required()
async def on_review_reply(session: Session[MessageCreatedEvent]):
    await review.on_reply(session)


review_command = Alconna(
    "bf1审核",
    Args["switch?", ["开", "关"]],
    meta=CommandMeta(
        "入群审核：有人申请入群时查他填的 EA 名字并发到群里，管理员回复同意或拒绝；"
        "需要 bot 是群管理员，入群问题让申请者填 EA 名字",
        usage="bf1审核 [开|关]",
        example="/bf1审核 开",
    ),
)


@command.on(review_command)
async def bf1review(session: Session, switch: Query[str] = Query("switch")):  # noqa: B008
    if session.event.guild is None:
        return "入群审核只能在群里开关"
    if switch.available and not await require_perm(session, Permission.GroupAdmin):
        return "只有群管理员及以上能开关入群审核"
    return await review.switch(session.guild.id, switch.result if switch.available else None)
