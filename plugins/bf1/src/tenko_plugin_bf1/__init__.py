"""战地一查询：战绩、武器、载具、最近对局和服务器，百科和小贴士，以及入群审核。

查询数据来自 bf-manager 的公开查询接口，百科和小贴士在 :mod:`.codex`；这里解析命令、取数据，
图片排版在 :mod:`.pages`，渲染不了时回文字。
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from arclet.alconna import Alconna, AllParam, Args, CommandMeta
from arclet.entari import (
    BasicConfModel,
    MessageChain,
    MessageCreatedEvent,
    Session,
    command,
    plugin,
)
from arclet.entari.command import Query
from arclet.entari.event.base import GuildMemberAddedEvent, InternalEvent
from loguru import logger
from satori import At, Author, Element, Image, Quote, Text
from sqlalchemy import BigInteger, String, select
from sqlalchemy.orm import Mapped, mapped_column
from tenko_api.accounts import Account, accounts_for_group, online_accounts
from tenko_api.features import declare, feature_enabled, feature_required
from tenko_api.perm import Permission, require_perm
from tenko_api.render import RenderService
from tenko_api.storage import Model, Store, get_session

from . import codex, pages


class Bf1Config(BasicConfModel):
    """对应 config/tenko.toml 中的 ``[plugin_config.bf1]``。"""

    base_url: str = "https://bfm.umrbotech.com"
    """bf-manager 地址，不带 /api/v1。"""


plugin.metadata(
    "战地一查询",
    author=["13"],
    version="0.1.15",
    description=(
        "经 bf-manager 查询战地一玩家战绩、武器、载具、最近对局和服务器，"
        "带百科、小贴士（被戳时随机发）和入群审核。"
    ),
    urls={
        "homepage": "https://github.com/g1331/tenko-plugins/tree/main/plugins/bf1",
        "repository": "https://github.com/g1331/tenko-plugins",
    },
    config=Bf1Config,
)
declare(scope="group", default_enabled=True)

config = plugin.get_config(Bf1Config)

TOP = 10
ITEM_CARDS = 10
RECENT = 6


class Binding(Model):
    """QQ 号 → 绑定的 EA 账号（按 PID 查，改名不影响）。"""

    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    pid: Mapped[int] = mapped_column(BigInteger)
    # 从旧 bot 迁过来的绑定有些没有名字
    name: Mapped[str | None] = mapped_column(String(64))


# 0.1.4–0.1.5 把绑定存在这个 JSON 里，第一次用到绑定时导入数据库
_legacy = Store("bindings", version=1)
_legacy_lock = asyncio.Lock()


async def _import_legacy() -> None:
    if not _legacy.path.exists():
        return
    async with _legacy_lock:
        if not _legacy.path.exists():
            return
        data = _legacy.load(default={})
        async with get_session() as db:
            existing = set(await db.scalars(select(Binding.user_id)))
            db.add_all(
                Binding(user_id=user_id, pid=bound["pid"], name=bound.get("name"))
                for user_id, bound in data.items()
                if user_id not in existing
            )
            await db.commit()
        _legacy.path.rename(_legacy.path.with_name(f"{_legacy.path.name}.imported"))
        logger.info("已把 {} 条绑定从 {} 导入数据库", len(data), _legacy.path.name)


async def _bound(user_id: str) -> Binding | None:
    await _import_legacy()
    async with get_session() as db:
        return await db.get(Binding, user_id)


class Bf1Error(Exception):
    """要原样回给群友的错误。"""


async def _get(path: str, **params: Any) -> Any:
    # EA 接口慢，战绩和武器常要好几秒。
    url = f"{config.base_url.rstrip('/')}/api/v1/bf1{path}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, params=params)
    except httpx.HTTPError as error:
        logger.warning("bf-manager 请求失败：{} {}", url, type(error).__name__)
        raise Bf1Error("查询服务暂时连不上，稍后再试") from error
    if response.status_code >= 400:
        try:
            message = response.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = f"查询失败（{response.status_code}）"
        raise Bf1Error(message)
    return response.json()


async def _persona(name: str) -> dict[str, Any]:
    result = await _get("/players/search", name=name)
    personas = result["personas"]
    # 搜索是模糊匹配，精确同名的不一定排第一。
    exact = [p for p in personas if p["display_name"].casefold() == name.casefold()]
    if not exact:
        if not personas:
            raise Bf1Error(f"没找到玩家 {name}")
        names = "、".join(p["display_name"] for p in personas[:5])
        raise Bf1Error(f"没找到玩家 {name}，相近的有：{names}")
    # 重名时 bf-manager 会回填生涯时长，0 是没玩过 BF1 的空号。
    return next((p for p in exact if p.get("time_played_hours")), exact[0])


def _hours(seconds: float | None) -> str:
    return f"{(seconds or 0) / 3600:.1f} 小时"


def _kpm(kills: int | None, seconds: float | None) -> str:
    return f"{(kills or 0) / (seconds / 60):.2f}" if seconds else "-"


def _n(value: Any) -> Any:
    return "-" if value is None else value


def format_stats(name: str, stats: dict[str, Any]) -> str:
    s = stats["summary"]
    wins, losses = s.get("wins") or 0, s.get("losses") or 0
    win_rate = f"{wins / (wins + losses):.1%}" if wins + losses else "-"
    kd = f"{s['kd']:.2f}" if s.get("kd") is not None else "-"
    kpm = f"{s['kpm']:.2f}" if s.get("kpm") is not None else "-"
    best = pages.CLASSES.get(s.get("best_class") or "", s.get("best_class") or "-")
    longest = s.get("longest_headshot_meters")
    return "\n".join(
        [
            f"{name} 的战地一战绩",
            f"等级 {_n(s.get('rank'))} | 技巧 {_n(s.get('skill'))} | "
            f"时长 {_hours(s.get('time_played_seconds'))}",
            f"击杀 {_n(s.get('kills'))} | 死亡 {_n(s.get('deaths'))} | KD {kd} | KPM {kpm}",
            f"胜 {wins} | 负 {losses} | 胜率 {win_rate}",
            f"步兵击杀 {_n(s.get('infantry_kills'))} | 载具击杀 {_n(s.get('vehicle_kills'))}",
            f"协助 {_n(s.get('assists'))} | 复活 {_n(s.get('revives'))} | "
            f"治疗 {_n(s.get('heals'))} | 修理 {_n(s.get('repairs'))}",
            f"狗牌 {_n(s.get('dogtags'))} | 最高连杀 {_n(s.get('max_killstreak'))} | "
            f"最远爆头 {f'{longest:.1f} 米' if longest is not None else '-'}",
            f"最佳兵种 {best}",
        ]
    )


def format_weapons(name: str, weapons: list[dict[str, Any]]) -> str:
    top = pages.top_kills(weapons, TOP)
    if not top:
        return f"{name} 还没有武器击杀记录"
    lines = [f"{name} 的武器击杀前 {len(top)}"]
    for index, w in enumerate(top, 1):
        accuracy = f"{w['accuracy']:.1f}%" if w.get("accuracy") is not None else "-"
        headshot = f"{(w.get('headshots') or 0) / w['kills']:.1%}"
        lines.append(
            f"{index}. {pages.cn(w.get('name'))} · {pages.cn(w.get('category')) or '-'}\n"
            f"   击杀 {w['kills']} | KPM {_kpm(w['kills'], w.get('time_seconds'))} | "
            f"命中 {accuracy} | 爆头 {headshot} | {_hours(w.get('time_seconds'))}"
        )
    return "\n".join(lines)


def format_vehicles(name: str, vehicles: list[dict[str, Any]]) -> str:
    top = pages.top_kills(vehicles, TOP)
    if not top:
        return f"{name} 还没有载具击杀记录"
    lines = [f"{name} 的载具击杀前 {len(top)}"]
    for index, v in enumerate(top, 1):
        lines.append(
            f"{index}. {pages.cn(v.get('name'))} · {pages.cn(v.get('category')) or '-'}\n"
            f"   击杀 {v['kills']} | KPM {_kpm(v['kills'], v.get('time_seconds'))} | "
            f"摧毁 {v.get('destroyed') or 0} | {_hours(v.get('time_seconds'))}"
        )
    return "\n".join(lines)


def format_servers(keyword: str, servers: list[dict[str, Any]], total: int) -> str:
    if not servers:
        return f"没找到名字含「{keyword}」的服务器（空服不在列表里）"
    more = f"，人最多的 {len(servers)} 个" if total > len(servers) else ""
    lines = [f"「{keyword}」找到 {total} 个服务器{more}："]
    for index, s in enumerate(servers, 1):
        queue = f"[{s['queue_count']}]" if s.get("queue_count") else ""
        lines.append(
            f"{index}. {s['name']}\n"
            f"   {s['player_count']}/{s['max_player_count']}{queue} | "
            f"{s.get('mode_display_name') or '-'}·{s.get('map_display_name') or '-'} | "
            f"{s.get('region_display_name') or '-'} | gameId {s.get('game_id')}"
        )
    return "\n".join(lines)


def _source() -> str:
    return urlparse(config.base_url).netloc or config.base_url


async def _optional(path: str) -> Any:
    """封禁、战队、在线状态这类附加信息拿不到就不显示，不影响主战绩。"""

    try:
        return await _get(path)
    except Bf1Error:
        return None


def _player_link(pid: int, tab: str = "") -> str:
    """bf-manager 网页的玩家页，分区记在 # 后面。"""

    return f"{config.base_url.rstrip('/')}/bf1/player/{pid}{tab and '#' + tab}"


async def _reply(
    service, template: str, context, fallback: str, link: str | None = None
) -> MessageChain | str:
    image = await pages.render(service, template, context)
    if not image:
        return f"{fallback}\n{link}" if link else fallback
    chain = MessageChain(Image.of(raw=image, mime="image/jpeg"))
    return chain + Text(link) if link else chain


async def _player(name: str | None, user_id: str) -> dict[str, Any]:
    """带名字按名字查；不带就查发消息的人绑定的号（按 PID，改名也不影响）。"""

    if name:
        return await _persona(name)
    bound = await _bound(user_id)
    if not bound:
        raise Bf1Error("还没绑定：使用 /bf1绑定 你的EA名字，或者在命令后面带上名字")
    return await _get(f"/players/{bound.pid}")


async def stats_reply(name: str | None, service, user_id: str = "") -> MessageChain | str:
    reply, _, _ = await _stats_card(await _player(name, user_id), service)
    return reply


async def _stats_card(persona: dict[str, Any], service) -> tuple[MessageChain | str, Any, Any]:
    """战绩卡片，连同取到的生涯数据和封禁状态（入群审核要用）。"""

    pid = persona["persona_id"]
    stats, weapons, vehicles, ban, platoon, online = await asyncio.gather(
        _get(f"/stats/{pid}"),
        _get(f"/stats/{pid}/weapons"),
        _get(f"/stats/{pid}/vehicles"),
        _optional(f"/stats/{pid}/ban"),
        _optional(f"/stats/{pid}/platoon"),
        _optional(f"/stats/{pid}/online"),
    )
    context = pages.stats_page(
        persona,
        stats,
        weapons["weapons"],
        vehicles["vehicles"],
        ban,
        platoon,
        online,
        _source(),
    )
    reply = await _reply(
        service,
        "stats.html",
        context,
        format_stats(persona["display_name"], stats),
        _player_link(pid),
    )
    return reply, stats, ban


async def items_reply(
    kind: str, name: str | None, service, user_id: str = ""
) -> MessageChain | str:
    persona = await _player(name, user_id)
    pid = persona["persona_id"]
    result, platoon = await asyncio.gather(
        _get(f"/stats/{pid}/{kind}"), _optional(f"/stats/{pid}/platoon")
    )
    items = result[kind]
    context = pages.items_page(kind, persona, platoon, items, _source(), ITEM_CARDS)
    text = (format_weapons if kind == "weapons" else format_vehicles)(
        persona["display_name"], items
    )
    return await _reply(service, "items.html", context, text, _player_link(pid, kind))


def format_recent(name: str, context: dict[str, Any]) -> str:
    rounds = context["rounds"]
    lines = [
        f"{name} 最近 {len(rounds)} 局",
        " | ".join(f"{label} {value}" for label, value in context["kpis"]),
    ]
    for index, r in enumerate(rounds, 1):
        gear = "、".join(f"{g['name']} {g['kills']} 杀" for g in r["gear"])
        lines += [
            f"{index}. {r['when']} {r['map']}·{r['mode'] or '-'} {r['outcome']}",
            f"   {r['kd']} | KD {r['ratio']} | KPM {r['kpm']} | {r['minutes']} | {r['kit']}",
            *([f"   {gear}"] if gear else []),
            f"   {r['server']}",
        ]
    return "\n".join(lines)


async def _recent_card(
    persona: dict[str, Any], service, link: bool = True
) -> MessageChain | str | None:
    """最近对局卡片；bf-manager 没记录到能看的对局时返回 None。"""

    pid = persona["persona_id"]
    # 多取一些：排队、观战、进服就走的局会被筛掉
    result, platoon = await asyncio.gather(
        _get(f"/stats/{pid}/rounds", limit=20), _optional(f"/stats/{pid}/platoon")
    )
    context = pages.recent_page(persona, platoon, result["items"], _source(), RECENT)
    if not context["rounds"]:
        return None
    text = format_recent(persona["display_name"], context)
    return await _reply(
        service, "recent.html", context, text, _player_link(pid, "rounds") if link else None
    )


async def recent_reply(name: str | None, service, user_id: str = "") -> MessageChain | str:
    persona = await _player(name, user_id)
    card = await _recent_card(persona, service)
    if card is None:
        raise Bf1Error(
            f"还没有记录到 {persona['display_name']} 的对局（bf-manager 跟踪到的才有，保留 90 天）"
        )
    return card


async def _bind(user_id: str, persona: dict[str, Any]) -> None:
    await _import_legacy()
    async with get_session() as db:
        await db.merge(
            Binding(user_id=user_id, pid=persona["persona_id"], name=persona["display_name"])
        )
        await db.commit()


async def bind_reply(name: str | None, user_id: str) -> str:
    if not name:
        bound = await _bound(user_id)
        if not bound:
            return "用法：/bf1绑定 <EA 名字>"
        return f"当前绑定：{bound.name or '名字未知'}（PID {bound.pid}）"
    persona = await _persona(name)
    before = await _bound(user_id)
    await _bind(user_id, persona)
    pid, new = persona["persona_id"], persona["display_name"]
    reply = f"已绑定 {new}（PID {pid}）"
    if before is None:
        return reply
    old = before.name or "名字未知"
    if before.pid != pid:
        return f"绑定变更：{old}（PID {before.pid}）→ {new}（PID {pid}）"
    if before.name and before.name != new:
        return f"{reply}，改过名：{old} → {new}"
    return reply


async def _search_servers(keyword: str) -> tuple[list[dict[str, Any]], int]:
    result = await _get("/servers", name=keyword, limit=100)
    # 接口不按人数排；多取一些自己排，total 是全部匹配数。
    servers = sorted(result["items"], key=lambda s: s.get("player_count") or 0, reverse=True)
    return servers, result["total"]


async def servers_reply(keyword: str, service) -> MessageChain | str:
    servers, total = await _search_servers(keyword)
    servers = servers[:TOP]
    context = pages.servers_page(keyword, servers, total, _source())
    return await _reply(service, "servers.html", context, format_servers(keyword, servers, total))


def format_server(detail: dict[str, Any], players: dict[str, Any] | None) -> str:
    s = detail["summary"]
    owner = ((detail.get("extras") or {}).get("owner") or {}).get("display_name") or "-"
    lines = [
        s["name"],
        f"{s.get('player_count')}/{s.get('max_player_count')} | "
        f"{s.get('mode_display_name') or '-'}·{s.get('map_display_name') or '-'} | "
        f"{s.get('region_display_name') or '-'} | gameId {s.get('game_id')}",
        f"服主 {owner}",
    ]
    for team in (players or {}).get("teams") or []:
        names = "、".join(p.get("display_name") or "-" for p in team.get("players") or [])
        lines.append(f"{team.get('faction') or '-'}（{len(team.get('players') or [])}）：{names}")
    for label, key in [("排队", "queued"), ("观战", "spectators")]:
        if waiting := (players or {}).get(key):
            names = "、".join(p.get("display_name") or "-" for p in waiting)
            lines.append(f"{label}（{len(waiting)}）：{names}")
    return "\n".join(lines)


async def server_reply(keyword: str, service) -> MessageChain | str:
    note = ""
    # gameId 是十几位数；短数字（比如服名里的 3090）按名字搜
    if keyword.isdigit() and len(keyword) >= 10:
        game_id = int(keyword)
    else:
        servers, total = await _search_servers(keyword)
        if not servers:
            raise Bf1Error(f"没找到名字含「{keyword}」的服务器（空服不在列表里）")
        game_id = servers[0]["game_id"]
        if total > 1:
            note = f"其他 {total - 1} 个：/bf1ss {keyword}"
    detail, players = await asyncio.gather(
        _get(f"/servers/{game_id}"), _optional(f"/servers/{game_id}/players")
    )
    context = pages.server_page(detail, players, _source())
    # 匹配说明和网页链接跟在图后面当文字，不画进图里
    link = f"{config.base_url.rstrip('/')}/bf1/server/{game_id}#players"
    fallback = format_server(detail, players)
    return await _reply(
        service, "server.html", context, fallback, f"{note}\n{link}" if note else link
    )


def format_overview(overview: dict[str, Any]) -> str:
    players, servers = overview.get("players") or {}, overview.get("servers") or {}
    lines = [
        f"战地一当前在线 {players.get('total', '-')} 人，{servers.get('total', '-')} 个服务器",
        f"亚服 {players.get('asia', '-')} | 欧服 {players.get('eu', '-')} | "
        f"其他 {players.get('other', '-')}",
    ]
    for m in (overview.get("top_map_modes") or [])[:5]:
        lines.append(f"{m.get('label')}：{m.get('players')} 人 / {m.get('servers')} 服")
    return "\n".join(lines)


async def overview_reply(service) -> MessageChain | str:
    overview = await _get("/overview")
    if not overview.get("available"):
        raise Bf1Error("全服概况暂时没有数据，稍后再试")
    context = pages.overview_page(overview, _source())
    link = f"{config.base_url.rstrip('/')}/stats"
    return await _reply(service, "overview.html", context, format_overview(overview), link)


async def _answer(query) -> MessageChain | str:
    try:
        return await query
    except Bf1Error as error:
        return str(error)


def _meta(description: str, usage: str, example: str, aliases: list[str]) -> CommandMeta:
    """主命令的说明里列出旧名字；旧名字本身不进帮助列表。"""

    also = "；也可以用 " + " ".join(f"/{a}" for a in aliases) if aliases else ""
    return CommandMeta(description + also, usage=usage, example=example)


def _commands(names: list[str], args: Args, description: str, usage: str, example: str):
    """一组同义命令：/bf1s 这类主命令，加上旧 bot 用过的 /stat、/战绩 等。

    不用 Alconna 别名或 shortcut：entari 先按命令名前缀派发，别名到不了处理函数，
    所以每个名字单独注册一条，共用同一个处理函数。
    """

    main, aliases = names[0], names[1:]

    def register(handler):
        command.on(Alconna(main, args, meta=_meta(description, usage, example, aliases)))(handler)
        for alias in aliases:
            meta = CommandMeta(description, usage=usage.replace(main, alias, 1), hide=True)
            command.on(Alconna(alias, args, meta=meta))(handler)
        return handler

    return register


def _player_commands(names: list[str], what: str):
    return _commands(
        names,
        Args["name?", str],
        f"查询战地一玩家的{what}，绑定过可以不带名字",
        f"{names[0]} [EA 名字]",
        f"/{names[0]} SHlSAN13",
    )


def _name(name: Query[str]) -> str | None:
    return name.result if name.available else None


@_player_commands(["bf1s", "stat", "生涯", "战绩"], "生涯战绩")
async def bf1s(
    session: Session,
    name: Query[str] = Query("name"),  # noqa: B008
    *,
    render_service: RenderService,
):
    return await _answer(stats_reply(_name(name), render_service, session.user.id))


@_player_commands(["bf1w", "wp", "weapon", "武器"], "武器击杀排行")
async def bf1w(
    session: Session,
    name: Query[str] = Query("name"),  # noqa: B008
    *,
    render_service: RenderService,
):
    return await _answer(items_reply("weapons", _name(name), render_service, session.user.id))


@_player_commands(["bf1v", "vc", "vehicle", "载具"], "载具击杀排行")
async def bf1v(
    session: Session,
    name: Query[str] = Query("name"),  # noqa: B008
    *,
    render_service: RenderService,
):
    return await _answer(items_reply("vehicles", _name(name), render_service, session.user.id))


@_player_commands(["bf1r", "recent", "match", "最近", "对局"], "最近对局")
async def bf1r(
    session: Session,
    name: Query[str] = Query("name"),  # noqa: B008
    *,
    render_service: RenderService,
):
    return await _answer(recent_reply(_name(name), render_service, session.user.id))


@_commands(
    ["bf1绑定", "bf1b", "绑定", "bind"],
    Args["name?", str],
    "把自己的 EA 名字记下来，之后查战绩可以不带名字；不带名字查看当前绑定",
    "bf1绑定 [EA 名字]",
    "/bf1绑定 SHlSAN13",
)
async def bf1bind(session: Session, name: Query[str] = Query("name")):  # noqa: B008
    return await _answer(bind_reply(_name(name), session.user.id))


def _keyword(keyword: Query[list]) -> str:
    return " ".join(str(part) for part in keyword.result).strip()


@_commands(
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
    if not (text := _keyword(keyword)):
        return "用法：/bf1ss <服务器名关键字>"
    return await _answer(servers_reply(text, render_service))


@_commands(
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
    if not (text := _keyword(keyword)):
        return "用法：/bf1pl <服务器名关键字 | gameId>"
    return await _answer(server_reply(text, render_service))


@_commands(["bf1", "bfstat"], Args(), "战地一全服在线人数、地区分布和热门地图", "bf1", "/bf1")
async def bf1(session: Session, *, render_service: RenderService):
    return await _answer(overview_reply(render_service))


# —— 百科和小贴士：游戏里的典籍和载入画面小贴士，被戳时随机发一条 ——

WIKI_LIST = 20
TIP_LIST = 5
POKE_COOLDOWN = 10
_WIKI_HINT = "可以搜武器、载具、地名、战役、人物，或者发分类名：" + "、".join(codex.CATEGORIES)


def format_entry(entry: codex.Entry) -> str:
    lines = [f"#{entry.no} {entry.title}（{entry.category}）", *entry.taglines]
    return "\n".join([*lines, f"解锁：{entry.unlock}", entry.description])


def _wiki_line(entry: codex.Entry) -> str:
    tagline = f" · {entry.taglines[0]}" if entry.taglines else ""
    return f"#{entry.no} {entry.title}{tagline}（{entry.category}）"


def format_tip(tip: codex.Tip) -> str:
    return f"【{'·'.join(tip.tags)}】{tip.text}" if tip.tags else tip.text


async def _wiki_card(entry: codex.Entry, service) -> MessageChain | str:
    context = pages.wiki_page(entry, len(codex.WIKI))
    return await _reply(service, "wiki.html", context, format_entry(entry))


async def wiki_reply(keyword: str, service) -> MessageChain | str:
    if not keyword:
        return await _wiki_card(codex.random_entry(), service)
    entries = codex.search_wiki(keyword)
    if not entries:
        raise Bf1Error(f"百科里没找到“{keyword}”。{_WIKI_HINT}")
    if len(entries) == 1:
        return await _wiki_card(entries[0], service)
    head = f"“{keyword}”找到 {len(entries)} 条，发 /百科 序号 查看"
    if keyword.strip() in codex.CATEGORIES:
        return f"{head}：\n" + "、".join(f"{e.no} {e.name}" for e in entries)
    lines = [_wiki_line(e) for e in entries[:WIKI_LIST]]
    if len(entries) > WIKI_LIST:
        lines.append(f"……还有 {len(entries) - WIKI_LIST} 条，换个更准的词")
    return f"{head}：\n" + "\n".join(lines)


def tip_reply(keyword: str) -> str:
    if not keyword:
        return format_tip(codex.random_tip())
    tips = codex.search_tips(keyword)
    if not tips:
        raise Bf1Error(f"没有带“{keyword}”的小贴士，可以搜地图名、模式名或者正文里的词")
    if len(tips) <= TIP_LIST:
        head = f"带“{keyword}”的小贴士有 {len(tips)} 条："
    else:
        head = f"带“{keyword}”的小贴士有 {len(tips)} 条，随机 {TIP_LIST} 条："
        tips = random.sample(tips, TIP_LIST)
    return head + "".join(f"\n· {format_tip(t)}" for t in tips)


async def poke_reply(user_id: str, service) -> list[Element]:
    """@ 戳的人，跟一条随机的小贴士或百科卡片。"""

    item = codex.pick()
    if isinstance(item, codex.Tip):
        return [At(user_id), Text(f"\n{format_tip(item)}")]
    card = await _wiki_card(item, service)
    return [At(user_id), Text("\n"), *_chain(card)]


@_commands(
    ["bf1百科", "百科", "wiki"],
    Args["keyword?", AllParam],
    "战地一游戏里的百科（典籍）；不带关键词随机看一条",
    "bf1百科 [序号 | 分类 | 关键词]",
    "/bf1百科 MP18",
)
async def bf1wiki(
    session: Session,
    keyword: Query[list] = Query("keyword"),  # noqa: B008
    *,
    render_service: RenderService,
):
    text = _keyword(keyword) if keyword.available else ""
    return await _answer(wiki_reply(text, render_service))


@_commands(
    ["bf1tip", "tip", "小贴士"],
    Args["keyword?", AllParam],
    "战地一载入画面的小贴士；不带关键词随机看一条，可以按地图、模式或正文搜",
    "bf1tip [关键词]",
    "/bf1tip 亚眠",
)
async def bf1tip(session: Session, keyword: Query[list] = Query("keyword")):  # noqa: B008
    text = _keyword(keyword) if keyword.available else ""
    try:
        return tip_reply(text)
    except Bf1Error as error:
        return str(error)


_poked: dict[tuple[str, str], float] = {}


@plugin.listen(InternalEvent)
async def on_poke(event: InternalEvent, *, render_service: RenderService):
    # satori 的 onebot11 适配器不转换戳一戳，原样作为 INTERNAL 事件送来
    origin = event._origin
    data = getattr(origin, "_data", None)
    if getattr(origin, "_type", None) == "notice.notify.poke" and isinstance(data, dict):
        await poked(event.account, data, render_service)


async def poked(receiver: Account, data: dict[str, Any], service) -> bool:
    """群里有人戳 bot 时回一条；回了返回 True。"""

    if data.get("group_id") is None:
        return False
    group, user, target = (str(data.get(k)) for k in ("group_id", "user_id", "target_id"))
    if not feature_enabled("bf1", group):
        return False
    # 宿主按群选路，事件只交给一个号；被戳的可能是群里另一个 bot 号，由它来回
    accounts = {str(a.self_id): a for a in accounts_for_group(group, platform=receiver.platform)}
    accounts.setdefault(str(receiver.self_id), receiver)
    if target not in accounts or user in accounts:
        return False
    now = time.monotonic()
    if now - _poked.get((group, user), -POKE_COOLDOWN) < POKE_COOLDOWN:
        return False
    if len(_poked) > 1000:
        _poked.clear()
    _poked[(group, user)] = now
    await accounts[target].protocol.send_message(group, await poke_reply(user, service))
    return True


# —— 入群审核：申请时把申请者填的 EA 名字查一遍发到群里，管理员回复这条消息同意或拒绝 ——

_REVIEW_HINT = "回复本消息「y」同意，回复其他文字作为理由拒绝"


class JoinReview(Model):
    """开了入群审核的群。"""

    group_id: Mapped[str] = mapped_column(String(32), primary_key=True)


class JoinRequest(Model):
    """待审的入群申请。

    不设时限：flag 是 QQ 群通知的 seq，NapCat 处理时在收到申请那个号最近 100 条群通知里找，
    申请还在通知里就能处理，bot 重启也不影响。处理掉或者申请者已经进群才删。
    """

    group_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    applicant: Mapped[str] = mapped_column(String(32), primary_key=True)
    # 每个管理员号的群通知各编各的 seq，得用收到申请的那个号处理
    self_id: Mapped[str] = mapped_column(String(32))
    flag: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[float]
    # 查到的 EA 号，同意时给申请者绑定
    pid: Mapped[int | None] = mapped_column(BigInteger)
    name: Mapped[str | None] = mapped_column(String(64))


# 认领申请、管理员抢着回复时，查和写之间不能插进别的协程
_request_lock = asyncio.Lock()


def _join_answer(comment: str) -> str:
    """QQ 把入群问题拼成「问题：…\\n答案：…」；没设问题时整段附言就是答案。"""

    return comment.rpartition("答案：")[2].strip()


async def _verdict(applicant: str, persona: dict[str, Any], stats: Any, ban: Any) -> str:
    if not stats["summary"].get("time_played_seconds"):
        return "有这个 EA 账号，但没玩过战地一"
    ban = ban or {}
    notes = [
        f"{source.upper()} 实锤 {ban.get(f'{source}_url') or ''}".rstrip()
        for source in ("bfeac", "bfban")
        if ban.get(source) == "hit"
    ]
    bound = await _bound(applicant)
    if bound and bound.pid != persona["persona_id"]:
        notes.append(f"这个 QQ 绑定的是 {bound.name or f'PID {bound.pid}'}，不是同一个号")
    return "；".join(notes) or "有效 ID"


async def review_request(
    applicant: str, nickname: str, comment: str, service
) -> tuple[list[Element], dict[str, Any] | None]:
    """审核消息和查到的玩家。审核消息第一行带申请者 QQ，管理员回复时靠它找回申请。"""

    answer = _join_answer(comment)
    persona = card = recent = None
    rounds_note = ""
    if not answer:
        verdict = "没填 EA 名字"
    else:
        try:
            persona = await _persona(answer)
            (card, stats, ban), recent = await asyncio.gather(
                _stats_card(persona, service), _optional_recent(persona, service)
            )
        except Bf1Error as error:
            verdict = str(error)
        else:
            verdict = await _verdict(applicant, persona, stats, ban)
            if recent is None and stats["summary"].get("time_played_seconds"):
                rounds_note = "最近对局：bf-manager 90 天内没记录到\n"
    head = (
        f"收到 {nickname}({applicant}) 的入群申请\n答案：{answer or '-'}\n检查：{verdict}\n"
        + rounds_note
    )
    parts = [Text(head), *_chain(card)]
    if recent is not None:
        # 两张都回退成文字时中间空一行；战绩卡片已经带了玩家页链接，对局卡片不再带
        gap = "\n\n" if isinstance(card, str) and isinstance(recent, str) else ""
        parts += _chain(f"{gap}{recent}" if isinstance(recent, str) else recent)
    return [*parts, Text(f"\n{_REVIEW_HINT}")], persona


def _chain(reply: MessageChain | str | None) -> list[Element]:
    if reply is None:
        return []
    return [Text(reply)] if isinstance(reply, str) else list(reply)


async def _optional_recent(persona: dict[str, Any], service) -> MessageChain | str | None:
    """审核时对局记录查不到不耽误战绩卡片。"""

    try:
        return await _recent_card(persona, service, link=False)
    except Bf1Error:
        return None


async def _claim(group_id: str, applicant: str, self_id: str, flag: str) -> bool:
    """同一份申请每个号各收到一次；几秒内已经有号接手的就不再处理。重复申请照常处理。"""

    async with _request_lock, get_session() as db:
        pending = await db.get(JoinRequest, (group_id, applicant))
        if pending is not None and pending.created_at > time.time() - 30:
            return False
        await db.merge(
            JoinRequest(
                group_id=group_id,
                applicant=applicant,
                self_id=self_id,
                flag=flag,
                created_at=time.time(),
                pid=None,
                name=None,
            )
        )
        await db.commit()
    return True


async def _remember_persona(group_id: str, applicant: str, persona: dict[str, Any]) -> None:
    async with get_session() as db:
        if pending := await db.get(JoinRequest, (group_id, applicant)):
            pending.pid, pending.name = persona["persona_id"], persona["display_name"]
            await db.commit()


async def _drop_request(group_id: str, applicant: str) -> None:
    async with get_session() as db:
        if pending := await db.get(JoinRequest, (group_id, applicant)):
            await db.delete(pending)
            await db.commit()


async def _review_on(group_id: str) -> bool:
    async with get_session() as db:
        return await db.get(JoinReview, group_id) is not None


async def _onebot(account: Account, action: str, **params: Any) -> Any:
    """OneBot 原始接口；失败时 satori 抛 ActionFailed。

    直接走 ``call_api``：Entari 包装 ``protocol.internal`` 时会把关键字参数整个包成一项
    ``kwargs``，NapCat 收不到参数。
    """

    # shortcut: 绕开 Entari 的包装缺陷，ArcletProject/Entari#32 修复后改回 protocol.internal。
    return await asyncio.wait_for(account.protocol.call_api(f"internal/{action}", params), 10)


async def rename_member(group: str, user_id: str, accounts: list[Account]) -> str | None:
    """进群的人绑定过 EA 号，就把群名片改成它现在的名字；改成了返回名字。

    进群通知只交给选路选中的一个号，它不一定是管理员，所以挨个问账号在群里的身份，
    用第一个群主或管理员号改。
    """

    bound = await _bound(user_id)
    if bound is None:
        return None
    try:
        name = (await _get(f"/players/{bound.pid}"))["display_name"]
    except Bf1Error:
        name = bound.name
    if not name:
        return None
    for account in accounts:
        try:
            me = await _onebot(
                account,
                "get_group_member_info",
                group_id=int(group),
                user_id=int(account.self_id),
                no_cache=True,
            )
            if (me or {}).get("role") not in ("owner", "admin"):
                continue
            await _onebot(
                account, "set_group_card", group_id=int(group), user_id=int(user_id), card=name
            )
        except Exception as error:  # 掉线、超时、不是 QQ 群：换下一个号
            logger.warning("用 {} 改 {} 的群名片失败：{!r}", account.self_id, user_id, error)
            continue
        return name
    logger.info("群 {} 里没有能改名片的管理员号，{} 的名片没改", group, user_id)
    return None


@plugin.listen(GuildMemberAddedEvent)
async def on_join_request(
    session: Session[GuildMemberAddedEvent], *, render_service: RenderService
):
    # shortcut: satori 的 onebot11 适配器把加群申请（request.group.add）也报成 GUILD_MEMBER_ADDED，
    # 只能靠带不带 message（flag 和附言）区分真入群；上游改成 GUILD_MEMBER_REQUEST 后改听那个事件
    request = getattr(session.event._origin, "message", None)
    group, applicant = session.guild.id, session.user.id
    if request is None:
        # 真进群了：在 QQ 里直接同意的申请不会再有人回复，这里清掉
        await _drop_request(group, applicant)
        if feature_enabled("bf1", group) and await _review_on(group):
            receiver = session.account
            others = accounts_for_group(group, platform=receiver.platform)
            accounts = [receiver, *(a for a in others if str(a.self_id) != str(receiver.self_id))]
            if name := await rename_member(group, applicant, accounts):
                await session.send(
                    MessageChain(At(applicant), Text(f" 已自动将你的群名片改为 {name}"))
                )
        return
    if not feature_enabled("bf1", group) or not await _review_on(group):
        return
    # QQ 把申请推给群里每个管理员号，宿主也都放行，第一个收到的处理
    if not await _claim(group, applicant, str(session.account.self_id), request.id):
        return
    message, persona = await review_request(
        applicant, session.user.name or applicant, request.content, render_service
    )
    if persona:
        await _remember_persona(group, applicant, persona)
    await session.send(message)


def _quoted_applicant(elements: list[Element], self_ids: set[str]) -> str | None:
    """回复的是 bot 发的审核消息时，取出申请者 QQ。

    每个 NapCat 各编各的消息号，事件可能由另一个号收到，所以不比消息号，看引用里的原文。
    """

    quote = next((e for e in elements if isinstance(e, Quote)), None)
    if quote is None:
        return None
    author = next((c for c in quote.children if isinstance(c, Author)), None)
    if author is None or author.id not in self_ids:
        return None
    said = "".join(c.text for c in quote.children if isinstance(c, Text))
    found = re.match(r"收到 .*\((\d+)\) 的入群申请", said)
    return found[1] if found else None


async def decide(group: str, applicant: str, said: str) -> str:
    """管理员的回复：y 同意，其他文字作为理由拒绝。"""

    async with _request_lock, get_session() as db:
        pending = await db.get(JoinRequest, (group, applicant))
        if pending is None:
            return f"{applicant} 的入群申请已经处理过了"
        account = next((a for a in online_accounts() if str(a.self_id) == pending.self_id), None)
        if account is None:
            return f"收到申请的号 {pending.self_id} 不在线，上线后再回复"
        flag, pid, name = pending.flag, pending.pid, pending.name
        # 先删再调接口：失败多半是申请在 QQ 里已经处理过了，留着也没用
        await db.delete(pending)
        await db.commit()
    approve = said.casefold() == "y"
    try:
        await account.protocol.guild_member_approve(flag, approve, "" if approve else said)
    except Exception as error:  # NapCat 的报错（不是管理员、申请已被别人处理）原样告诉管理员
        logger.warning("处理入群申请失败：{!r}", error)
        return f"处理 {applicant} 的入群申请失败：{error}"
    if not approve:
        return f"已拒绝 {applicant} 的入群申请，理由：{said}"
    if pid is not None and not await _bound(applicant):
        await _bind(applicant, {"persona_id": pid, "display_name": name})
    return f"已同意 {applicant} 的入群申请"


@plugin.listen(MessageCreatedEvent)
@feature_required()
async def on_review_reply(session: Session[MessageCreatedEvent]):
    if session.event.guild is None:
        return
    elements = list(session.event.message.message)
    self_ids = {str(a.self_id) for a in online_accounts()} | {str(session.account.self_id)}
    applicant = _quoted_applicant(elements, self_ids)
    if applicant is None:
        return
    async with get_session() as db:
        if await db.get(JoinRequest, (session.guild.id, applicant)) is None:
            return
    said = "".join(e.text for e in elements if isinstance(e, Text)).strip()
    if not said or not await require_perm(session, Permission.GroupAdmin):
        return
    await session.send(await decide(session.guild.id, applicant, said))


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
    group = session.guild.id
    if not switch.available:
        return f"本群入群审核：{'开' if await _review_on(group) else '关'}"
    if not await require_perm(session, Permission.GroupAdmin):
        return "只有群管理员及以上能开关入群审核"
    async with get_session() as db:
        row = await db.get(JoinReview, group)
        if switch.result == "开" and row is None:
            db.add(JoinReview(group_id=group))
        elif switch.result == "关" and row is not None:
            await db.delete(row)
        await db.commit()
    return f"本群入群审核已{switch.result}"
