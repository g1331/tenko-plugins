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
from satori import Image
from tenko_api.features import declare
from tenko_api.render import RenderService

from . import pages


class Bf1Config(BasicConfModel):
    """对应 config/tenko.toml 中的 ``[plugin_config.bf1]``。"""

    base_url: str = "https://bfm.umrbotech.com"
    """bf-manager 地址，不带 /api/v1。"""


plugin.metadata(
    "战地一查询",
    author=["13"],
    version="0.1.0",
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
ITEM_CARDS = 12


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
    if not personas:
        raise Bf1Error(f"没找到玩家 {name}")
    # 重名时 bf-manager 会回填生涯时长，0 是没玩过 BF1 的空号。
    return next((p for p in personas if p.get("time_played_hours")), personas[0])


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


async def _reply(service, template: str, context, fallback: str) -> MessageChain | str:
    image = await pages.render(service, template, context)
    return MessageChain(Image.of(raw=image, mime="image/jpeg")) if image else fallback


async def stats_reply(name: str, service) -> MessageChain | str:
    persona = await _persona(name)
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
        service, "stats.html", context, format_stats(persona["display_name"], stats)
    )


async def items_reply(kind: str, name: str, service) -> MessageChain | str:
    persona = await _persona(name)
    pid = persona["persona_id"]
    result, platoon = await asyncio.gather(
        _get(f"/stats/{pid}/{kind}"), _optional(f"/stats/{pid}/platoon")
    )
    items = result[kind]
    context = pages.items_page(kind, persona, platoon, items, _source(), ITEM_CARDS)
    text = (format_weapons if kind == "weapons" else format_vehicles)(
        persona["display_name"], items
    )
    return await _reply(service, "items.html", context, text)


async def servers_reply(keyword: str, service) -> MessageChain | str:
    result = await _get("/servers", name=keyword, limit=100)
    # 接口不按人数排；多取一些自己排，total 是全部匹配数。
    servers = sorted(result["items"], key=lambda s: s.get("player_count") or 0, reverse=True)
    servers = servers[:TOP]
    context = pages.servers_page(keyword, servers, result["total"], _source())
    return await _reply(
        service, "servers.html", context, format_servers(keyword, servers, result["total"])
    )


async def _answer(query) -> MessageChain | str:
    try:
        return await query
    except Bf1Error as error:
        return str(error)


def _player_command(name: str, what: str) -> Alconna:
    return Alconna(
        name,
        Args["name", str],
        meta=CommandMeta(
            f"查询战地一玩家的{what}", usage=f"{name} <EA 名字>", example=f"/{name} SHlSAN13"
        ),
    )


@command.on(_player_command("bf1s", "生涯战绩"))
async def bf1s(
    session: Session,
    name: Query[str] = Query("name"),  # noqa: B008
    *,
    render_service: RenderService,
):
    return await _answer(stats_reply(name.result, render_service))


@command.on(_player_command("bf1w", "武器击杀排行"))
async def bf1w(
    session: Session,
    name: Query[str] = Query("name"),  # noqa: B008
    *,
    render_service: RenderService,
):
    return await _answer(items_reply("weapons", name.result, render_service))


@command.on(_player_command("bf1v", "载具击杀排行"))
async def bf1v(
    session: Session,
    name: Query[str] = Query("name"),  # noqa: B008
    *,
    render_service: RenderService,
):
    return await _answer(items_reply("vehicles", name.result, render_service))


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
    text = " ".join(str(part) for part in keyword.result).strip()
    if not text:
        return "用法：/bf1ss <服务器名关键字>"
    return await _answer(servers_reply(text, render_service))
