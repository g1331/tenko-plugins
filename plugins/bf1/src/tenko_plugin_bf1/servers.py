"""服务器查询：按名字搜服，看某个服的详情和玩家列表。"""

from __future__ import annotations

import asyncio
from typing import Any

from arclet.entari import MessageChain

from . import api, pages
from .api import Bf1Error
from .reply import render

TOP = 10


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


async def _search(keyword: str) -> tuple[list[dict[str, Any]], int]:
    result = await api.get("/servers", name=keyword, limit=100)
    # 接口不按人数排；多取一些自己排，total 是全部匹配数。
    servers = sorted(result["items"], key=lambda s: s.get("player_count") or 0, reverse=True)
    return servers, result["total"]


async def servers_reply(keyword: str, service) -> MessageChain | str:
    servers, total = await _search(keyword)
    servers = servers[:TOP]
    context = pages.servers_page(keyword, servers, total, api.source())
    return await render(service, "servers.html", context, format_servers(keyword, servers, total))


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
        names = "、".join(map(_roster_name, team.get("players") or []))
        lines.append(f"{team.get('faction') or '-'}（{len(team.get('players') or [])}）：{names}")
    for label, key in [("排队", "queued"), ("观战", "spectators")]:
        if waiting := (players or {}).get(key):
            names = "、".join(map(_roster_name, waiting))
            lines.append(f"{label}（{len(waiting)}）：{names}")
    if loading := ((players or {}).get("summary") or {}).get("loading_count"):
        lines.append(f"载入中 {loading} 人")
    return "\n".join(lines)


def _roster_name(p: dict[str, Any]) -> str:
    name = p.get("display_name") or "-"
    return f"{name}（载入中）" if p.get("loading") else name


async def server_reply(keyword: str, service) -> MessageChain | str:
    note = ""
    # gameId 是十几位数；短数字（比如服名里的 3090）按名字搜
    if keyword.isdigit() and len(keyword) >= 10:
        game_id = int(keyword)
    else:
        servers, total = await _search(keyword)
        if not servers:
            raise Bf1Error(f"没找到名字含「{keyword}」的服务器（空服不在列表里）")
        game_id = servers[0]["game_id"]
        if total > 1:
            note = f"其他 {total - 1} 个：/bf1ss {keyword}"
    detail, players = await asyncio.gather(
        api.get(f"/servers/{game_id}"), api.optional(f"/servers/{game_id}/players")
    )
    context = pages.server_page(detail, players, api.source())
    # 匹配说明和网页链接跟在图后面当文字，不画进图里
    link = api.site(f"/bf1/server/{game_id}#players")
    fallback = format_server(detail, players)
    return await render(
        service, "server.html", context, fallback, f"{note}\n{link}" if note else link
    )
