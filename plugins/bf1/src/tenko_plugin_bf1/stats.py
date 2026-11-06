"""玩家查询：生涯战绩、武器载具排行、玩家信息、最近对局和收藏。

每种查询一个 ``*_reply``：取数据、交给 :mod:`.pages` 排版出图，渲染不了时回 ``format_*`` 的文字。
"""

from __future__ import annotations

import asyncio
from typing import Any

from arclet.entari import MessageChain

from . import api, codex, groups, pages
from .api import Bf1Error
from .binding import find_player
from .config import config
from .reply import render

RECENT = 6
INFO_SERVERS = 8


def _hours(seconds: float | None) -> str:
    return f"{(seconds or 0) / 3600:.1f} 小时"


def _kpm(kills: int | None, seconds: float | None) -> str:
    return f"{(kills or 0) / (seconds / 60):.2f}" if seconds else "-"


def _n(value: Any) -> Any:
    return "-" if value is None else value


# —— 生涯战绩 ——


def format_stats(
    name: str,
    stats: dict[str, Any],
    platoon: dict[str, Any] | None = None,
    dogtags: list[str] | None = None,
) -> str:
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
            *([f"装备狗牌：{' / '.join(dogtags)}"] if dogtags else []),
            *_platoon_lines(platoon),
            *_career_lines(stats),
        ]
    )


def _platoon_lines(platoon: dict[str, Any] | None) -> list[str]:
    if not platoon:
        return []
    head = f"战队：[{platoon.get('tag') or '-'}] {platoon.get('name') or ''}".rstrip()
    if platoon.get("size"):
        head += f"（{platoon['size']} 人{'，EA 认证' if platoon.get('verified') else ''}）"
    description = " ".join((platoon.get("description") or "").split())
    return [head, *([f"战队描述：{description}"] if description else [])]


def _career_lines(stats: dict[str, Any]) -> list[str]:
    """荣誉、细项、模式、精英兵、常玩地图；接口没给的部分不写。"""

    lines = []
    honors, misc = stats.get("honors"), stats.get("misc")
    if honors:
        honor = (
            f"MVP {_n(honors.get('mvp'))} | 王牌小队 {_n(honors.get('ace_squad'))} | "
            f"勋带 {_n(honors.get('ribbons'))} | 勋章 {_n(honors.get('medals'))}"
        )
        if misc and misc.get("completion_rate") is not None:
            honor += f" | 完成率 {misc['completion_rate']:.1%}"
        lines.append(honor)
    if details := pages.career_details(stats):
        lines.append(" | ".join(f"{label} {value}" for label, value in details))
    if elite := pages.elite_kits(stats.get("elite_kits") or []):
        lines.append(
            "精英兵：" + "；".join(f"{k['name']} {k['time']} 击杀 {k['kills']}" for k in elite)
        )
    if modes := pages.career_modes(stats.get("modes") or []):
        lines.append(
            "模式：" + "；".join(f"{m['name']} {m['record']}（{m['rate']}）" for m in modes)
        )
    if maps := pages.career_maps(stats.get("maps"), ""):
        lines.append("常玩地图：" + "；".join(f"{m['name']} {m['hours']}" for m in maps))
    return lines


async def stats_reply(
    name: str | None, service, user_id: str = "", card: str | None = None
) -> MessageChain | str:
    reply, _, _ = await stats_card(await find_player(name, user_id, card), service)
    return reply


async def stats_card(persona: dict[str, Any], service) -> tuple[MessageChain | str, Any, Any]:
    """战绩卡片，连同取到的生涯数据和封禁状态（入群审核要用）。"""

    pid = persona["persona_id"]
    stats, weapons, vehicles, ban, platoon, online, found = await asyncio.gather(
        api.get(f"/stats/{pid}"),
        api.get(f"/stats/{pid}/weapons"),
        api.get(f"/stats/{pid}/vehicles"),
        api.optional(f"/stats/{pid}/ban"),
        api.optional(f"/stats/{pid}/platoon"),
        api.optional(f"/stats/{pid}/online"),
        api.optional(f"/stats/{pid}/collection"),
    )
    # 装备的狗牌只有收藏接口给，取不到就不画
    dogtags = pages.equipped_dogtags((found or {}).get("equipped"), config.base_url)
    context = pages.stats_page(
        persona,
        stats,
        weapons["weapons"],
        vehicles["vehicles"],
        ban,
        platoon,
        online,
        api.source(),
        config.base_url,
        None if platoon else codex.random_tip(),
        dogtags,
    )
    reply = await render(
        service,
        "stats.html",
        context,
        format_stats(persona["display_name"], stats, platoon, [t["name"] for t in dogtags]),
        api.player_link(pid),
    )
    return reply, stats, ban


# —— 武器、载具排行 ——


def _weapon_line(w: dict[str, Any]) -> str:
    kills = w.get("kills") or 0
    accuracy = f"{w['accuracy']:.1f}%" if w.get("accuracy") is not None else "-"
    headshot = f"{(w.get('headshots') or 0) / kills:.1%}" if kills else "-"
    return (
        f"{pages.cn(w.get('name'))} · {pages.cn(w.get('category')) or '-'}\n"
        f"   击杀 {kills} | KPM {_kpm(kills, w.get('time_seconds'))} | "
        f"命中 {accuracy} | 爆头 {headshot} | {_hours(w.get('time_seconds'))}"
    )


def _vehicle_line(v: dict[str, Any]) -> str:
    kills = v.get("kills") or 0
    return (
        f"{pages.cn(v.get('name'))} · {pages.cn(v.get('category')) or '-'}\n"
        f"   击杀 {kills} | KPM {_kpm(kills, v.get('time_seconds'))} | "
        f"摧毁 {v.get('destroyed') or 0} | {_hours(v.get('time_seconds'))}"
    )


def format_items(kind: str, heading: str, items: list[dict[str, Any]]) -> str:
    line = _weapon_line if kind == "weapons" else _vehicle_line
    return "\n".join([heading, *(f"{i}. {line(item)}" for i, item in enumerate(items, 1))])


def _items_heading(
    name: str, label: str, sort: pages.Sort, keyword: str | None, shown: int, total: int
) -> tuple[str, list[str]]:
    """文字版的第一行，和卡片名字右边的排序、搜索说明；默认按击杀排时卡片上不写。"""

    least = pages.MIN_RATE_KILLS
    floor = ""
    if sort.rate:
        # 搜索时样本不够的也列，置灰接在后面
        floor = f"击杀不到 {least} 的置后" if keyword else f"击杀 ≥ {least}"
    note = [f"名字含「{keyword}」· {total} 件"] if keyword else []
    if keyword or sort.key != "kills":
        note.append(" · ".join(x for x in (f"按{sort.label}排序", floor) if x))
    if keyword:
        top = f"前 {shown}" if total > shown else ""
        later = f"，击杀不到 {least} 的排在后面" if sort.rate else ""
        found = f"{name} 的{label}里名字含「{keyword}」的有 {total} 件"
        return f"{found}，按{sort.label}排{top}{later}", note
    if sort.key == "kills":
        return f"{name} 的{label}击杀前 {shown}", note
    floor = f"（击杀不少于 {least} 的）" if sort.rate else ""
    return f"{name} 的{label}按{sort.label}排前 {shown}{floor}", note


async def items_reply(
    kind: str,
    name: str | None,
    service,
    user_id: str = "",
    group: groups.Group | None = None,
    layout: tuple[int, int] = (pages.ITEM_COLS, pages.ITEM_ROWS),
    keyword: str | None = None,
    sort_word: str | None = None,
    card: str | None = None,
) -> MessageChain | str:
    """武器或载具排行；给了分组（步枪、突击兵、坦克……）就只排这一组。

    layout 是卡片的列数和行数，文字版也按列 × 行的数量列；keyword 是旧 bot 的 -n 名字搜索，
    sort_word 是 -s 的排序方式。
    """

    whole = "武器" if kind == "weapons" else "载具"
    sort = pages.SORTS[kind][0]
    if sort_word and not (sort := pages.find_sort(kind, sort_word)):
        choices = "、".join(s.label for s in pages.SORTS[kind])
        raise Bf1Error(f"{whole}没有「{sort_word}」这种排法，可以按 {choices} 排")
    persona = await find_player(name, user_id, card)
    pid = persona["persona_id"]
    result, platoon = await asyncio.gather(
        api.get(f"/stats/{pid}/{kind}"), api.optional(f"/stats/{pid}/platoon")
    )
    items = result[kind]
    if group:
        items = [i for i in items if i in group]
    label = group.label if group else whole
    ranked = pages.rank_items(items, sort, keyword)
    display = persona["display_name"]
    if not ranked:
        if keyword:
            return f"{display} 的{label}里没有名字含「{keyword}」的"
        if sort.rate:
            return f"{display} 还没有击杀满 {pages.MIN_RATE_KILLS} 的{label}"
        return f"{display} 还没有{label}击杀记录"
    cols, rows = layout
    shown = ranked[: cols * rows]
    heading, note = _items_heading(display, label, sort, keyword, len(shown), len(ranked))
    title = f"{whole} · {group.label}" if group else whole
    context = pages.items_page(
        kind, persona, platoon, shown, api.source(), cols, rows, title, sort, note
    )
    text = format_items(kind, heading, shown)
    return await render(service, "items.html", context, text, api.player_link(pid, kind))


# —— 玩家信息（天眼查） ——


def format_info(context: dict[str, Any]) -> str:
    platoon = context["platoon"]
    online = context["online"]
    status = "状态未知" if online is None else "离线" if not online else "在线"
    if online and context["online_server"]:
        status = f"正在游戏：{context['online_server']}"
    states = {"clean": "无记录", "unknown": "无信息"}
    bans = " | ".join(
        f"{b['source']} {b['hit_text'] if b['state'] == 'hit' else states[b['state']]}"
        for b in context["bans"]
    )
    lines = [f"{context['name']}（PID {context['pid']}）"]
    if platoon:
        lines.append(f"战队：[{platoon.get('tag') or '-'}] {platoon.get('name') or ''}".rstrip())
    lines.append(f"{status} | {bans}")
    if context["rsp"]:
        counts = " · ".join(f"{r['label']} {r['total']}" for r in context["roles"])
        lines.append(f"私服身份（bf-manager 收录的 {context['tracked']} 个私服）：{counts}")
        for r in context["roles"]:
            if r["rows"]:
                lines.append(f"{r['label']}：")
                lines += [f"{i}. {row['name']}" for i, row in enumerate(r["rows"], 1)]
                if r["more"]:
                    lines.append(f"……还有 {r['more']} 个，加 -{r['key'][0]} 全部列出")
            elif r["full"]:
                lines.append(f"{r['label']}：没有")
    else:
        lines.append("私服身份暂时查不到")
    if context["recent"]:
        lines.append("最近游玩（bf-manager 跟踪到的对局）：")
        lines += [f"· {s['name']}（{s['when']}，{s['rounds']} 局）" for s in context["recent"]]
    return "\n".join(lines)


async def _nothing() -> None:
    return None


async def info_reply(
    name: str | None,
    service,
    user_id: str = "",
    full: tuple[str, ...] = (),
    card: str | None = None,
) -> MessageChain | str:
    """full 是要全部列出的身份（owner/admin/vip/ban），这时不查最近游玩。"""

    persona = await find_player(name, user_id, card)
    pid = persona["persona_id"]
    # 最近玩过的服用 bf-manager 跟踪到的对局：EA 的“最近游玩”不准
    rsp, rounds, ban, online, platoon = await asyncio.gather(
        api.optional(f"/stats/{pid}/servers"),
        _nothing() if full else api.optional(f"/stats/{pid}/rounds", limit=100),
        *(api.optional(f"/stats/{pid}/{p}") for p in ("ban", "online", "platoon")),
    )
    if full and rsp is None:
        raise Bf1Error("私服身份暂时查不到，稍后再试")
    items = rounds["items"] if rounds else None
    context = pages.info_page(
        persona, rsp, items, ban, online, platoon, api.source(), INFO_SERVERS, full
    )
    # 查 info 多半是要名字和 PID 去封人、加白，图里的字复制不了
    caption = f"玩家名：{context['name']}\nPID：{pid}"
    link = api.player_link(pid)
    return await render(service, "info.html", context, format_info(context), link, caption)


# —— 最近对局 ——


def format_recent(name: str, context: dict[str, Any]) -> str:
    rounds = context["rounds"]
    lines = [
        f"{name} 最近 {len(rounds)} 局",
        " | ".join(f"{label} {value}" for label, value in context["kpis"]),
    ]
    for index, r in enumerate(rounds, 1):
        gear = "、".join(f"{g['name']} {g['kills']} 杀" for g in r["gear"])
        badges = "".join(f" {b}" for b, on in (("MVP", r["mvp"]), ("王牌", r["ace"])) if on)
        score = next(c["value"] for c in r["stats"] if c["label"] == "得分")
        honors = [text for text, _ in r["growth"]]
        honors += [f"{x['name']}×{x['count']}" for x in r["ribbons"]]
        compare = [
            f"{c['label']} {c['value']}（生涯 {c['compare']['career']}"
            + (f" {c['compare']['shift']}" if c["compare"]["shift"] else "")
            + "）"
            for c in r["stats"]
            if c["compare"]
        ]
        lines += [
            f"{index}. {r['when']} {r['map']}·{r['mode'] or '-'} {r['outcome']}{badges}",
            f"   {r['kd']} | KD {r['ratio']} | KPM {r['kpm']} | 得分 {score} | "
            f"{r['minutes']} | {r['kit']}",
            *([f"   对比生涯：{'；'.join(compare)}"] if compare else []),
            *(
                [
                    "   得分构成："
                    + " | ".join(f"{x['label']} {x['value']}" for x in r["parts"]["segments"])
                    + f" | 总经验 {r['parts']['total']}"
                ]
                if r["parts"]
                else []
            ),
            *([f"   {'、'.join(honors)}"] if honors else []),
            *([f"   {gear}"] if gear else []),
            f"   {r['server']}",
        ]
    return "\n".join(lines)


async def recent_card(
    persona: dict[str, Any], service, link: bool = True, count: int = RECENT
) -> MessageChain | str | None:
    """最近 count 局的卡片；bf-manager 没记录到能看的对局时返回 None。"""

    pid = persona["persona_id"]
    # 多取一些：排队、观战、进服就走的局会被筛掉；接口一次最多 100 局
    limit = min(100, max(20, count * 4))
    result, platoon = await asyncio.gather(
        api.get(f"/stats/{pid}/rounds", limit=limit), api.optional(f"/stats/{pid}/platoon")
    )
    # 和生涯比要单局详情：每局一个请求，几局一起取，取不到的局就不比
    played = pages.played_rounds(result["items"], count)
    found = await asyncio.gather(*(api.optional(f"/rounds/{i['round']['id']}") for i in played))
    details = {d["id"]: d for d in found if d}
    context = pages.recent_page(
        persona, platoon, result["items"], api.source(), count, details, config.base_url
    )
    if not context["rounds"]:
        return None
    text = format_recent(persona["display_name"], context)
    return await render(
        service, "recent.html", context, text, api.player_link(pid, "rounds") if link else None
    )


async def recent_reply(
    name: str | None,
    service,
    user_id: str = "",
    count: int = RECENT,
    card: str | None = None,
) -> MessageChain | str:
    persona = await find_player(name, user_id, card)
    reply = await recent_card(persona, service, count=count)
    if reply is None:
        raise Bf1Error(
            f"还没有记录到 {persona['display_name']} 的对局（bf-manager 跟踪到的才有，保留 90 天）"
        )
    return reply


# —— 收藏 ——


def format_collection(context: dict[str, Any]) -> str:
    lines = [f"{context['name']} 的收藏"]
    if context["equipped"]:
        lines.append("佩戴的狗牌：" + "、".join(t["name"] for t in context["equipped"]))
    got = [m["name"] for _, items in context["medals"] for m in items if m["got"]]
    going = [
        f"{m['name']} {m['progress']}"
        for _, items in context["medals"]
        for m in items
        if m["progress"]
    ]
    lines.append(f"勋章 {context['medals_got']}/{context['medals_total']}")
    if got:
        lines.append("已获得：" + "、".join(got))
    if going:
        lines.append("进行中：" + "、".join(going))
    lines.append(
        f"狗牌 {context['dogtags_got']}/{context['dogtags_total']}："
        + "；".join(
            f"{category} {sum(t['got'] for t in items)}/{len(items)}"
            for category, items in context["dogtags"]
        )
    )
    if context["ribbons"]:
        top = sorted(
            (r for _, items in context["ribbons"] for r in items if r["count"]),
            key=lambda r: -r["count"],
        )[:10]
        lines.append(
            f"勋带 {context['ribbons_got']}/{context['ribbons_total']} 种，"
            f"共 {context['ribbons_sum']:,} 次；最多的："
            + "、".join(f"{r['name']} ×{r['count']:,}" for r in top)
        )
    return "\n".join(lines)


async def collection_reply(
    name: str | None, service, user_id: str = "", card: str | None = None
) -> MessageChain | str:
    """勋章、狗牌和佩戴的狗牌；勋带次数在生涯战绩里，拿不到就不画勋带墙。"""

    persona = await find_player(name, user_id, card)
    pid = persona["persona_id"]
    found, stats, platoon = await asyncio.gather(
        api.get(f"/stats/{pid}/collection"),
        api.optional(f"/stats/{pid}"),
        api.optional(f"/stats/{pid}/platoon"),
    )
    context = pages.collection_page(persona, platoon, stats, found, api.source(), config.base_url)
    caption = f"玩家名：{persona['display_name']}\nPID：{pid}"
    return await render(
        service,
        "collection.html",
        context,
        format_collection(context),
        api.player_link(pid),
        caption,
    )
