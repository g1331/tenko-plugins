"""把 bf-manager 返回的数据整理成模板上下文，并截成图片。

这里不碰 Entari：输入是接口返回的 dict，输出是模板上下文或图片字节。
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import random
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any

import zhconv
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from loguru import logger

from . import collection
from .names import CAREER_MODES, ELITE_KITS, MAPS

HERE = Path(__file__).parent
ASSETS = HERE / "assets"
WIDTH = 1000
RENDER_TIMEOUT = 40

CLASSES = {
    "assault": "突击兵",
    "medic": "医疗兵",
    "support": "支援兵",
    "scout": "侦察兵",
    "tanker": "坦克兵",
    "pilot": "飞行员",
    "cavalry": "骑兵",
}
SKIN_RARITY = {"Superior": "gold", "Enhanced": "blue"}
GOLD, BLUE, WHITE = "#ca843a", "#1e90ff", "#ffffff"

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


def cn(text: str | None) -> str:
    """EA 只给繁体名字，转成简体。"""

    return zhconv.convert(text, "zh-cn") if text else ""


def _int(value: Any) -> str:
    return f"{value:,}" if isinstance(value, int) else "-"


def _fixed(value: float | None, digits: int = 2) -> str:
    return f"{value:.{digits}f}" if isinstance(value, int | float) else "-"


def _ratio(part: float, whole: float) -> str:
    return f"{part / whole:.1%}" if whole else "-"


def _kpm(kills: int | None, seconds: float | None) -> str:
    return f"{(kills or 0) / (seconds / 60):.2f}" if seconds else "-"


def _duration(seconds: float | None) -> str:
    """和网页一样写成 291h 37m。"""

    minutes = int((seconds or 0) // 60)
    return f"{minutes // 60}h {minutes % 60}m"


def _percent(part: float, whole: float) -> int:
    return round(part / whole * 100) if whole else 0


def _rarity(value: int, gold: int, blue: int) -> str:
    return GOLD if value >= gold else BLUE if value >= blue else WHITE


def top_kills(items: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    played = [i for i in items if i.get("kills")]
    return sorted(played, key=lambda i: i["kills"], reverse=True)[:count]


def _common(source: str) -> dict[str, Any]:
    backgrounds = sorted((ASSETS / "backgrounds").iterdir())
    return {
        "background": _data_uri(random.choice(backgrounds)),
        "source": source,
        "now": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def _portrait(cls: str) -> str:
    return asset(f"classes/{cls if cls in CLASSES else 'assault'}.webp")


def _image(item: dict[str, Any]) -> str:
    return item.get("skin_image") or item.get("image") or ""


def _gear(items: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    top = top_kills(items, count)
    most = top[0]["kills"] if top else 0
    return [
        {
            "name": cn(i.get("name")),
            "image": _image(i),
            # 装着皮肤时图是皮肤图，名字下面写皮肤名，颜色按稀有度
            "skin": cn(i.get("skin_name")) if i.get("skin_image") else "",
            "rarity": SKIN_RARITY.get(i.get("skin_rarity") or "", "white"),
            "kills": _int(i["kills"]),
            "ratio": _percent(i["kills"], most),
        }
        for i in top
    ]


def _split(title, left_label, left, left_color, right_label, right, right_color):
    return {
        "title": title,
        "left_label": left_label,
        "left": _int(left),
        "left_color": left_color,
        "right_label": right_label,
        "right": _int(right),
        "right_color": right_color,
        "pct": _percent(left, left + right),
    }


def equipped_dogtags(ids: list[str] | None, site: str = "") -> list[dict[str, str]]:
    """玩家装备着的两块狗牌（收藏接口的 equipped），图鉴里没有的跳过。"""

    tags = [collection.DOGTAG[i] for i in ids or [] if i in collection.DOGTAG]
    return [{"name": t.name, "image": collection.image("dogtags", t.id, site)} for t in tags]


def collection_page(
    persona: dict[str, Any],
    platoon: dict[str, Any] | None,
    stats: dict[str, Any] | None,
    found: dict[str, Any],
    source: str,
    site: str,
) -> dict[str, Any]:
    """收藏（网页战绩页的「收藏」分页）：佩戴的狗牌、勋章墙、狗牌墙，外加生涯里的勋带墙。

    found 是 /stats/{pid}/collection；勋带次数在生涯战绩里，stats 拿不到就不画勋带墙。
    勋章解锁后 EA 会把阶段进度清零，只有没解锁的才写进度。
    """

    medals = {m["code"]: m for m in found.get("medals") or []}
    dogtags = {t["id"]: t for t in found.get("dogtags") or []}

    def medal(m: collection.Medal) -> dict[str, Any]:
        state = medals.get(m.code) or {}
        done = bool(state.get("unlocked"))
        stages = state.get("stages_done") or 0
        return {
            "name": m.name,
            "image": collection.image("medals", m.code, site),
            "got": done,
            "progress": f"{stages}/{state.get('stages_total') or m.stages}"
            if not done and stages
            else "",
        }

    def dogtag(t: collection.Dogtag) -> dict[str, Any]:
        state = dogtags.get(t.id) or {}
        acquired, needed = state.get("acquired"), state.get("needed")
        return {
            "name": t.name,
            "image": collection.image("dogtags", t.id, site),
            "got": bool(state.get("unlocked")),
            "progress": min(100, round(acquired / needed * 100))
            if not state.get("unlocked") and acquired and needed
            else 0,
        }

    ribbon_counts = (
        {r["code"]: r.get("count") or 0 for r in stats["ribbons"]}
        if stats and stats.get("ribbons") is not None
        else None
    )
    ribbons = (
        [
            (
                category,
                [
                    {
                        "name": r.name,
                        "image": collection.image("ribbons", r.code, site),
                        "count": ribbon_counts.get(r.code, 0),
                    }
                    for r in items
                ],
            )
            for category, items in collection.grouped(collection.RIBBONS)
        ]
        if ribbon_counts is not None
        else []
    )
    return {
        **_common(source),
        "width": COLLECTION_WIDTH,
        "name": persona["display_name"],
        "tag": (platoon or {}).get("tag"),
        "avatar_url": persona.get("avatar_url") or "",
        "equipped": equipped_dogtags(found.get("equipped"), site),
        "medals": [
            (c, [medal(m) for m in items]) for c, items in collection.grouped(collection.MEDALS)
        ],
        "medals_got": sum(1 for m in medals.values() if m.get("unlocked")),
        "medals_total": len(collection.MEDALS),
        "dogtags": [
            (c, [dogtag(t) for t in items]) for c, items in collection.grouped(collection.DOGTAGS)
        ],
        "dogtags_got": sum(1 for t in dogtags.values() if t.get("unlocked")),
        "dogtags_total": len(collection.DOGTAGS),
        "ribbons": ribbons,
        "ribbons_got": sum(1 for n in (ribbon_counts or {}).values() if n),
        "ribbons_total": len(collection.RIBBONS),
        "ribbons_sum": sum((ribbon_counts or {}).values()),
    }


PLATOON_FIELDS = ("tag", "name", "size", "description", "emblem_url", "verified")


def _platoon(platoon: dict[str, Any] | None) -> dict[str, Any] | None:
    """战队字段都可能缺，补齐成 None，模板里直接判断。"""

    return {key: platoon.get(key) for key in PLATOON_FIELDS} if platoon else None


def _bans(ban: dict[str, Any] | None) -> tuple[list[dict[str, Any]], str]:
    """BFBAN、BFEAC 两个标记，和判定作弊的来源（没有则为空）。"""

    ban = ban or {}
    bans = [
        {"source": "BFBAN", "logo": asset("bfban.webp"), "hit_text": "实锤"},
        {"source": "BFEAC", "logo": asset("bfeac.webp"), "hit_text": "已封禁"},
    ]
    for b in bans:
        state = ban.get(b["source"].lower())
        b["state"] = state if state in ("hit", "clean") else "unknown"
    return bans, " / ".join(b["source"] for b in bans if b["state"] == "hit")


def _pct(value: float | None) -> str:
    return f"{value:.1%}" if isinstance(value, int | float) else "-"


def _wan(value: int | None) -> str:
    """得分到百万级，和网页一样用“万”。"""

    if value is None:
        return "-"
    if value < 10_000:
        return f"{value:,}"
    return f"{value / 10_000:.{0 if value >= 1_000_000 else 1}f} 万"


# 战斗、团队的细项，和网页 CareerOverview 一致；值是 (分组, 键, 名字)
DETAILS = (
    ("combat", "headshot_rate", "爆头率"),
    ("combat", "accuracy", "命中率"),
    ("combat", "squad_wipes", "团灭"),
    ("combat", "vehicles_destroyed", "摧毁载具"),
    ("combat", "avenger_kills", "复仇击杀"),
    ("combat", "savior_kills", "救星击杀"),
    ("combat", "nemesis_kills", "宿敌击杀"),
    ("combat", "max_nemesis_streak", "最高宿敌连杀"),
    ("teamwork", "flag_captures", "占点"),
    ("teamwork", "flag_defends", "守点"),
    ("teamwork", "resupplies", "补给"),
    ("teamwork", "spot_assists", "索敌助攻"),
    ("teamwork", "suppression_assists", "压制助攻"),
    ("teamwork", "squad_kill_assists", "小队击杀助攻"),
    ("teamwork", "squad_spawns", "小队重生"),
    ("teamwork", "orders_completed", "完成命令"),
)
CAREER_MAPS = 6
STATS_WIDE = 1760


def career_details(stats: dict[str, Any]) -> list[tuple[str, str]]:
    rows = []
    for group, key, label in DETAILS:
        part = stats.get(group)
        if part is None:
            continue
        value = part.get(key)
        rows.append((label, _pct(value) if key.endswith(("rate", "accuracy")) else _int(value)))
    return rows


def career_modes(modes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """各模式胜负和目标得分，按局数排；没玩过的不列。"""

    played = [m for m in modes if (m.get("wins") or 0) + (m.get("losses") or 0)]
    played.sort(key=lambda m: (m.get("wins") or 0) + (m.get("losses") or 0), reverse=True)
    return [
        {
            "name": CAREER_MODES.get(m["mode"], m["mode"]),
            "record": f"{_int(m.get('wins') or 0)} / {_int(m.get('losses') or 0)}",
            "rate": _pct(m.get("win_rate")),
            "score": _wan(m.get("score")),
        }
        for m in played
    ]


def career_maps(maps: list[dict[str, Any]] | None, site: str) -> list[dict[str, Any]]:
    """最常玩的几张图：地图图片用 bf-manager 网站的图鉴图。"""

    if not maps:
        return []
    total = sum(m.get("time_seconds") or 0 for m in maps)
    return [
        {
            "name": MAPS.get(m["map"], m["map"]),
            "image": f"{site.rstrip('/')}/bf1/codex/maps-480/{m['map']}.webp" if site else "",
            "hours": f"{(m.get('time_seconds') or 0) / 3600:,.0f} 小时",
            "share": _ratio(m.get("time_seconds") or 0, total),
        }
        for m in maps[:CAREER_MAPS]
    ]


def elite_kits(kits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    played = sorted(
        (k for k in kits if k.get("time_seconds")), key=lambda k: k["time_seconds"], reverse=True
    )
    return [
        {
            "name": ELITE_KITS.get(k["kit"], k["kit"]),
            "time": _duration(k["time_seconds"]),
            "kills": _int(k.get("kills")),
            "kpm": _kpm(k.get("kills"), k["time_seconds"]) if k.get("kills") is not None else "-",
        }
        for k in played
    ]


CAREER_RIBBONS = 10


def ribbon_rows(
    ribbons: list[dict[str, Any]] | None, site: str, count: int | None = None
) -> list[dict[str, Any]]:
    """拿过的勋带按次数排，带名字和图；count 为空时全列。"""

    got = sorted((r for r in ribbons or [] if r.get("count")), key=lambda r: -r["count"])
    return [
        {
            "name": info.name if (info := collection.RIBBON.get(r["code"])) else r["code"],
            "image": collection.image("ribbons", r["code"], site) if site else "",
            "count": r["count"],
        }
        for r in got[:count]
    ]


def _honors(stats: dict[str, Any]) -> list[tuple[str, str]]:
    honors, misc = stats.get("honors"), stats.get("misc")
    tiles = []
    if honors:
        tiles += [("勋带", _int(honors.get("ribbons"))), ("勋章", _int(honors.get("medals")))]
    if misc:
        tiles += [
            ("完成率", _pct(misc.get("completion_rate"))),
            ("打到结算", f"{_int(misc.get('rounds_completed'))} 局"),
        ]
    return tiles


def stats_page(
    persona: dict[str, Any],
    stats: dict[str, Any],
    weapons: list[dict[str, Any]],
    vehicles: list[dict[str, Any]],
    ban: dict[str, Any] | None,
    platoon: dict[str, Any] | None,
    online: dict[str, Any] | None,
    source: str,
    site: str = "",
    tip: Any = None,
    equipped: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """site 是 bf-manager 网站地址，地图图片从那里取；tip 是没加入战队时放在战队位置的小贴士；
    equipped 是 equipped_dogtags 的结果，放在名字旁边。

    荣誉、细项、模式、精英兵、地图是 2026-11 起的扩展字段，接口没给时对应部分不画。
    """

    s = stats["summary"]
    wins, losses = s.get("wins") or 0, s.get("losses") or 0
    soldiers = sorted(
        stats.get("soldiers") or [], key=lambda c: c.get("time_seconds") or 0, reverse=True
    )
    best = s.get("best_class") or (soldiers[0]["class"] if soldiers else "assault")
    total_time = sum(c.get("time_seconds") or 0 for c in soldiers)
    longest_time = (soldiers[0].get("time_seconds") or 0) if soldiers else 0
    played_hours = (s.get("time_played_seconds") or 0) / 3600
    rank = s.get("rank")
    longest = s.get("longest_headshot_meters")
    bans, banned_by = _bans(ban)

    def per_hour(value: int | None) -> str:
        return f"{(value or 0) / played_hours:.1f}" if played_hours else "-"

    def class_row(c: dict[str, Any]) -> dict[str, Any]:
        seconds = c.get("time_seconds") or 0
        return {
            "name": CLASSES.get(c["class"], c["class"]),
            "portrait": _portrait(c["class"]),
            "best": c["class"] == best,
            "share": _ratio(seconds, total_time),
            "ratio": _percent(seconds, longest_time),
            "time": _duration(seconds),
            "kills": _int(c.get("kills")),
            "kpm": _kpm(c.get("kills"), seconds),
            "spm": f"{(c.get('score') or 0) / (seconds / 60):.0f}" if seconds else "-",
        }

    return {
        **_common(source),
        "name": persona["display_name"],
        "pid": persona["persona_id"],
        "avatar_url": persona.get("avatar_url") or "",
        "online": online.get("is_online") if online else None,
        "online_server": (online or {}).get("server_name"),
        "platoon": _platoon(platoon),
        "equipped": equipped or [],
        "rank": rank if rank is not None else "-",
        "rank_color": _rarity(rank or 0, 150, 100),
        "hours": f"{played_hours:,.0f} 小时",
        "best_class": CLASSES.get(best, best),
        "portrait": _portrait(best),
        "bans": bans,
        "banned": bool(banned_by),
        "banned_by": banned_by,
        "kd": _fixed(s.get("kd")),
        "kpis": [
            ("KPM", _fixed(s.get("kpm"))),
            ("SPM", f"{s['sps'] * 60:,.0f}" if s.get("sps") else "-"),
            ("胜率", _ratio(wins, wins + losses)),
            ("技巧值", _fixed(s.get("skill"), 0)),
            *(
                [("MVP", _int(honors.get("mvp"))), ("王牌小队", _int(honors.get("ace_squad")))]
                if (honors := stats.get("honors"))
                else []
            ),
        ],
        "splits": [
            _split(
                "击杀构成",
                "步战",
                s.get("infantry_kills") or 0,
                "#f59e0b",
                "载具",
                s.get("vehicle_kills") or 0,
                "#64748b",
            ),
            _split("胜负 · 胜率", "胜", wins, "#10b981", "负", losses, "#ef4444"),
        ],
        "tiles": [
            ("击杀", _int(s.get("kills")), "crosshair", False),
            ("死亡", _int(s.get("deaths")), "skull", False),
            ("协助击杀", _int(s.get("assists")), "handshake", False),
            ("最高连杀", _int(s.get("max_killstreak")), "flame", True),
            ("最远爆头", f"{longest:.0f} m" if longest is not None else "-", "target", False),
            ("狗牌", _int(s.get("dogtags")), "tag", False),
        ],
        "team": [
            (label, _int(s.get(key)), ico, per_hour(s.get(key)))
            for label, key, ico in [
                ("复活", "revives", "heart"),
                ("治疗", "heals", "syringe"),
                ("修理", "repairs", "wrench"),
            ]
        ],
        "classes": [class_row(c) for c in soldiers],
        "elite": elite_kits(stats.get("elite_kits") or []),
        "tip": {"text": tip.text, "tags": list(tip.tags)} if tip else {"text": "", "tags": []},
        "gear": [("常用武器", _gear(weapons, 4)), ("常用载具", _gear(vehicles, 4))],
        "details": career_details(stats),
        "honors": (honor_tiles := _honors(stats)),
        "ribbons": ribbon_rows(stats.get("ribbons"), site, CAREER_RIBBONS),
        "modes": career_modes(stats.get("modes") or []),
        "maps": (map_rows := career_maps(stats.get("maps"), site)),
        # 有生涯扩展数据就排成左右两栏，图不至于拉得太长
        "wide": (wide := bool(honor_tiles or map_rows or stats.get("modes"))),
        "width": STATS_WIDE if wide else WIDTH,
    }


def _weapon_stats(w: dict[str, Any]) -> list[str]:
    kills = w["kills"]
    return [
        _int(kills),
        _kpm(kills, w.get("time_seconds")),
        f"{w['accuracy']:.1f}%" if w.get("accuracy") is not None else "-",
        _ratio(w.get("headshots") or 0, kills),
        _duration(w.get("time_seconds")),
    ]


def _vehicle_stats(v: dict[str, Any]) -> list[str]:
    return [
        _int(v["kills"]),
        _kpm(v["kills"], v.get("time_seconds")),
        _int(v.get("destroyed") or 0),
        _duration(v.get("time_seconds")),
    ]


# 武器、载具卡片默认 2 列 5 行；旧 bot 的 -c/-r 自己排，上限也沿用旧 bot 的 8 列 10 行
ITEM_COLS, ITEM_ROWS = 2, 5
MAX_COLS, MAX_ROWS = 8, 10
ITEM_CARD, ITEM_GAP, PAGE_PAD = 467, 18, 48


def items_page(
    kind: str,
    persona: dict[str, Any],
    platoon: dict[str, Any] | None,
    items: list[dict[str, Any]],
    source: str,
    cols: int = ITEM_COLS,
    rows: int = ITEM_ROWS,
    title: str | None = None,
) -> dict[str, Any]:
    weapon = kind == "weapons"
    cards = []
    for item in top_kills(items, cols * rows):
        stars = item["kills"] // 100
        cards.append(
            {
                "name": cn(item.get("name")),
                "category": cn(item.get("category")),
                "image": _image(item),
                "skin": cn(item.get("skin_name")),
                "rarity": SKIN_RARITY.get(item.get("skin_rarity") or "", "white"),
                "stars": stars,
                "star_color": _rarity(stars, 100, 60),
                "stats": _weapon_stats(item) if weapon else _vehicle_stats(item),
            }
        )
    # 不够一行就按实际张数收窄，免得右边空一大块
    shown = min(cols, len(cards)) if cards else cols
    return {
        **_common(source),
        "title": title or ("武器" if weapon else "载具"),
        "name": persona["display_name"],
        "tag": (platoon or {}).get("tag"),
        "avatar_url": persona.get("avatar_url") or "",
        "columns": ["击杀", "KPM", "命中率", "爆头率", "时长"]
        if weapon
        else ["击杀", "KPM", "摧毁", "时长"],
        "items": cards,
        "cols": shown,
        "width": PAGE_PAD + shown * ITEM_CARD + (shown - 1) * ITEM_GAP,
    }


ROLES = (("owner", "服主"), ("admin", "管理"), ("vip", "VIP"), ("ban", "封禁"))
# 全部列出时按最长的那类分栏：服名长，一栏至少六百来像素才放得下
FULL_COLUMNS = ((16, 1), (60, 2), (10**9, 3))
FULL_WIDTHS = {1: WIDTH, 2: 1400, 3: 1900}


def _played_servers(items: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """把最近的对局按服务器归并：最后一次在哪玩、玩了几局。排队、观战、进服就走的不算。"""

    servers: dict[Any, dict[str, Any]] = {}
    for item in items:  # 新的在前
        r, p = item["round"], item["player"]
        if not (p.get("kills") or 0) + (p.get("deaths") or 0):
            continue
        key = r.get("server_game_id") or r.get("server_name")
        if key not in servers:
            if len(servers) == count:
                continue
            servers[key] = {
                "name": r.get("server_name") or "-",
                "when": datetime.fromtimestamp(r["ended_at"]).strftime("%Y-%m-%d %H:%M"),
                "where": " · ".join(
                    x for x in (r.get("map_display_name"), r.get("mode_display_name")) if x
                ),
                "rounds": 0,
            }
        servers[key]["rounds"] += 1
    return list(servers.values())


def info_page(
    persona: dict[str, Any],
    rsp: dict[str, Any] | None,
    rounds: list[dict[str, Any]] | None,
    ban: dict[str, Any] | None,
    online: dict[str, Any] | None,
    platoon: dict[str, Any] | None,
    source: str,
    count: int,
    full: tuple[str, ...] = (),
) -> dict[str, Any]:
    """玩家信息（旧 bot 的 -info 加天眼查）：在哪些私服有身份、最近玩过的服、战队和封禁标记。

    最近玩过的服用 bf-manager 跟踪到的对局归并：EA 的“最近游玩”不准。
    full 里的身份（旧 bot 天眼查的 -o/-a/-v/-b）全部列出、多了分栏，其他身份只留个数，不列最近游玩。
    """

    bans, banned_by = _bans(ban)
    roles = []
    for key, label in ROLES:
        servers = (rsp or {}).get(key) or []
        shown = servers if key in full else [] if full else servers[:count]
        rows = [
            {
                "name": srv.get("name") or "-",
                "map": srv.get("map_display_name"),
                "players": f"{srv['player_count']}/{srv.get('max_player_count') or '-'}"
                if srv.get("player_count") is not None
                else None,
            }
            for srv in shown
        ]
        roles.append(
            {
                "key": key,
                "label": label,
                "total": len(servers),
                "rows": rows,
                "more": len(servers) - len(shown) if not full else 0,
                "full": key in full,
            }
        )
    longest = max((r["total"] for r in roles if r["full"]), default=0)
    columns = next(c for limit, c in FULL_COLUMNS if longest <= limit)
    return {
        **_common(source),
        "name": persona["display_name"],
        "pid": persona["persona_id"],
        "avatar_url": persona.get("avatar_url") or "",
        "online": online.get("is_online") if online else None,
        "online_server": (online or {}).get("server_name"),
        "platoon": platoon,
        "bans": bans,
        "banned": bool(banned_by),
        "banned_by": banned_by,
        "rsp": rsp is not None,
        "tracked": (rsp or {}).get("tracked_servers") or 0,
        "roles": roles,
        "rounds": rounds is not None,
        "recent": _played_servers(rounds or [], count),
        "full": bool(full),
        "columns": columns,
        "width": FULL_WIDTHS[columns],
    }


def catalog_page(category: str, entries: list[Any]) -> dict[str, Any]:
    """百科某个分类的目录，照旧 bot 的目录图分栏列出序号和名字；背景是其中一条的配图。"""

    columns = (
        1 if len(entries) <= 12 else 2 if len(entries) <= 44 else 3 if len(entries) <= 90 else 4
    )
    rows = -(-len(entries) // columns)
    return {
        **_common("游戏内典籍"),
        "background": random.choice(entries).image,
        "width": 120 + 430 * columns,
        "category": category,
        "columns": [
            [(e.no, e.name) for e in entries[i * rows : (i + 1) * rows]] for i in range(columns)
        ],
        "count": len(entries),
    }


def _server_row(s: dict[str, Any]) -> dict[str, Any]:
    players, most = s.get("player_count") or 0, s.get("max_player_count") or 0
    return {
        "name": s["name"],
        "map": s.get("map_display_name") or s.get("map_name") or "-",
        "map_image": s.get("map_image_url"),
        "mode": s.get("mode_display_name") or s.get("game_mode") or "-",
        "region": s.get("region_display_name") or "-",
        "country": s.get("country"),
        "tick": s.get("tick_rate"),
        "locked": bool(s.get("has_password")),
        "game_id": s.get("game_id"),
        "players": players,
        "max": most,
        "full": bool(most) and players >= most,
        "queue": s.get("queue_count") or 0,
        "spectators": s.get("spectator_count") or 0,
        "ratio": min(100, _percent(players, most)),
    }


def servers_page(
    keyword: str, servers: list[dict[str, Any]], total: int, source: str
) -> dict[str, Any]:
    shown = f"，按人数显示前 {len(servers)} 个" if total > len(servers) else ""
    return {
        **_common(source),
        "keyword": keyword,
        "summary": f"共找到 {total} 个有人的服务器{shown}",
        "servers": [_server_row(s) for s in servers],
    }


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


FACTIONS = {
    "法国": "fra",
    "德意志帝国": "ger",
    "大英帝国": "uk",
    "美国": "usa",
    "意大利王国": "ita",
    "俄罗斯帝国": "rus",
    "奥匈帝国": "ahu",
    "奥斯曼帝国": "otm",
    "皇家海军陆战队": "rm",
    "红军": "bol",
}


def _flag(faction: str | None) -> str | None:
    slug = FACTIONS.get(faction or "")
    return asset(f"factions/{slug}.png") if slug else None


def _team(team: dict[str, Any], first: int) -> dict[str, Any]:
    """一队的计分板；序号从 first 开始，两队接着编号。"""

    players = sorted(team.get("players") or [], key=lambda p: p.get("rank") or 0, reverse=True)
    rows, kds, kpms, wins = [], [], [], []
    for index, p in enumerate(players, first):
        stats = p.get("stats") or {}
        kd, kpm, win = stats.get("kd"), stats.get("kpm"), stats.get("win_rate")
        kds += [kd] if isinstance(kd, int | float) else []
        kpms += [kpm] if isinstance(kpm, int | float) else []
        wins += [win] if isinstance(win, int | float) else []
        hours = stats.get("time_hours")
        rows.append(
            {
                "index": index,
                "name": p.get("display_name") or "-",
                "rank": p.get("rank") or 0,
                "kd": _fixed(kd),
                "kd_hot": isinstance(kd, int | float) and kd >= 2,
                "kpm": _fixed(kpm),
                "kpm_hot": isinstance(kpm, int | float) and kpm >= 1.5,
                "win": f"{win:.1f}%" if isinstance(win, int | float) else "-",
                "hours": f"{hours:.0f}h" if isinstance(hours, int | float) else "-",
                "language": p.get("language") or "",
                "admin": bool(p.get("is_admin")),
                "vip": bool(p.get("is_vip")),
            }
        )
    win = _mean(wins)
    return {
        "faction": team.get("faction") or f"队伍 {team.get('team_id')}",
        "flag": _flag(team.get("faction")),
        "count": len(rows),
        "rank_150": team.get("rank_150_count") or 0,
        "kd": _fixed(_mean(kds)),
        "kpm": _fixed(_mean(kpms)),
        "win": f"{win:.1f}%" if win is not None else "-",
        "players": rows,
    }


def _operation(op: dict[str, Any] | None) -> dict[str, Any] | None:
    """行动进度，照网页的区域链：攻方旗 → 每张图缩略图 + 区域箭头（拿下蓝、没拿下红）→ 守方旗。"""

    maps = (op or {}).get("maps") or []
    index = (op or {}).get("map_index")
    if not maps or not isinstance(index, int) or not 0 <= index < len(maps):
        return None
    taken = op.get("sectors_taken") or 0
    track = []
    for i, m in enumerate(maps):
        total = m.get("sectors") or 0
        done = total if i < index else taken if i == index else 0
        track.append(
            {
                "image": m.get("image_url"),
                "current": i == index,
                "sectors": [k < done for k in range(total)],
            }
        )
    total = maps[index].get("sectors")
    # 正在争夺的是下一个区域；区域数未收录时只写已拿下几个
    where = f"区域 {min(taken + 1, total)}/{total}" if total else f"已推进 {taken} 区"
    return {
        "attacker": _flag(op.get("attacker")),
        "defender": _flag(op.get("defender")),
        "track": track,
        "where": f"{where} · 第 {index + 1}/{len(maps)} 张图",
        "next": maps[index + 1].get("name") if index + 1 < len(maps) else None,
    }


def server_page(
    detail: dict[str, Any], players: dict[str, Any] | None, source: str
) -> dict[str, Any]:
    extras = detail.get("extras") or {}
    owner = extras.get("owner") or {}
    teams, first = [], 1
    for raw in (players or {}).get("teams") or []:
        teams.append(_team(raw, first))
        first += max(32, teams[-1]["count"])
    summary = detail["summary"]
    common = _common(source)
    return {
        **common,
        # 背景用当前地图实景，拿不到才用随机背景
        "background": summary.get("map_image_url") or common["background"],
        "server": _server_row(summary),
        "operation": _operation((players or {}).get("operation")),
        "owner": owner.get("display_name"),
        "bookmarks": _int(extras.get("bookmark_count")),
        "roles": [
            (label, len(extras.get(key) or []))
            for label, key in [("管理", "admins"), ("VIP", "vips"), ("封禁", "banned")]
        ],
        "teams": teams,
        "waiting": [
            (label, color, [(p.get("display_name") or "-", p.get("rank") or 0) for p in people])
            for label, color, key in [
                ("排队", "#34d399", "queued"),
                ("观战", "#f87171", "spectators"),
            ]
            if (people := (players or {}).get(key))
        ],
        "players_missing": players is None,
        "online": [
            ("管理", sum(p["admin"] for t in teams for p in t["players"])),
            ("VIP", sum(p["vip"] for t in teams for p in t["players"])),
            ("150 级", sum(t["rank_150"] for t in teams)),
        ],
        "width": 1280,
    }


OUTCOMES = {
    "win": ("胜利", "#fbbf24"),
    "loss": ("失败", "rgba(255,255,255,.5)"),
    "left": ("中途退出", "rgba(255,255,255,.3)"),
    "unknown": ("胜负不明", "rgba(255,255,255,.3)"),
}


def _outcome(p: dict[str, Any]) -> str:
    if p.get("status") == "left":
        return "left"
    won = p.get("won")
    return "unknown" if won is None else "win" if won else "loss"


# 武器和它自己的生涯比：命中率照网页的门槛，KPM 要用满 3 分钟；差不到 5% 算持平
MIN_GEAR_SHOTS, MIN_GEAR_SECONDS, GEAR_TREND = 50, 180, 0.05


def _gear_trend(now: float, career: float | None) -> str | None:
    if not career or abs(now - career) / career < GEAR_TREND:
        return None
    return "up" if now > career else "down"


def _gear_compare(w: dict[str, Any]) -> list[dict[str, Any]]:
    """这件武器这一局的命中率、KPM 和它这一局之前的生涯比（只有单局详情里有生涯值）。"""

    rows = []
    shots, seconds = w.get("shots") or 0, w.get("seconds") or 0
    if w.get("kind") == "weapon" and shots >= MIN_GEAR_SHOTS and w.get("career_accuracy"):
        now = (w.get("hits") or 0) / shots
        rows.append(
            {
                "label": "命中",
                "value": f"{now:.1%}",
                "career": f"{w['career_accuracy']:.1%}",
                "trend": _gear_trend(now, w["career_accuracy"]),
            }
        )
    if seconds >= MIN_GEAR_SECONDS and w.get("career_kpm"):
        now = w["kills"] / (seconds / 60)
        rows.append(
            {
                "label": "KPM",
                "value": f"{now:.2f}",
                "career": f"{w['career_kpm']:.2f}",
                "trend": _gear_trend(now, w["career_kpm"]),
            }
        )
    return rows


def _round_gear(weapons: list[dict[str, Any]] | None, count: int) -> list[dict[str, Any]]:
    """这一局有击杀的武器载具，按击杀排；战马的击杀和骑马时用的武器重复，不列。"""

    used = [w for w in weapons or [] if w.get("kills") and not w.get("shared_kills")]
    rows = []
    for w in sorted(used, key=lambda w: w["kills"], reverse=True)[:count]:
        rows.append(
            {
                "name": w.get("name") or "-",
                "image": w.get("image") or "",
                "kills": w["kills"],
                "headshots": w.get("headshots") or 0 if w.get("kind") == "weapon" else 0,
                "compare": _gear_compare(w),
            }
        )
    return rows


RECORDS = {
    "max_killstreak": "最高连杀新纪录 {}",
    "longest_headshot": "最远爆头新纪录 {} 米",
    "max_nemesis_streak": "最高宿敌连杀新纪录 {}",
}
ROUND_RIBBONS = 6
RECENT_WIDE = 1760
COLLECTION_WIDTH = 1760
# 和生涯比的门槛，同网页 RoundHonors：在场太短、开火和击杀太少的不比
MIN_COMPARE_SECONDS, MIN_COMPARE_SHOTS, MIN_COMPARE_KILLS = 300, 50, 5


def _growth(p: dict[str, Any]) -> list[tuple[str, bool]]:
    """这一局的成长：升级、兵种升级、新纪录、勋章、服役星、技巧值；第二项是否高亮。"""

    chips = []
    if p.get("rank_to"):
        chips.append((f"升到 {p['rank_to']} 级", True))
    chips += [(f"{CLASSES.get(kit, kit)}升级", True) for kit in p.get("kit_ranks") or []]
    for key, value in (p.get("records") or {}).items():
        chips.append((RECORDS.get(key, key + " {}").format(value), True))
    if p.get("medals"):
        chips.append((f"完成勋章 {p['medals']}", True))
    chips += [(f"{s.get('name') or '-'} ★{s['stars']}", False) for s in p.get("stars") or []]
    if delta := p.get("skill_delta"):
        chips.append((f"技巧值 {delta:+.1f}", False))
    return chips


def _shift(before: float, after: float | None) -> tuple[str, str] | None:
    """这一局让生涯值变了多少（相对变化），和网页一样两位小数；第二项是 up、down 或 flat。"""

    if after is None or before <= 0:
        return None
    change = (after / before - 1) * 100
    if abs(change) < 0.005:
        return "持平", "flat"
    size = "<0.01" if abs(change) < 0.01 else f"{abs(change):.2f}"
    return (f"+{size}%", "up") if change > 0 else (f"−{size}%", "down")


def _compare(p: dict[str, Any]) -> list[dict[str, Any]]:
    """和这一局之前的生涯比：K/D、KPM、SPM、命中率、爆头率。只有单局详情里有生涯数据。"""

    career, after = p.get("career"), p.get("career_after") or {}
    if not career or (p.get("seconds") or 0) < MIN_COMPARE_SECONDS:
        return []
    combat = p.get("combat")
    shots = (combat or {}).get("shots") or 0
    kills = p.get("kills") or 0

    def pct(v: float) -> str:
        return f"{v * 100:.1f}%"

    items = [
        ("K/D", p.get("kd"), "kd", lambda v: f"{v:.2f}"),
        ("KPM", p.get("kpm"), "kpm", lambda v: f"{v:.2f}"),
        # 旧对局的 SPM 含勋带和结算奖励，和生涯口径不同，不比
        (
            "SPM",
            p.get("spm") if p.get("battle_score") is not None else None,
            "spm",
            lambda v: f"{v:.0f}",
        ),
        (
            "命中率",
            combat["hits"] / shots if combat and shots >= MIN_COMPARE_SHOTS else None,
            "accuracy",
            pct,
        ),
        (
            "爆头率",
            (combat.get("headshots") or 0) / kills
            if combat and kills - (p.get("vehicle_kills") or 0) >= MIN_COMPARE_KILLS
            else None,
            "headshot_rate",
            pct,
        ),
    ]
    rows = []
    for label, value, key, fmt in items:
        before = career.get(key)
        if value is None or before is None:
            continue
        shift = _shift(before, after.get(key))
        rows.append(
            {
                "label": label,
                "value": fmt(value),
                "career": fmt(before),
                "shift": shift[0] if shift else None,
                "trend": shift[1] if shift else None,
            }
        )
    return rows


# 得分构成，配色同网页 RoundHonors.ScoreBreakdown：前五项是计分板上的分，后两项是结算时另给的经验
SCORE_PARTS = (
    ("general", "战斗", "rgba(56,189,248,.8)"),
    ("team", "团队", "rgba(52,211,153,.8)"),
    ("squad", "小队", "rgba(167,139,250,.8)"),
    ("objective", "目标", "rgba(251,191,36,.85)"),
    ("other", "其他", "rgba(255,255,255,.35)"),
    ("award", "勋带勋章", "rgba(253,230,138,.4)"),
    ("bonus", "结算奖励", "rgba(255,255,255,.15)"),
)


def _score_parts(p: dict[str, Any]) -> dict[str, Any] | None:
    """一根条加图例；“其他”是总分减各项的零头，可能为负，不是正数的项不画。"""

    parts = p.get("score_parts")
    if not parts:
        return None
    shown = [(label, parts.get(key) or 0, color) for key, label, color in SCORE_PARTS]
    shown = [x for x in shown if x[1] > 0]
    total = sum(value for _, value, _ in shown) or 1
    return {
        "segments": [
            {"label": label, "value": f"{value:,}", "color": color, "pct": value / total * 100}
            for label, value, color in shown
        ],
        "total": _int(p.get("score")),
    }


def _round_ribbons(p: dict[str, Any], site: str) -> list[dict[str, Any]]:
    return ribbon_rows(p.get("ribbons"), site, ROUND_RIBBONS)


def _round_row(item: dict[str, Any], site: str = "") -> dict[str, Any]:
    r, p = item["round"], item["player"]
    outcome = _outcome(p)
    faction = r.get("team1_faction") if p.get("team") == 1 else r.get("team2_faction")
    kit = p.get("kit")
    return {
        "outcome": OUTCOMES[outcome][0],
        "color": OUTCOMES[outcome][1],
        "win": outcome == "win",
        "flag": _flag(faction),
        "faction": faction or "阵营未知",
        "map": r.get("map_display_name") or r.get("map_name") or "未知地图",
        "map_image": r.get("map_image_url"),
        "mode": r.get("mode_display_name") or r.get("game_mode") or "",
        "server": r.get("server_name") or "-",
        "when": datetime.fromtimestamp(r["ended_at"]).strftime("%Y-%m-%d %H:%M"),
        # 写本人在场多久：中途进出的人整局时长没有意义
        "minutes": f"在场 {max(1, round((p.get('seconds') or 0) / 60))} 分钟",
        "kit": "精英兵" if kit == "elite" else CLASSES.get(kit or "", "-"),
        "portrait": _portrait(kit) if kit in CLASSES else None,
        "kd": f"{p['kills']}/{p['deaths']}",
        "ratio": _fixed(p.get("kd")),
        "kpm": _fixed(p.get("kpm")),
        "spm": _fixed(p.get("spm"), 0),
        "suspect": p.get("status") == "suspect",
        "gear": _round_gear(p.get("weapons"), 3),
        "mvp": bool(p.get("mvp")),
        "ace": bool(p.get("ace_squad")),
        "ribbons": _round_ribbons(p, site),
        "more_ribbons": max(
            0, len([x for x in p.get("ribbons") or [] if x.get("count")]) - ROUND_RIBBONS
        ),
        "growth": _growth(p),
        "parts": _score_parts(p),
        "stats": _round_stats(p, kd=f"{p['kills']}/{p['deaths']}"),
    }


def _round_stats(p: dict[str, Any], kd: str) -> list[dict[str, Any]]:
    """计分板几项，能和生涯比的在下面写生涯值和变化；命中率、爆头率只在能比时列。

    得分是计分板口径（不含勋带勋章奖励和结算奖励），较早的对局没有分项，用总分。
    """

    compare = {c["label"]: c for c in _compare(p)}
    score = p["battle_score"] if p.get("battle_score") is not None else p.get("score")
    cells = [
        ("击杀 / 死亡", kd, None),
        ("KD", _fixed(p.get("kd")), compare.pop("K/D", None)),
        ("KPM", _fixed(p.get("kpm")), compare.pop("KPM", None)),
        ("SPM", _fixed(p.get("spm"), 0), compare.pop("SPM", None)),
        ("得分", _int(score), None),
        *((label, c["value"], c) for label, c in compare.items()),
    ]
    return [{"label": label, "value": value, "compare": c} for label, value, c in cells]


def played_rounds(items: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """没击杀也没死过的（排队、观战、进服就走）不算一局。"""

    return [i for i in items if (i["player"].get("kills") or 0) + (i["player"].get("deaths") or 0)][
        :count
    ]


def _with_detail(item: dict[str, Any], details: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """有单局详情就用详情里这个人的那一行：多了和生涯的对比。"""

    detail = details.get(item["round"]["id"])
    pid = item["player"]["persona_id"]
    mine = next((p for p in (detail or {}).get("players") or [] if p["persona_id"] == pid), None)
    return {**item, "player": mine} if mine else item


def recent_page(
    persona: dict[str, Any],
    platoon: dict[str, Any] | None,
    items: list[dict[str, Any]],
    source: str,
    count: int,
    details: dict[int, dict[str, Any]] | None = None,
    site: str = "",
) -> dict[str, Any]:
    """details 是对局 id 到单局详情（/rounds/{id}），拿到的局会和生涯比；site 用来取勋带图。"""

    played = [_with_detail(i, details or {}) for i in played_rounds(items, count)]
    players = [i["player"] for i in played]
    kills = sum(p.get("kills") or 0 for p in players)
    deaths = sum(p.get("deaths") or 0 for p in players)
    seconds = sum(p.get("seconds") or 0 for p in players)
    outcomes = [_outcome(p) for p in players]
    wins, losses = outcomes.count("win"), outcomes.count("loss")
    return {
        **_common(source),
        "name": persona["display_name"],
        "tag": (platoon or {}).get("tag"),
        "avatar_url": persona.get("avatar_url") or "",
        "kpis": [
            ("胜 / 负", f"{wins} / {losses}"),
            ("击杀 / 死亡", f"{kills} / {deaths}"),
            ("KD", _fixed(kills / deaths if deaths else float(kills))),
            ("KPM", _kpm(kills, seconds)),
            ("在场", _duration(seconds)),
        ],
        "rounds": [_round_row(i, site) for i in played],
        # 两局一行，图不至于拉得太长；只有一局时照旧一栏
        "columns": 2 if len(played) > 1 else 1,
        "width": RECENT_WIDE if len(played) > 1 else WIDTH,
    }


def _sparkline(history: list[dict[str, Any]], width: int, height: int) -> dict[str, Any] | None:
    points = [h["players"] for h in history if isinstance(h.get("players"), int)]
    if len(points) < 2:
        return None
    low, high = min(points), max(points)
    span = (high - low) or 1
    step = width / (len(points) - 1)
    line = " ".join(
        f"{i * step:.1f},{height - (v - low) / span * height:.1f}" for i, v in enumerate(points)
    )
    return {
        "line": line,
        "area": f"0,{height} {line} {width},{height}",
        "low": _int(low),
        "high": _int(high),
    }


def overview_page(overview: dict[str, Any], source: str) -> dict[str, Any]:
    regions = [("亚服", "asia"), ("欧服", "eu"), ("其他", "other")]
    kinds = [("私服", "private"), ("官服", "official")]
    players, servers = overview.get("players") or {}, overview.get("servers") or {}
    queues = overview.get("queues") or {}
    top = overview.get("top_map_modes") or []
    most = max((m.get("players") or 0 for m in top), default=0)
    modes = overview.get("mode_distribution") or []
    total = sum(m.get("players") or 0 for m in modes)
    return {
        **_common(source),
        "players": _int(players.get("total")),
        "servers": _int(servers.get("total")),
        "queues": _int(queues.get("total")),
        "spectators": _int((overview.get("spectators") or {}).get("total")),
        "split": [
            (label, _int(players.get(key)), _int(servers.get(key)))
            for label, key in regions + kinds
        ],
        "maps": [
            {
                "label": m.get("label") or "-",
                "image": m.get("image"),
                "players": _int(m.get("players")),
                "servers": m.get("servers") or 0,
                "ratio": _percent(m.get("players") or 0, most),
            }
            for m in top[:6]
        ],
        "modes": [
            (m.get("label") or "-", _int(m.get("players")), _percent(m.get("players") or 0, total))
            for m in modes[:5]
        ],
        "trend": _sparkline(overview.get("history") or [], 900, 150),
    }


# 百科卡片照游戏里的典籍页和 v3：左边配图卡片，右边正文；高度固定为卡片高，正文越长图越宽
WIKI_CARD = 400  # 640×901 的配图缩到这个宽
WIKI_FONT = 19
WIKI_LINE = WIKI_FONT * 1.6
WIKI_PAD = 36
WIKI_GAP = 44


def _ems(text: str, latin: float = 0.56) -> float:
    """按字宽折算成多少个汉字宽。"""

    return sum(1 if ord(c) > 0x2E7F else latin for c in text)


def _lines(em: float, size: float, width: int) -> float:
    # 右边留 30px 给竖线；两端对齐和标点避头尾会挤掉一点
    return -(-em // ((width - 30) / size * 0.97))


def _wiki_text_width(entry: Any, paragraphs: list[str]) -> int:
    """正文区要多宽，右边的字才能和卡片一样高：标题、解锁条件和每段正文分别估行数。"""

    card = WIKI_CARD * 901 / 640
    ems = [_ems(p) for p in paragraphs]
    width = 520
    while width < 2800:
        # 分类、间距固定 72px；标题 38px、解锁条件 19px（前面有个勾选框图标）
        head = 72 + 36 * len(entry.taglines) + 47.5 * _lines(_ems(entry.title, 0.68), 38, width)
        head += 30 * _lines(_ems(entry.unlock) + 1.5, WIKI_FONT, width)
        body = sum(_lines(em, WIKI_FONT, width) for em in ems) * WIKI_LINE
        if head + body + WIKI_FONT * 0.6 * (len(ems) - 1) <= card:
            break
        width += 20
    return width


def wiki_page(entry: Any, total: int) -> dict[str, Any]:
    """百科条目：左边游戏里的典籍配图，右边标题、解锁条件和正文；背景是配图本身虚化。"""

    paragraphs = [p.strip() for p in entry.description.split("\n") if p.strip()]
    text = _wiki_text_width(entry, paragraphs)
    return {
        **_common("游戏内典籍"),
        "background": entry.image,
        "width": WIKI_PAD * 2 + WIKI_CARD + WIKI_GAP + text,
        "card": WIKI_CARD,
        "no": entry.no,
        "total": total,
        "category": entry.category,
        "name": entry.name,
        "title": entry.title,
        "taglines": entry.taglines,
        "unlock": entry.unlock,
        "paragraphs": paragraphs,
        "image": entry.image,
    }


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
            logger.debug("战地一图片有资源没加载完：{!r}", error)
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
        logger.warning("战地一图片渲染失败，回退文字：{!r}", error)
        return None
