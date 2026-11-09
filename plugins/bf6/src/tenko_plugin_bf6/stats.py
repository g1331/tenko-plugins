"""玩家查询：生涯战绩、武器载具配备排行和对局估算。

每种查询一个 ``*_reply``：取数据、交给 :mod:`.pages` 排版出图，渲染不了时回 ``format_*`` 的文字。
"""

from __future__ import annotations

from typing import Any

from arclet.entari import MessageChain

from . import api, catalog, pages
from .api import Bf6Error
from .binding import find_player
from .reply import render

RECENT = 8
MAX_RECENT = 20


async def _stats(player: dict[str, Any]) -> dict[str, Any]:
    return await api.get(f"/players/{player['nucleus_id']}/stats")


def _no_data(player: dict[str, Any]) -> str:
    return f"{player['display_name']} 还没有战地六多人战绩"


# —— 生涯战绩 ——


def _line(cells: list[dict[str, str]]) -> str:
    return " | ".join(f"{c['label']} {c['value']}{_unit(c['unit'])}" for c in cells)


def _unit(unit: str) -> str:
    """中文单位前空一格（125.7 小时），m、km 紧跟数字。"""

    return f" {unit}" if unit and not unit.isascii() else unit


def format_stats(context: dict[str, Any]) -> str:
    lines = [
        f"{context['name']} 的战地六生涯战绩",
        f"KD {context['kd']}（真人击杀 {context['human_kills']} / 死亡 {context['deaths']}）",
        _line(context["cells"][:3]) + f"（{context['cells'][2]['note']}）",
        _line(context["cells"][3:]),
        _line(context["combat"][:6]),
        _line(context["combat"][6:11]),
        _line(context["combat"][11:]),
    ]
    if context["kit"]:
        lines.append(f"主玩兵种 {context['kit']}")
    if context["signature"]:
        top = "、".join(f"{g['name']} {g['kills']}" for g in context["signature"])
        lines.append(f"王牌装备：{top}")
    lines.append("生涯之最：" + _line(context["bests"]))
    if context["modes"]:
        modes = "、".join(f"{m['name']} {m['matches']} 场" for m in context["modes"])
        lines.append(f"常玩模式：{modes}")
    if context["redsec"]:
        lines.append("禁区冲突：" + _line(context["redsec"]))
    return "\n".join(lines)


async def stats_reply(
    name: str | None, service, user_id: str = "", card: str | None = None
) -> MessageChain | str:
    player = await find_player(name, user_id, card)
    stats = await _stats(player)
    if not stats["has_data"]:
        return _no_data(player)
    context = pages.stats_page(player, stats)
    return await render(
        service, "stats.html", context, format_stats(context), api.player_link(player)
    )


# —— 武器、载具、配备排行 ——


def format_items(context: dict[str, Any]) -> str:
    head = f"{context['name']} 的战地六{context['title']}"
    if context["note"]:
        head += f"（{'，'.join(context['note'])}）"
    if not context["items"]:
        return f"{head}\n没有符合的记录"
    columns = context["columns"]
    lines = [head]
    for index, item in enumerate(context["items"], 1):
        values = " | ".join(
            f"{label} {value}" for label, value in zip(columns, item["stats"], strict=True)
        )
        lines.append(f"{index}. {item['name']}（{item['category']}）{values}")
    return "\n".join(lines)


def _sort_of(kind: str, word: str | None) -> pages.Sort:
    if not word:
        return pages.SORTS[kind][0]
    if found := pages.find_sort(kind, word):
        return found
    choices = "、".join(s.label for s in pages.SORTS[kind])
    raise Bf6Error(f"不认识的排序：{word}，可以用 {choices}")


async def items_reply(
    kind: str,
    name: str | None,
    service,
    user_id: str,
    group: catalog.Group | None,
    layout: tuple[int, int],
    keyword: str | None = None,
    sort_word: str | None = None,
    card: str | None = None,
) -> MessageChain | str:
    """武器、载具排行；武器的“配备”类别换成配备的统计。"""

    player = await find_player(name, user_id, card)
    stats = await _stats(player)
    if not stats["has_data"]:
        return _no_data(player)
    source = "gadgets" if group and group.key == "gadgets" else kind
    sort = _sort_of(source, sort_word)
    items = stats[source]
    if group and source == kind:
        items = [i for i in items if catalog.in_group(kind, group, i["id"])]
    if keyword:
        items = [i for i in items if pages.name_matches(keyword, catalog.name(source, i))]
    note = []
    if keyword:
        note.append(f"搜“{keyword}”")
    if sort is not pages.SORTS[source][0]:
        note.append(f"按{sort.label}排")
        if sort.rate:
            note.append(f"击杀满 {pages.MIN_RATE_KILLS} 才排名")
    title = pages.item_title(source, group.label if group else None)
    context = pages.items_page(
        player, source, pages.rank_items(items, sort, keyword), title, sort, layout, note
    )
    return await render(
        service, "items.html", context, format_items(context), api.player_link(player)
    )


# —— 对局估算 ——


def format_recent(context: dict[str, Any]) -> str:
    head = f"{context['name']} 的战地六对局估算"
    if not context["days"]:
        return f"{head}\n{recent_empty(context)}"
    lines = [head]
    for day in context["days"]:
        lines.append(f"【{day['label']}】{day['matches']} 场 {day['wins']} 胜")
        for r in day["rounds"]:
            lines.append(
                f"{r['title']} {r['label']} · {r['subtitle']}\n"
                f"  击杀 {r['kills']} | 死亡 {r['deaths']} | KD {r['kd']} | "
                f"得分 {r['score']} | {r['minutes']} 分钟"
                + (f" | {r['gear']}" if r["gear"] else "")
            )
    return "\n".join(lines)


def recent_empty(context: dict[str, Any]) -> str:
    if context["since"]:
        return f"从 {context['since']} 开始记录，之后打完的对局会出现在这里"
    return "现在开始记录，之后打完的对局会出现在这里"


async def recent_reply(
    name: str | None, service, user_id: str, count: int = RECENT, card: str | None = None
) -> MessageChain | str:
    player = await find_player(name, user_id, card)
    # 先读一次生涯：bf-manager 靠有人查过才开始记录这名玩家，读到变化时顺手记下最新一段
    stats = await _stats(player)
    if not stats["has_data"]:
        return _no_data(player)
    data = await api.get(f"/players/{player['nucleus_id']}/matches")
    context = pages.recent_page(player, data, count)
    context["empty"] = recent_empty(context)
    return await render(
        service, "recent.html", context, format_recent(context), api.player_link(player)
    )
