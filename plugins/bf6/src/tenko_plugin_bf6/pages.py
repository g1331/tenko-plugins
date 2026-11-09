"""把 bf-manager 返回的数据整理成模板上下文，并截成图片。

这里不碰 Entari：输入是接口返回的 dict，输出是模板上下文或图片字节。
EA 没给的项是 None，一律显示“—”，和 0 区分开。
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from functools import cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from loguru import logger

from . import api, catalog

HERE = Path(__file__).parent
ASSETS = HERE / "assets"
WIDTH = 1000
RENDER_TIMEOUT = 40
DASH = "—"
# 卡片上的时间都按北京时间写
CST = ZoneInfo("Asia/Shanghai")

_env = Environment(
    loader=FileSystemLoader(HERE / "templates"),
    autoescape=True,
    undefined=StrictUndefined,
)


@cache
def _data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def asset(name: str) -> str:
    return _data_uri(ASSETS / name)


_env.globals["asset"] = asset


# —— 数字 ——


def num(value: float | None) -> str:
    return DASH if value is None else f"{round(value):,}"


def fixed(value: float | None, digits: int = 2) -> str:
    return DASH if value is None else f"{value:.{digits}f}"


def pct(ratio: float | None, digits: int = 1) -> str:
    return DASH if ratio is None else f"{ratio * 100:.{digits}f}%"


def hours(seconds: float | None) -> str:
    return DASH if not seconds else f"{seconds / 3600:,.1f}"


def duration(seconds: float | None) -> str:
    """不到 6 分钟的写分钟，免得写成 0.0 小时。"""

    if seconds and seconds < 360:
        return f"{max(1, round(seconds / 60))} 分钟"
    return f"{hours(seconds)} 小时"


def minutes(seconds: float | None) -> str:
    return DASH if not seconds else f"{max(1, round(seconds / 60))}"


def compact(value: float | None) -> tuple[str, str]:
    """大数缩写成数字和单位：≥10 万写“万”，≥1 亿写“亿”。"""

    if value is None:
        return DASH, ""
    if value >= 1e8:
        return f"{value / 1e8:.2f}", "亿"
    if value >= 1e5:
        return f"{value / 1e4:,.1f}", "万"
    return num(value), ""


def km(meters: float | None) -> str:
    return DASH if meters is None else f"{meters / 1000:,.0f}"


def share(part: float | None, whole: float | None) -> float:
    return part / whole if part and whole else 0.0


def _common(player: dict[str, Any], title: str) -> dict[str, Any]:
    return {
        "name": player["display_name"],
        "avatar_url": player.get("avatar_url"),
        "platform": api.platform_label(player.get("platform")),
        "title": title,
        "source": api.source(),
        "now": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
    }


# —— 生涯 ——


def _cell(label: str, value: str, unit: str = "", note: str = "") -> dict[str, str]:
    return {"label": label, "value": value, "unit": unit, "note": note}


def _hero_cells(c: dict[str, Any]) -> list[dict[str, str]]:
    xp = c.get("xp")
    return [
        _cell("KPM", fixed(c.get("kpm")), note="每分钟真人击杀"),
        _cell("SPM", num(c.get("spm")), note="每分钟得分"),
        _cell(
            "胜率",
            pct(c.get("win_rate")),
            note=f"{num(c.get('wins'))} 胜 {num(c.get('losses'))} 负",
        ),
        _cell("场次", num(c.get("matches"))),
        _cell("战斗时长", hours(c.get("time_seconds")), "小时"),
        _cell(
            "经验",
            f"{xp / 1e6:.2f}" if xp and xp >= 1e6 else num(xp),
            "M" if xp and xp >= 1e6 else "",
        ),
    ]


def _combat(c: dict[str, Any]) -> list[dict[str, str]]:
    damage = compact(c.get("damage"))
    return [
        _cell("击杀", num(c.get("kills")), note="含 AI"),
        _cell("助攻", num(c.get("assists"))),
        _cell("伤害", *damage),
        _cell("爆头击杀", num(c.get("headshots")), note=pct(c.get("headshot_rate"))),
        _cell("命中率", pct(c.get("accuracy"))),
        _cell("最高连杀", num(c.get("longest_killstreak"))),
        _cell("多杀", num(c.get("multi_kills"))),
        _cell("远距离击杀", num(c.get("long_distance_kills"))),
        _cell("载具击杀", num(c.get("vehicle_kills"))),
        _cell("摧毁载具", num(c.get("vehicles_destroyed"))),
        _cell("急救", num(c.get("revives"))),
        _cell("修理", num(c.get("repairs"))),
        _cell("补给队友", num(c.get("resupplies"))),
        _cell("占领据点", num(c.get("captures"))),
        _cell("处决", num(c.get("takedowns"))),
        _cell("移动距离", km(c.get("distance_m")), "km"),
    ]


def _bests(b: dict[str, Any]) -> list[dict[str, str]]:
    return [
        _cell("单局最多击杀", num(b.get("round_kills"))),
        _cell("单局最高得分", num(b.get("round_score"))),
        _cell("一条命最多击杀", num(b.get("life_kills"))),
        _cell("最远击杀", num(b.get("longest_kill_m")), "m"),
        _cell("单局最多爆头", num(b.get("round_headshots"))),
        _cell("单局最高伤害", num(b.get("round_damage"))),
        _cell("单局最多急救", num(b.get("round_revives"))),
        _cell("单局最多守点", num(b.get("round_defends"))),
    ]


def _by(key: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda i: i.get(key) or 0, reverse=True)


def _kits(kits: list[dict[str, Any]], best: str | None) -> list[dict[str, Any]]:
    total = sum(k.get("time_seconds") or 0 for k in kits)
    return [
        {
            "name": catalog.name("kits", k),
            "best": k["id"] == best,
            "share": share(k.get("time_seconds"), total),
            "hours": hours(k.get("time_seconds")),
            "kills": num(k.get("kills")),
            "kd": fixed(k.get("kd")),
        }
        for k in _by("time_seconds", kits)
    ]


def _weapon_mix(categories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = [c for c in _by("kills", categories) if c.get("kills")]
    total = sum(c["kills"] for c in ranked)
    return [
        {
            "name": catalog.name("weapon_categories", c),
            "kills": num(c["kills"]),
            "share": share(c["kills"], total),
            "kpm": fixed(c.get("kpm")),
        }
        for c in ranked
    ]


SIGNATURE_WEAPONS = 3


def _signature(stats: dict[str, Any]) -> list[dict[str, Any]]:
    """王牌装备：击杀最多的三把武器和一辆载具，写上占生涯总击杀的多少。"""

    total = stats["career"].get("kills")
    weapons = [w for w in _by("kills", stats["weapons"]) if w.get("kills")][:SIGNATURE_WEAPONS]
    vehicles = [v for v in _by("kills", stats["vehicles"]) if v.get("kills")][:1]
    gear = [
        {
            "name": catalog.name("weapons", w),
            "image": catalog.image("weapons", w),
            "category": catalog.weapon_category(w["id"]),
            "kills": num(w["kills"]),
            "share": pct(share(w["kills"], total)),
            "rate": f"KPM {fixed(w.get('kpm'))} · 爆头率 {pct(w.get('headshot_rate'))}",
        }
        for w in weapons
    ]
    gear += [
        {
            "name": catalog.name("vehicles", v),
            "image": catalog.image("vehicles", v),
            "category": catalog.vehicle_type(v["id"]) or "载具",
            "kills": num(v["kills"]),
            "share": pct(share(v["kills"], total)),
            "rate": f"KPM {fixed(v.get('kpm'))} · {duration(v.get('time_seconds'))}",
        }
        for v in vehicles
    ]
    return gear


CAREER_MODES = 6
CAREER_MAPS = 6


def _modes(modes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": catalog.name("modes", m),
            "matches": num(m.get("matches")),
            "win_rate": pct(m.get("win_rate"), 0),
            "kills": num(m.get("kills")),
            "hours": hours(m.get("time_seconds")),
        }
        for m in _by("time_seconds", modes)[:CAREER_MODES]
    ]


def _maps(maps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": catalog.name("maps", m),
            "image": catalog.image("maps", m),
            "matches": num(m.get("matches")),
            "win_rate": pct(m.get("win_rate"), 0),
            "hours": hours(m.get("time_seconds")),
        }
        for m in _by("time_seconds", maps)[:CAREER_MAPS]
    ]


def _redsec(r: dict[str, Any] | None) -> list[dict[str, str]] | None:
    if not r or not r.get("matches"):
        return None
    return [
        _cell("场次", num(r.get("matches"))),
        _cell("吃鸡", num(r.get("wins")), note=pct(r.get("win_rate"))),
        _cell("前三", num(r.get("top3"))),
        _cell("前十", num(r.get("top10"))),
        _cell("击杀", num(r.get("kills"))),
        _cell("最长连胜", num(r.get("longest_win_streak"))),
        _cell("单局团灭小队", num(r.get("round_squad_wipes"))),
        _cell("时长", hours(r.get("time_seconds")), "小时"),
    ]


def stats_page(player: dict[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
    c = stats["career"]
    best = c.get("best_class")
    top_mode = _by("time_seconds", stats["modes"])[:1]
    return {
        **_common(player, "生涯战绩"),
        "has_data": stats["has_data"],
        "kit": catalog.name("kits", best) if best else None,
        "kit_image": catalog.image("kits", best) if best else None,
        "top_mode": catalog.name("modes", top_mode[0]) if top_mode else None,
        "kd": fixed(c.get("kd")),
        "human_kills": num(c.get("human_kills")),
        "deaths": num(c.get("deaths")),
        "cells": _hero_cells(c),
        "combat": _combat(c),
        "kits": _kits(stats["kits"], best),
        "mix": _weapon_mix(stats["weapon_categories"]),
        "signature": _signature(stats),
        "bests": _bests(stats["bests"]),
        "modes": _modes(stats["modes"]),
        "maps": _maps(stats["maps"]),
        "redsec": _redsec(stats.get("redsec")),
    }


# —— 武器、载具、配备排行 ——


@dataclass(frozen=True)
class Sort:
    key: str
    label: str
    words: tuple[str, ...]
    # 按比率排时击杀太少的不参与排名，免得一两个击杀的排到最前面
    rate: bool = False


MIN_RATE_KILLS = 10
_KILLS = Sort("kills", "击杀", ("击杀", "kills", "kill"))
_KPM = Sort("kpm", "KPM", ("kpm",), rate=True)
_TIME = Sort("time_seconds", "时长", ("时长", "时间", "time"))
_DAMAGE = Sort("damage", "伤害", ("伤害", "damage", "dmg"))
SORTS = {
    "weapons": (
        _KILLS,
        _KPM,
        Sort("accuracy", "命中率", ("命中率", "命中", "acc"), rate=True),
        Sort("headshot_rate", "爆头率", ("爆头率", "爆头", "hs"), rate=True),
        _DAMAGE,
        _TIME,
    ),
    "vehicles": (_KILLS, _KPM, _DAMAGE, _TIME, Sort("distance_m", "里程", ("里程", "距离"))),
    "gadgets": (
        Sort("uses", "使用", ("使用", "次数", "uses")),
        _KILLS,
        _DAMAGE,
        Sort("vehicles_destroyed", "摧毁", ("摧毁", "摧毁载具")),
        Sort("repairs", "修理", ("修理", "repair")),
    ),
}


def find_sort(kind: str, word: str) -> Sort | None:
    folded = word.casefold()
    return next((s for s in SORTS[kind] if folded in (w.casefold() for w in s.words)), None)


def _fold(text: str) -> str:
    """搜名字用：不分大小写和全半角，空格、-、·、. 忽略。"""

    return re.sub(r"[\s\-·._/]", "", unicodedata.normalize("NFKC", text).casefold())


def name_matches(keyword: str, name: str) -> bool:
    return _fold(keyword) in _fold(name)


def ranked(item: dict[str, Any], sort: Sort) -> bool:
    if item.get(sort.key) is None:
        return False
    return not sort.rate or (item.get("kills") or 0) >= MIN_RATE_KILLS


def rank_items(
    items: list[dict[str, Any]], sort: Sort, keyword: str | None
) -> list[tuple[dict[str, Any], bool]]:
    """按排序项从高到低排好；不带 -n 时只列上榜的，带 -n 时没上榜的置灰排在后面。"""

    on = sorted((i for i in items if ranked(i, sort)), key=lambda i: i[sort.key], reverse=True)
    if not keyword:
        return [(i, True) for i in on]
    off = sorted((i for i in items if not ranked(i, sort)), key=lambda i: -(i.get("kills") or 0))
    return [(i, True) for i in on] + [(i, False) for i in off]


def _weapon_card(w: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": catalog.name("weapons", w),
        "image": catalog.image("weapons", w),
        "category": catalog.weapon_category(w["id"]),
        "stats": [
            num(w.get("kills")),
            fixed(w.get("kpm")),
            pct(w.get("accuracy")),
            pct(w.get("headshot_rate")),
            "".join(compact(w.get("damage"))),
            hours(w.get("time_seconds")),
        ],
    }


def _vehicle_card(v: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": catalog.name("vehicles", v),
        "image": catalog.image("vehicles", v),
        "category": catalog.vehicle_type(v["id"]) or "载具",
        "stats": [
            num(v.get("kills")),
            fixed(v.get("kpm")),
            "".join(compact(v.get("damage"))),
            hours(v.get("time_seconds")),
            km(v.get("distance_m")),
        ],
    }


def _gadget_card(g: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": catalog.name("gadgets", g),
        "image": catalog.image("gadgets", g),
        "category": catalog.gadget_group(g["id"]),
        "stats": [
            num(g.get("uses")),
            num(g.get("kills")),
            "".join(compact(g.get("damage"))),
            num(g.get("vehicles_destroyed")),
            num(g.get("repairs")),
        ],
    }


CARDS: dict[str, tuple[Callable[[dict[str, Any]], dict[str, Any]], tuple[str, ...]]] = {
    "weapons": (_weapon_card, ("击杀", "KPM", "命中率", "爆头率", "伤害", "小时")),
    "vehicles": (_vehicle_card, ("击杀", "KPM", "伤害", "小时", "里程 km")),
    "gadgets": (_gadget_card, ("使用", "击杀", "伤害", "摧毁", "修理")),
}
# 每种排序项在卡片数据里是第几格，用来标橙
HIGHLIGHT = {
    "weapons": {
        "kills": 0,
        "kpm": 1,
        "accuracy": 2,
        "headshot_rate": 3,
        "damage": 4,
        "time_seconds": 5,
    },
    "vehicles": {"kills": 0, "kpm": 1, "damage": 2, "time_seconds": 3, "distance_m": 4},
    "gadgets": {"uses": 0, "kills": 1, "damage": 2, "vehicles_destroyed": 3, "repairs": 4},
}
ITEM_COLS, ITEM_ROWS = 2, 5
MAX_COLS, MAX_ROWS = 6, 10
COL_WIDTH = 470


def item_title(kind: str, label: str | None) -> str:
    what = {"weapons": "武器", "vehicles": "载具", "gadgets": "配备"}[kind]
    return f"{label}排行" if label else f"{what}排行"


def items_page(
    player: dict[str, Any],
    kind: str,
    items: list[tuple[dict[str, Any], bool]],
    title: str,
    sort: Sort,
    layout: tuple[int, int],
    note: list[str],
) -> dict[str, Any]:
    cols, rows = layout
    card, columns = CARDS[kind]
    return {
        **_common(player, title),
        "width": max(WIDTH, 48 + cols * COL_WIDTH + (cols - 1) * 18),
        "cols": cols,
        "columns": columns,
        "highlight": HIGHLIGHT[kind][sort.key],
        "items": [{**card(i), "muted": not on} for i, on in items[: cols * rows]],
        "note": note,
    }


# —— 对局估算 ——


def _clock(at: datetime) -> str:
    return at.strftime("%H:%M")


def _day(at: datetime, today: date) -> str:
    days = (today - at.date()).days
    weekday = "一二三四五六日"[at.weekday()]
    if days == 0:
        return f"今天 · 周{weekday}"
    if days == 1:
        return f"昨天 · 周{weekday}"
    return f"{at.month} 月 {at.day} 日 · 周{weekday}"


def _local(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(CST)


def _outcome(m: dict[str, Any]) -> tuple[str, str]:
    """一场时写胜负；合并了几场时写几胜几负；场次没涨（中途退出）时写未计场次。"""

    if m["matches"] == 0:
        return "partial", "未计场次"
    if m["matches"] == 1 and m["wins"]:
        return "win", "胜利"
    if m["matches"] == 1 and m["losses"]:
        return "loss", "失败"
    if m["wins"] or m["losses"]:
        return "merged", f"{m['wins']} 胜 {m['losses']} 负"
    return "merged", f"{m['matches']} 场"


def _mode_list(splits: list[dict[str, Any]]) -> str:
    return "、".join(
        catalog.name("modes", s) + (f" ×{s['matches']}" if s["matches"] > 1 else "") for s in splits
    )


def _round(m: dict[str, Any]) -> dict[str, Any]:
    single = m["matches"] == 1
    map_ = m["maps"][0] if single and m["maps"] else None
    mode = m["modes"][0] if m["modes"] else None
    if single:
        title = (
            catalog.name("maps", map_) if map_ else catalog.name("modes", mode) if mode else "1 场"
        )
    else:
        title = f"{m['matches']} 场" if m["matches"] else "未完成的对局"
    subtitle = catalog.name("modes", mode) if single and mode and map_ else ""
    if not single:
        subtitle = _mode_list(m["modes"])
    start, end = _local(m["started_after"]), _local(m["observed_at"])
    span = (
        f"{_clock(start)}–{_clock(end)}"
        if start.date() == end.date()
        else f"{start.month}/{start.day} {_clock(start)} – {_clock(end)}"
    )
    weapon = next(
        (w for w in m["weapons"] if w["kills"]), m["weapons"][0] if m["weapons"] else None
    )
    vehicle = next((v for v in m["vehicles"] if v["kills"]), None)
    gear = []
    if weapon:
        gear.append(
            catalog.name("weapons", weapon) + (f" {weapon['kills']} 杀" if weapon["kills"] else "")
        )
    if vehicle:
        gear.append(f"{catalog.name('vehicles', vehicle)} {vehicle['kills']} 杀")
    outcome, label = _outcome(m)
    backdrop = (
        catalog.image("maps", map_) if map_ else catalog.image("modes", mode) if mode else None
    )
    return {
        "outcome": outcome,
        "label": label,
        "title": title,
        "subtitle": " · ".join(t for t in (subtitle, span) if t),
        "kit": m.get("kit_name") or (catalog.name("kits", m["kit"]) if m.get("kit") else None),
        "gear": " · ".join(gear),
        "backdrop": backdrop,
        "kills": num(m["human_kills"]),
        "deaths": num(m["deaths"]),
        "kd": fixed(m.get("kd")),
        "score": num(m["score"]),
        "minutes": minutes(m["time_seconds"]),
        "end": end,
    }


def recent_page(player: dict[str, Any], data: dict[str, Any], count: int) -> dict[str, Any]:
    today = datetime.now(CST).date()
    days: list[dict[str, Any]] = []
    for m in data["matches"][:count]:
        row = _round(m)
        label = _day(row["end"], today)
        if not days or days[-1]["label"] != label:
            days.append({"label": label, "rounds": [], "matches": 0, "wins": 0})
        days[-1]["rounds"].append(row)
        days[-1]["matches"] += m["matches"]
        days[-1]["wins"] += m["wins"]
    since = data.get("tracked_since")
    return {
        **_common(player, "对局估算"),
        "days": days,
        "tracking": data["tracking"],
        "since": tracked_since(since) if since else None,
    }


def tracked_since(iso: str) -> str:
    at = _local(iso)
    return f"{at.month} 月 {at.day} 日 {_clock(at)}"


# —— 出图 ——


def _host_font(service: Any) -> str | None:
    # 宿主自带思源黑体 GB2312 子集；拿不到时用系统字体。
    path = Path(getattr(service, "template_dir", "")) / "assets/fonts/NotoSansSC-GB2312.woff2"
    return _data_uri(path) if path.is_file() else None


def html(template: str, context: dict[str, Any], sc_font: str | None = None) -> str:
    return _env.get_template(template).render(sc_font=sc_font, **context)


async def _screenshot(service: Any, page_html: str, width: int = WIDTH) -> bytes:
    # shortcut: 宿主的 render_template 只认宿主自带模板，这里借它的共享浏览器截插件自己的页面；
    # tenko-api 提供渲染任意 HTML 的接口后改用它。
    if not await service.start() or service.browser is None:
        raise RuntimeError("浏览器不可用")
    context = await service.browser.new_context(
        # 整页截图不会比视口矮，视口给小一点，内容短时才不会在下面留一截黑底。
        viewport={"width": width, "height": 100},
        device_scale_factor=1,
    )
    try:
        page = await context.new_page()
        try:
            await page.set_content(page_html, wait_until="load", timeout=15_000)
        except Exception as error:  # 个别远程图片拉不到时照样截，缺图的位置会隐藏
            logger.debug("战地六图片有资源没加载完：{!r}", error)
        return await page.screenshot(full_page=True, type="jpeg", quality=75)
    finally:
        await context.close()


async def render(service: Any, template: str, context: dict[str, Any]) -> bytes | None:
    """渲染失败返回 None，由调用方回退文字。"""

    if service is None:
        return None
    try:
        page_html = html(template, context, _host_font(service))
        width = context.get("width", WIDTH)
        return await asyncio.wait_for(_screenshot(service, page_html, width), RENDER_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning("战地六图片渲染失败，回退文字：{!r}", error)
        return None
