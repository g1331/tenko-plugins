"""各条命令的查询：取数据、记下分数变化、出图，渲染不了回文字。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from arclet.entari import MessageChain
from satori import Image, Text
from sqlalchemy import BigInteger, Float, String
from sqlalchemy.orm import Mapped, mapped_column
from tenko_api.storage import Model, get_session

from . import api, pages
from .api import ApexError
from .pages import cn

NEWS = 5
# 平台的常见叫法 → API 的平台参数
PLATFORMS = {
    "pc": "PC",
    "origin": "PC",
    "steam": "PC",
    "ps": "PS4",
    "ps4": "PS4",
    "ps5": "PS4",
    "xbox": "X1",
    "x1": "X1",
    "switch": "SWITCH",
    "ns": "SWITCH",
}
SOURCE = "数据来自 apexlegendsstatus.com"  # API 条款要求注明来源


class Snapshot(Model):
    """上次看到的分数（玩家排位分、各平台猎杀线），用来显示“比几小时前涨了多少”。"""

    key: Mapped[str] = mapped_column(String(96), primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger)
    at: Mapped[float] = mapped_column(Float)


async def _remember(key: str, value: Any, now: float) -> tuple[int, float] | None:
    """返回之前记下的 (分数, 时间)，并在分数变了时更新记录。

    分数没变就不覆盖，连查两次也还能和真正的上一个分数比。
    """

    if not isinstance(value, int):
        return None
    async with get_session() as db:
        row = await db.get(Snapshot, key)
        previous = (row.value, row.at) if row else None
        if row is None:
            db.add(Snapshot(key=key, value=value, at=now))
        elif row.value != value:
            row.value, row.at = value, now
        await db.commit()
    return previous


async def _reply(service, template: str, context: dict[str, Any], fallback: str):
    image = await pages.render(service, template, context)
    return MessageChain(Image.of(raw=image, mime="image/jpeg")) if image else fallback


def format_player(data: dict[str, Any], context: dict[str, Any] | None = None) -> str:
    g = data["global"]
    rank = g.get("rank") or {}
    level = f"{g.get('level', '-')}"
    if g.get("levelPrestige"):
        level += f"（{g['levelPrestige']} 转）"
    realtime = data.get("realtime") or {}
    lines = [
        f"{g.get('name')} 的 Apex 战绩（{pages.PLATFORM_NAMES.get(g.get('platform'), '-')}）",
        f"等级 {level} | 段位 {pages.tier(rank)} {rank.get('rankScore', 0)} 分"
        + (f"（{position}）" if (position := pages.rank_position(rank)) else ""),
        f"状态 {pages.state(realtime.get('currentStateAsText'))} | "
        f"当前英雄 {cn(realtime.get('selectedLegend'))}",
    ]
    if context and (progress := context["rank_progress"]):
        lines.append(f"距 {progress['next']} 还差 {progress['need']} 分")
    if context and (change := context["rank_delta"]):
        lines.append(f"比 {change['ago']}：{change['text']}")
    totals = [f"{name} {value}" for name, value in pages.totals(data)]
    if totals:
        lines.append("生涯汇总：" + " | ".join(totals))
    legends = [f"{lg['name']} {lg['kills']}" for lg in pages.legend_ranking(data)]
    if legends:
        lines.append("英雄击杀：" + " | ".join(legends))
    if ban := pages.ban(g):
        lines.append(f"⚠ 排位禁赛：{ban}")
    lines.append(SOURCE)
    return "\n".join(lines)


async def player_reply(name: str, platform: str | None, service=None):
    code = PLATFORMS.get((platform or "pc").casefold())
    if code is None:
        raise ApexError("平台只能是 PC、PS、Xbox 或 Switch")
    data = await api.get("/bridge", player=name, platform=code)
    g, now = data["global"], time.time()
    score = (g.get("rank") or {}).get("rankScore")
    previous = await _remember(f"rp:{code}:{g.get('uid') or g.get('name')}", score, now)
    context = pages.player_page(data, previous, now)
    return await _reply(service, "player.html", context, format_player(data, context))


def _remaining(slot: dict[str, Any]) -> str:
    return f"剩 {slot['remainingTimer']}" if slot.get("remainingTimer") else ""


def format_rotation(data: dict[str, Any]) -> str:
    lines = ["Apex 地图轮换"]
    for key, label in pages.MODES:
        mode = data.get(key)
        if not mode or not mode.get("current"):
            continue
        current, upcoming = mode["current"], mode.get("next") or {}
        event = (
            f"{cn(current['eventName'])} · " if key == "ltm" and current.get("eventName") else ""
        )
        line = f"{label}：{event}{cn(current.get('map'))} {_remaining(current)}".rstrip()
        if upcoming.get("map"):
            line += f"，下一张 {cn(upcoming['map'])}"
            if at := pages._clock(upcoming.get("start")):
                line += f"（{at} 开始）"
        lines.append(line)
    lines.append(SOURCE)
    return "\n".join(lines)


async def rotation_reply(service=None):
    data = await api.get("/maprotation", version=2)
    return await _reply(service, "map.html", pages.rotation_page(data), format_rotation(data))


def format_predator(data: dict[str, Any]) -> str:
    lines = ["Apex 猎杀线（进入前 750 名所需分数）"]
    for code, label in pages.PLATFORMS:
        entry = (data.get("RP") or {}).get(code)
        if entry:
            lines.append(
                f"{label}：{entry.get('val', '-')} 分 | "
                f"大师及猎杀 {entry.get('totalMastersAndPreds', '-')} 人"
            )
    lines.append(SOURCE)
    return "\n".join(lines)


async def predator_reply(service=None):
    data = await api.get("/predator")
    now, previous = time.time(), {}
    for code, entry in (data.get("RP") or {}).items():
        if isinstance(entry, dict):
            previous[code] = await _remember(f"predator:{code}", entry.get("val"), now)
    context = pages.predator_page(data, {k: v for k, v in previous.items() if v}, now)
    return await _reply(service, "predator.html", context, format_predator(data))


def format_servers(context: dict[str, Any]) -> str:
    lines = [f"Apex 服务器状态：{context['summary']}"]
    for section in context["sections"]:
        bad = [
            f"{region} {cell['status']}"
            for region, cell in zip(context["regions"], section["cells"], strict=True)
            if cell["css"] != "up"
        ]
        if bad:
            lines.append(f"{section['label']}：" + "、".join(bad))
    lines += [f"{p['label']}：{p['status']}" for p in context["platforms"] if p["css"] != "up"]
    lines.append(SOURCE)
    return "\n".join(lines)


async def servers_reply(service=None):
    context = pages.servers_page(await api.get("/servers"))
    return await _reply(service, "servers.html", context, format_servers(context))


def format_news(items: list[dict[str, str]]) -> str:
    if not items:
        return "暂时没有资讯"
    lines = ["Apex 最新资讯"]
    for index, item in enumerate(items, 1):
        day = f"[{item['date'][5:]}] " if item["date"] else ""
        lines.append(f"{index}. {day}{item['title']}\n   {item['url']}")
    return "\n".join(lines)


async def _description(item: dict[str, str]) -> str:
    """简介只在文章页里，拿不到就不显示。"""

    try:
        response = await api.request(item["url"])
    except ApexError:
        return ""
    return pages.description(response.text) if response.status_code == 200 else ""


async def news_reply(service=None):
    response = await api.request(pages.EA_NEWS)
    if response.status_code >= 400:
        raise ApexError(f"资讯获取失败（{response.status_code}）")
    items = pages.parse_news(response.text)[:NEWS]
    if not items:
        return format_news(items)
    summaries = await asyncio.gather(*(_description(item) for item in items))
    for item, summary in zip(items, summaries, strict=True):
        item["summary"] = summary
    reply = await _reply(service, "news.html", pages.news_page(items), format_news(items))
    if isinstance(reply, str):
        return reply
    # 图里点不了链接，附上最新一条的地址
    return reply + Text(items[0]["url"])


async def answer(query) -> MessageChain | str:
    try:
        return await query
    except ApexError as error:
        return str(error)
