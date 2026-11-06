"""全服信息：在线概况、当前行动和交换。"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from arclet.entari import MessageChain

from . import api, pages
from .api import Bf1Error
from .reply import render

# —— 全服概况 ——


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
    overview = await api.get("/overview")
    if not overview.get("available"):
        raise Bf1Error("全服概况暂时没有数据，稍后再试")
    context = pages.overview_page(overview, api.source())
    link = api.site("/stats")
    return await render(service, "overview.html", context, format_overview(overview), link)


# —— 行动 ——


def format_campaign(context: dict[str, Any]) -> str:
    lines = [f"当前行动：{context['name']}"]
    if context["description"]:
        lines.append(context["description"])
    if context["operations"]:
        lines.append("包含：" + "、".join(o["name"] for o in context["operations"]))
    if context["ended"]:
        lines.append(f"已于 {context['ends']} 结束")
    else:
        lines.append(f"剩余 {context['left']}（{context['ends']} 结束）")
    lines.append(f"距每日重置 {context['reset_left']}（每天 {context['reset']}，北京时间）")
    return "\n".join(lines)


async def campaign_reply(service) -> MessageChain | str:
    campaign = await api.get("/campaign")
    if not campaign:
        raise Bf1Error("当前没有进行中的行动")
    context = pages.campaign_page(campaign, api.source())
    link = api.site("/bf1/campaign")
    return await render(service, "campaign.html", context, format_campaign(context), link)


# —— 交换 ——

# 旧 bot 的 -t 日期写 2023.01.01；也认 2023-01-01、2023/1/1、2023年1月1日 和 20230101
_DAY = re.compile(r"(\d{4})\s*[-./年]\s*(\d{1,2})\s*[-./月]\s*(\d{1,2})\s*日?")
_EXCHANGE_LIST = ("列表", "历史", "全部", "期数", "list")
_EXCHANGE_HINT = "发 /交换 列表 看全部期数，/交换 2023.01.01 看那天的交换"


def parse_day(text: str) -> date | None:
    text = text.strip()
    if re.fullmatch(r"\d{8}", text):
        parts = text[:4], text[4:6], text[6:]
    elif found := _DAY.fullmatch(text):
        parts = found.groups()
    else:
        return None
    try:
        return date(*map(int, parts))
    except ValueError:
        return None


def format_exchange(context: dict[str, Any]) -> str:
    span = (
        f"{context['start']} ~ {context['until']}" if context["until"] else f"{context['start']} 起"
    )
    number = f"第 {context['number']}/{context['total']} 期，" if context["number"] else ""
    which = "当期" if context["latest"] else "历史"
    lines = [f"战地一交换（{which}，{number}{span}，北京时间）"]
    if context["stale"]:
        lines.append(
            f"这一期已经 {context['stale']} 天没换过，之后 EA 就没再更新交换，游戏里一直是这一期"
        )
    for group in context["groups"]:
        lines.append(f"{group['label']}（{len(group['tiles'])} 件）：")
        lines += [f"· {t['name']} · {t['parent']} · {t['price']} 废料" for t in group["tiles"]]
    return "\n".join(lines)


def format_exchange_periods(dates: list[date]) -> str:
    """按年列出全部期数，旧的在前，最新一期在最后。"""

    years: dict[int, list[date]] = {}
    for day in sorted(dates):
        years.setdefault(day.year, []).append(day)
    lines = [f"交换共 {len(dates)} 期（{min(dates)} ~ {max(dates)}，北京时间，按每期开始日期）："]
    lines += [
        f"{year}（{len(days)} 期）：" + "、".join(d.strftime("%m-%d") for d in days)
        for year, days in years.items()
    ]
    lines.append("发 /交换 日期 看那天的交换，比如 /交换 2023.01.01")
    return "\n".join(lines)


async def exchange_reply(
    service, day: date | None = None, listing: bool = False
) -> MessageChain | str:
    """不给日期查最新一期，给了查那天生效的一期；listing 时按年列出全部期数。"""

    dates = pages.exchange_dates(await api.get("/exchange/periods"))
    if not dates:
        raise Bf1Error("还没有交换记录")
    if listing:
        return format_exchange_periods(dates)
    first, last = dates[-1], dates[0]
    if day and day < first:
        raise Bf1Error(
            f"{day} 还没有交换记录：bf-manager 收录的是 {first} 到 {last} 的 {len(dates)} 期。"
            f"{_EXCHANGE_HINT}"
        )
    today = datetime.now(pages.BEIJING).date()
    if day and day > today:
        raise Bf1Error(f"{day} 还没到，查当期直接发 /交换")
    snapshot = await api.get("/exchange", **({"date": day.isoformat()} if day else {}))
    context = pages.exchange_page(snapshot, dates, api.source(), today)
    link = api.site("/bf1/exchange")
    return await render(service, "exchange.html", context, format_exchange(context), link)


async def exchange_query(word: str, service) -> MessageChain | str:
    """/交换 后面跟的参数：不给看当期，「列表」列出全部期数，日期看那天的一期。"""

    if word.casefold() in _EXCHANGE_LIST:
        return await exchange_reply(service, listing=True)
    parsed = parse_day(word) if word else None
    if word and parsed is None:
        raise Bf1Error(f"看不懂日期「{word}」，写成 2023.01.01 这样。{_EXCHANGE_HINT}")
    return await exchange_reply(service, parsed)
