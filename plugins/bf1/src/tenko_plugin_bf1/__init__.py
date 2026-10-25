"""战地一查询：战绩、武器、载具和服务器。

数据全部来自 bf-manager 的公开查询接口；这里解析命令、取数据，图片排版在 :mod:`.pages`，
渲染不了时回文字。
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlparse

import httpx
from arclet.alconna import Alconna, AllParam, Args, CommandMeta
from arclet.entari import BasicConfModel, MessageChain, Session, command, plugin
from arclet.entari.command import Query
from loguru import logger
from satori import Image, Text
from sqlalchemy import BigInteger, String, select
from sqlalchemy.orm import Mapped, mapped_column
from tenko_api.features import declare
from tenko_api.render import RenderService
from tenko_api.storage import Model, Store, get_session

from . import pages


class Bf1Config(BasicConfModel):
    """对应 config/tenko.toml 中的 ``[plugin_config.bf1]``。"""

    base_url: str = "https://bfm.umrbotech.com"
    """bf-manager 地址，不带 /api/v1。"""


plugin.metadata(
    "战地一查询",
    author=["13"],
    version="0.1.6",
    description="经 bf-manager 查询战地一玩家战绩、武器、载具和服务器。",
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
        raise Bf1Error("还没绑定 EA 名字：先发 /bf1绑定 你的EA名字，或者在命令后面带上名字")
    return await _get(f"/players/{bound.pid}")


async def stats_reply(name: str | None, service, user_id: str = "") -> MessageChain | str:
    persona = await _player(name, user_id)
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
    return await _reply(
        service,
        "stats.html",
        context,
        format_stats(persona["display_name"], stats),
        _player_link(pid),
    )


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


async def bind_reply(name: str | None, user_id: str) -> str:
    if not name:
        bound = await _bound(user_id)
        if not bound:
            return "用法：/bf1绑定 <EA 名字>"
        return f"当前绑定：{bound.name or '名字未知'}（PID {bound.pid}）"
    persona = await _persona(name)
    await _import_legacy()
    async with get_session() as db:
        await db.merge(
            Binding(user_id=user_id, pid=persona["persona_id"], name=persona["display_name"])
        )
        await db.commit()
    return (
        f"已绑定 {persona['display_name']}（PID {persona['persona_id']}），"
        "之后 /bf1s /bf1w /bf1v 不带名字就查这个号"
    )


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
            note = f"「{keyword}」匹配 {total} 个服务器，这是人最多的；要看别的用 /bf1pl gameId"
    detail, players = await asyncio.gather(
        _get(f"/servers/{game_id}"), _optional(f"/servers/{game_id}/players")
    )
    context = pages.server_page(detail, players, _source())
    # 匹配说明跟在图后面当文字，不画进图里
    fallback = format_server(detail, players)
    return await _reply(service, "server.html", context, fallback, note or None)


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


def _player_command(name: str, what: str) -> Alconna:
    return Alconna(
        name,
        Args["name?", str],
        meta=CommandMeta(
            f"查询战地一玩家的{what}，绑定过可以不带名字",
            usage=f"{name} [EA 名字]",
            example=f"/{name} SHlSAN13",
        ),
    )


def _name(name: Query[str]) -> str | None:
    return name.result if name.available else None


@command.on(_player_command("bf1s", "生涯战绩"))
async def bf1s(
    session: Session,
    name: Query[str] = Query("name"),  # noqa: B008
    *,
    render_service: RenderService,
):
    return await _answer(stats_reply(_name(name), render_service, session.user.id))


@command.on(_player_command("bf1w", "武器击杀排行"))
async def bf1w(
    session: Session,
    name: Query[str] = Query("name"),  # noqa: B008
    *,
    render_service: RenderService,
):
    return await _answer(items_reply("weapons", _name(name), render_service, session.user.id))


@command.on(_player_command("bf1v", "载具击杀排行"))
async def bf1v(
    session: Session,
    name: Query[str] = Query("name"),  # noqa: B008
    *,
    render_service: RenderService,
):
    return await _answer(items_reply("vehicles", _name(name), render_service, session.user.id))


def _bind_command(name: str) -> Alconna:
    return Alconna(
        name,
        Args["name?", str],
        meta=CommandMeta(
            "把自己的 EA 名字记下来，之后查战绩可以不带名字；不带名字查看当前绑定",
            usage=f"{name} [EA 名字]",
            example=f"/{name} SHlSAN13",
        ),
    )


@command.on(_bind_command("bf1绑定"))
async def bf1bind(session: Session, name: Query[str] = Query("name")):  # noqa: B008
    return await _answer(bind_reply(_name(name), session.user.id))


# bf1b 是 bf1绑定 的简写。不用 Alconna shortcut：entari 先按命令名前缀派发，
# "bf1b …" 只会命中 /bf1，到不了 bf1绑定 的 shortcut，所以单独注册一条
@command.on(_bind_command("bf1b"))
async def bf1b(session: Session, name: Query[str] = Query("name")):  # noqa: B008
    return await _answer(bind_reply(_name(name), session.user.id))


def _keyword(keyword: Query[list]) -> str:
    return " ".join(str(part) for part in keyword.result).strip()


server_command = Alconna(
    "bf1ss",
    Args["keyword", AllParam],
    meta=CommandMeta(
        "按名字搜索有人的战地一私服", usage="bf1ss <服务器名关键字>", example="/bf1ss sakula"
    ),
)


@command.on(server_command)
async def bf1ss(
    session: Session,
    keyword: Query[list] = Query("keyword"),  # noqa: B008
    *,
    render_service: RenderService,
):
    if not (text := _keyword(keyword)):
        return "用法：/bf1ss <服务器名关键字>"
    return await _answer(servers_reply(text, render_service))


detail_command = Alconna(
    "bf1pl",
    Args["keyword", AllParam],
    meta=CommandMeta(
        "查看战地一服务器的玩家列表；关键字匹配多个时显示人最多的",
        usage="bf1pl <服务器名关键字 | gameId>",
        example="/bf1pl pigu",
    ),
)


@command.on(detail_command)
async def bf1pl(
    session: Session,
    keyword: Query[list] = Query("keyword"),  # noqa: B008
    *,
    render_service: RenderService,
):
    if not (text := _keyword(keyword)):
        return "用法：/bf1pl <服务器名关键字 | gameId>"
    return await _answer(server_reply(text, render_service))


@command.on(Alconna("bf1", meta=CommandMeta("战地一全服在线人数、地区分布和热门地图", usage="bf1")))
async def bf1(session: Session, *, render_service: RenderService):
    return await _answer(overview_reply(render_service))
