"""把接口返回的数据整理成模板上下文，并截成图片。

这里不碰 Entari：输入是接口返回的数据，输出是模板上下文或图片字节。截图流程和战地一插件一样。
接口只给英文，显示前在这里译成中文。
"""

from __future__ import annotations

import asyncio
import base64
import html as html_lib
import mimetypes
import re
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any

import zhconv
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from loguru import logger

HERE = Path(__file__).parent
ASSETS = HERE / "assets"
WIDTH = 1000
RENDER_TIMEOUT = 40
SUMMARY = 80
EA_NEWS = "https://www.ea.com/zh-hant/games/apex-legends/apex-legends/news"

# shortcut: 名字以 B 站 Apex 百科（wiki.biligame.com/apex）为准，百科没收的对照多个国内 Apex 机器人
# 的译名表取多数；没收录的原样显示，新英雄、新地图上线时补一行。
CN = {
    # 地图
    "Kings Canyon": "诸王峡谷",
    "World's Edge": "世界尽头",
    "Olympus": "奥林匹斯",
    "Storm Point": "风暴点",
    "Broken Moon": "破碎月亮",
    "E-District": "电力区",
    "Skulltown": "骷髅镇",
    "Thunderdome": "雷霆堡",
    "Estates": "不动产",
    "Habitat": "栖息地",
    "Habitat 4": "栖息地4",
    "Fragment": "碎片东部",
    "Fragment East": "碎片东部",
    "Monument": "纪念碑",
    "Drop-Off": "原料场",
    "Zeus Station": "宙斯站",
    "Lava Siphon": "岩浆汲取器",
    "Barometer": "气压计",
    "Party Crasher": "派对破坏者",
    "Phase Runner": "相位穿梭器",
    "Overflow": "熔岩流",
    "Encore": "再来一次",
    "Hammond Labs": "哈蒙德实验室",
    "Production Yard": "生产场地",
    "Wattson's Pylon": "沃特森的塔",
    "Siphon": "岩浆汲取器",
    # 模式和活动
    "Control": "控制",
    "Gun Run": "子弹时间",
    "Team Deathmatch": "团队死斗",
    "Mixtape": "混合模式",
    "Arenas": "竞技场",
    "Lockdown": "封锁",
    "Wildcard": "外卡",
    "Revival": "复活",
    "Ranked": "排位赛",
    "Battle Royale": "大逃杀",
    "Duos": "双人赛",
    "Trios": "三人赛",
    "Quads": "四人赛",
    "Solos": "单人赛",
    # 段位
    "Rookie": "菜鸟",
    "Bronze": "青铜",
    "Silver": "白银",
    "Gold": "黄金",
    "Platinum": "白金",
    "Diamond": "钻石",
    "Master": "大师",
    "Apex Predator": "猎杀者",
    "Unranked": "未定级",
    # 英雄
    "Bloodhound": "寻血猎犬",
    "Gibraltar": "直布罗陀",
    "Lifeline": "命脉",
    "Pathfinder": "探路者",
    "Wraith": "恶灵",
    "Bangalore": "班加罗尔",
    "Caustic": "侵蚀",
    "Mirage": "幻象",
    "Octane": "动力小子",
    "Wattson": "沃特森",
    "Crypto": "密客",
    "Revenant": "亡灵",
    "Loba": "萝芭",
    "Rampart": "兰伯特",
    "Horizon": "地平线",
    "Fuse": "暴雷",
    "Valkyrie": "瓦尔基里",
    "Seer": "希尔",
    "Ash": "艾许",
    "Mad Maggie": "疯玛吉",
    "Newcastle": "纽卡斯尔",
    "Vantage": "万蒂奇",
    "Catalyst": "卡特莉丝",
    "Ballistic": "弹道",
    "Conduit": "导线管",
    "Alter": "变幻",
    "Sparrow": "琉雀",
    "Axle": "艾克赛尔",
}
MODES = (("battle_royale", "匹配"), ("ranked", "排位"), ("ltm", "限时模式"))
PLATFORMS = (("PC", "PC"), ("PS4", "PlayStation"), ("X1", "Xbox"), ("SWITCH", "Switch"))
PLATFORM_NAMES = dict(PLATFORMS)
LEGENDS = 6
# 各小段起点分，由 EA 帮助页（help.ea.com Ranked）每段所需分数和分段重置落点推出，第 30 赛季适用。
# shortcut: 第 32 赛季要加黑铁、翡翠、宗师段位，届时按新表更新；认不出的段位不画进度条。
RANK_FLOORS = (
    ("Rookie", 4, 0),
    ("Rookie", 3, 250),
    ("Rookie", 2, 500),
    ("Rookie", 1, 750),
    ("Bronze", 4, 1000),
    ("Bronze", 3, 1500),
    ("Bronze", 2, 2000),
    ("Bronze", 1, 2500),
    ("Silver", 4, 3000),
    ("Silver", 3, 3500),
    ("Silver", 2, 4000),
    ("Silver", 1, 4500),
    ("Gold", 4, 5500),
    ("Gold", 3, 6250),
    ("Gold", 2, 7000),
    ("Gold", 1, 7750),
    ("Platinum", 4, 8500),
    ("Platinum", 3, 9250),
    ("Platinum", 2, 10000),
    ("Platinum", 1, 11000),
    ("Diamond", 4, 12000),
    ("Diamond", 3, 13000),
    ("Diamond", 2, 14000),
    ("Diamond", 1, 15000),
    ("Master", 0, 16000),
)
CORE_STATS = (
    ("kd", "KD"),
    ("kills", "击杀"),
    ("wins", "胜场"),
    ("damage", "伤害"),
    ("top_3", "前三"),
)
TREND_SEASONS = 10
STATES = {"offline": "离线", "online": "在线", "in lobby": "大厅中", "in match": "比赛中"}
TOTALS = 15
# 追踪器按 key 翻译（名字会撞：活动击杀的名字也叫 "BR Kills"）。顺序即显示顺序。
# 英雄技能类追踪器（烟雾、跳板距离之类）各不相同，不放进汇总。
TRACKER_KEYS = {
    "kills": "击杀",
    "wins": "胜场",
    "damage": "伤害",
    "top_3": "前三",
    "top_5": "前五",
    "games_played": "场次",
    "headshots": "爆头",
    "revives": "救援",
    "finishers": "终结技",
    "kills_as_kill_leader": "击杀王时击杀",
    "winning_kills": "决胜击杀",
    "kills_20": "20 杀局数",
    "damage_4k": "4000 伤害局数",
}
WEAPONS = {
    "ar": "突击步枪",
    "smg": "冲锋枪",
    "lmg": "轻机枪",
    "shotgun": "霰弹枪",
    "sniper": "狙击枪",
    "marksman": "精确射手武器",
    "pistol": "手枪",
}
SERVER_SECTIONS = (
    ("Origin_login", "EA 登录"),
    ("EA_novafusion", "EA 后端"),
    ("EA_accounts", "EA 账户"),
    ("ApexOauth_Crossplay", "跨平台认证"),
)
SERVER_REGIONS = (
    ("Asia", "亚洲"),
    ("EU-West", "欧洲西部"),
    ("EU-East", "欧洲东部"),
    ("US-West", "美国西部"),
    ("US-Central", "美国中部"),
    ("US-East", "美国东部"),
    ("SouthAmerica", "南美"),
)
OTHER_PLATFORMS = (("Playstation-Network", "PlayStation 网络"), ("Xbox-Live", "Xbox Live"))
SERVER_STATUS = {"UP": ("正常", "up"), "SLOW": ("缓慢", "slow"), "DOWN": ("中断", "down")}
BAN_REASONS = {
    "COMPETITIVE_ABANDON": "中途退出排位",
    "COMPETITIVE_DODGE_COOLDOWN": "躲避匹配冷却",
    "COMPETITIVE_LEAVER": "中途退出排位",
}

_env = Environment(
    loader=FileSystemLoader(HERE / "templates"),
    autoescape=True,
    undefined=StrictUndefined,
)


@cache
def _data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


_env.globals["asset"] = lambda name: _data_uri(ASSETS / name)


def cn(name: str | None) -> str:
    return CN.get(name or "", name or "-")


def _int(value: Any) -> str:
    return f"{value:,}" if isinstance(value, int) else "-" if value is None else str(value)


def _common() -> dict[str, Any]:
    return {"now": datetime.now().strftime("%Y-%m-%d %H:%M")}


def tier(rank: dict[str, Any]) -> str:
    name = cn(rank.get("rankName"))
    # 大师和猎杀者没有小段
    if rank.get("rankDiv") and rank.get("rankName") not in ("Master", "Apex Predator"):
        name += f" {rank['rankDiv']}"
    return name


def state(text: str | None) -> str:
    """ "In match (05:12)" 这类带时长的状态，只译前面的词。"""

    if not text:
        return "-"
    head, _, rest = text.partition("(")
    word = STATES.get(head.strip().casefold())
    return f"{word} ({rest}" if word and rest else word or text


def tracker(key: str) -> tuple[tuple[int, int], str] | None:
    """key → (排序依据, 中文名)；认不出的返回 None。"""

    group, prefix = 0, ""
    if season := re.fullmatch(r"(.+)_season_(\d+)", key):
        # 赛季数据新的在前
        key, prefix, group = season[1], f"第 {season[2]} 赛季", 1000 - int(season[2])
    elif key.startswith("specialEvent_"):
        key, prefix, group = key.removeprefix("specialEvent_"), "活动", 1
    elif key.startswith("arenas_"):
        key, prefix, group = key.removeprefix("arenas_"), "竞技场", 2
    base = list(TRACKER_KEYS)
    if key in TRACKER_KEYS:
        return (group, base.index(key)), prefix + TRACKER_KEYS[key]
    weapon = re.fullmatch(r"(\w+?)_kills", key)
    if weapon and weapon[1] in WEAPONS:
        return (group, len(base)), prefix + WEAPONS[weapon[1]] + "击杀"
    return None


def _labelled(items: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    rows = [(found, value) for key, value in items if (found := tracker(key)) and value is not None]
    return [(label, value) for (_, label), value in sorted(rows, key=lambda r: r[0][0])]


def trackers(data: dict[str, Any]) -> list[tuple[str, Any]]:
    """当前横幅上装的追踪器；没装的拿不到数据。"""

    selected = (data.get("legends") or {}).get("selected") or {}
    return _labelled([(t.get("key") or "", t.get("value")) for t in selected.get("data") or []])


def totals(data: dict[str, Any], count: int = TOTALS) -> list[tuple[str, Any]]:
    """全英雄汇总（同样只算装过的追踪器），KD 放最前；接口没给汇总时退回当前横幅。"""

    total = data.get("total") or {}
    rows = _labelled([(key, t.get("value")) for key, t in total.items() if isinstance(t, dict)])
    try:
        kd = float((total.get("kd") or {}).get("value"))
    except (TypeError, ValueError):
        kd = -1
    if kd > 0:  # 没有死亡数据时接口给 -1
        rows.insert(0, ("KD", f"{kd:.2f}"))
    return (rows or trackers(data))[:count]


def legend_ranking(data: dict[str, Any], count: int = LEGENDS) -> list[dict[str, Any]]:
    """按击杀排英雄；只有装过击杀追踪器的英雄才有数。"""

    rows = []
    for name, legend in ((data.get("legends") or {}).get("all") or {}).items():
        kills = next((t for t in legend.get("data") or [] if t.get("key") == "kills"), None)
        if name == "Global" or not kills or not isinstance(kills.get("value"), int):
            continue
        if kills["value"] <= 0:
            continue
        top = (kills.get("rank") or {}).get("topPercent")
        rows.append(
            {
                "name": cn(name),
                "icon": (legend.get("ImgAssets") or {}).get("icon") or "",
                "kills": kills["value"],
                "top": f"全球前 {top}%" if isinstance(top, int | float) else "",
            }
        )
    rows.sort(key=lambda r: r["kills"], reverse=True)
    most = rows[0]["kills"] if rows else 0
    return [
        {**r, "kills": _int(r["kills"]), "ratio": round(r["kills"] / most * 100)}
        for r in rows[:count]
    ]


def rank_position(rank: dict[str, Any]) -> str:
    parts = []
    position = rank.get("ladderPosPlatform")
    if isinstance(position, int) and position > 0:
        parts.append(f"平台第 {position:,} 名")
    top = rank.get("ALStopPercentGlobal")
    if isinstance(top, int | float):  # 本赛季没打时是一句英文说明
        parts.append(f"全球前 {top}%")
    return " · ".join(parts)


def party(realtime: dict[str, Any]) -> str:
    if not realtime.get("isOnline"):
        return ""
    if realtime.get("partyFull"):
        return "队伍已满"
    return "队伍开放加入" if realtime.get("lobbyState") == "open" else ""


def ban(g: dict[str, Any]) -> str:
    """接口里的 bans 是排位禁赛（逃跑等惩罚），不是封号。"""

    bans = g.get("bans") or {}
    if not bans.get("isActive"):
        return ""
    reason = bans.get("last_banReason")
    text = BAN_REASONS.get(reason or "", reason or "原因未知")
    seconds = bans.get("remainingSeconds")
    return (
        f"{text}，还剩 {-(-seconds // 60)} 分钟"
        if isinstance(seconds, int) and seconds > 0
        else text
    )


def rank_progress(rank: dict[str, Any]) -> dict[str, Any] | None:
    """当前小段内的进度和距下一段的分数；大师以上、未定级或段位表认不出时返回 None。"""

    name, score = rank.get("rankName"), rank.get("rankScore")
    division = rank.get("rankDiv") if name != "Master" else 0
    index = next(
        (i for i, (n, d, _) in enumerate(RANK_FLOORS) if n == name and d == division), None
    )
    if index is None or index + 1 >= len(RANK_FLOORS) or not isinstance(score, int):
        return None
    floor, (next_name, next_div, top) = RANK_FLOORS[index][2], RANK_FLOORS[index + 1]
    span = top - floor
    return {
        "next": cn(next_name) + (f" {next_div}" if next_div else ""),
        "need": _int(max(top - score, 0)),
        "pct": max(0, min(100, round((score - floor) / span * 100))),
    }


def ago(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{max(minutes, 1)} 分钟前"
    if minutes < 60 * 24:
        return f"{minutes // 60} 小时前"
    return f"{minutes // (60 * 24)} 天前"


def delta(current: Any, previous: tuple[int, float] | None, now: float) -> dict[str, str] | None:
    """和上次记下的数比；没有记录或不是整数时返回 None。"""

    if previous is None or not isinstance(current, int):
        return None
    diff = current - previous[0]
    return {
        "text": f"{diff:+,}" if diff else "持平",
        "css": "up" if diff > 0 else "down" if diff < 0 else "same",
        "ago": ago(now - previous[1]),
    }


def core_stats(data: dict[str, Any]) -> list[tuple[str, str]]:
    """生涯总数里最常看的几项；没装追踪器的不显示。"""

    total = data.get("total") or {}
    rows = []
    for key, label in CORE_STATS:
        value = (total.get(key) or {}).get("value")
        if key == "kd":
            try:
                kd = float(value)
            except (TypeError, ValueError):
                continue
            if kd > 0:  # 没有死亡数据时接口给 -1
                rows.append((label, f"{kd:.2f}"))
        elif isinstance(value, int):
            rows.append((label, _int(value)))
    return rows


def season_trend(data: dict[str, Any]) -> dict[str, Any] | None:
    """各赛季击杀（没有就用胜场）画柱状图，至少要两个赛季。"""

    total = data.get("total") or {}
    for stat, label in (("kills", "击杀"), ("wins", "胜场")):
        seasons = sorted(
            (int(found[1]), t["value"])
            for key, t in total.items()
            if (found := re.fullmatch(rf"{stat}_season_(\d+)", key))
            and isinstance(t, dict)
            and isinstance(t.get("value"), int)
        )[-TREND_SEASONS:]
        if len(seasons) >= 2:
            most = max(v for _, v in seasons) or 1
            return {
                "label": label,
                "bars": [
                    {"season": f"S{n}", "value": _int(v), "pct": max(2, round(v / most * 100))}
                    for n, v in seasons
                ],
            }
    return None


def player_page(
    data: dict[str, Any], previous: tuple[int, float] | None = None, now: float = 0
) -> dict[str, Any]:
    g = data["global"]
    rank = g.get("rank") or {}
    realtime = data.get("realtime") or {}
    selected = (data.get("legends") or {}).get("selected") or {}
    images = selected.get("ImgAssets") or {}
    return {
        **_common(),
        "name": g.get("name") or "-",
        "platform": PLATFORM_NAMES.get(g.get("platform") or "", g.get("platform") or "-"),
        "level": _int(g.get("level")),
        "prestige": g.get("levelPrestige") or 0,
        "progress": g.get("toNextLevelPercent") or 0,
        "tier": tier(rank),
        "rank_score": _int(rank.get("rankScore")),
        "rank_image": rank.get("rankImg") or "",
        "rank_position": rank_position(rank),
        "party": party(realtime),
        "online": bool(realtime.get("isOnline")),
        "state": state(realtime.get("currentStateAsText")),
        "legend": cn(selected.get("LegendName") or realtime.get("selectedLegend")),
        "legend_icon": images.get("icon") or "",
        "banner": images.get("banner") or "",
        "rank_progress": rank_progress(rank),
        "rank_delta": delta(rank.get("rankScore"), previous, now),
        "core": core_stats(data),
        "trend": season_trend(data),
        "legends": legend_ranking(data),
        "ban": ban(g),
    }


def _slot(slot: dict[str, Any]) -> dict[str, Any]:
    total, left = slot.get("DurationInSecs"), slot.get("remainingSecs")
    return {
        "map": cn(slot.get("map")),
        "image": slot.get("asset") or "",
        "remaining": slot.get("remainingTimer") or "",
        "elapsed": round((1 - left / total) * 100) if total and left is not None else 0,
    }


def _clock(timestamp: Any) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%H:%M") if isinstance(timestamp, int) else ""


def rotation_page(data: dict[str, Any]) -> dict[str, Any]:
    modes = []
    for key, label in MODES:
        mode = data.get(key) or {}
        if not mode.get("current"):
            continue
        current, upcoming = mode["current"], mode.get("next") or {}
        modes.append(
            {
                "label": label,
                "event": cn(current["eventName"])
                if key == "ltm" and current.get("eventName")
                else "",
                **_slot(current),
                "next": cn(upcoming.get("map")) if upcoming.get("map") else "",
                "next_at": _clock(upcoming.get("start")),
            }
        )
    return {**_common(), "modes": modes}


def predator_page(
    data: dict[str, Any],
    previous: dict[str, tuple[int, float]] | None = None,
    now: float = 0,
) -> dict[str, Any]:
    rp = data.get("RP") or {}
    rows = [(code, label, rp[code]) for code, label in PLATFORMS if rp.get(code)]
    master = RANK_FLOORS[-1][2]
    most = max((e.get("totalMastersAndPreds") or 0 for _, _, e in rows), default=0) or 1
    # 段位线左端是大师门槛，右端是最高的那条猎杀线
    top = max((e["val"] for _, _, e in rows if isinstance(e.get("val"), int)), default=master)
    return {
        **_common(),
        "master": _int(master),
        "platforms": [
            {
                "code": code,
                "label": label,
                "score": _int(entry.get("val")),
                # 大师不满 750 人的平台，猎杀线会低于大师门槛
                "above": _int(entry["val"] - master)
                if isinstance(entry.get("val"), int) and entry["val"] > master
                else "",
                "delta": delta(entry.get("val"), (previous or {}).get(code), now),
                "masters": _int(entry.get("totalMastersAndPreds")),
                # 没到大师门槛的平台不画在线上
                "pos": round((entry["val"] - master) / (top - master) * 100, 1)
                if isinstance(entry.get("val"), int) and entry["val"] > master
                else None,
                "masters_pct": round((entry.get("totalMastersAndPreds") or 0) / most * 100),
                "updated": _clock(entry.get("updateTimestamp")),
            }
            for code, label, entry in rows
        ],
    }


def _server(entry: Any) -> dict[str, Any]:
    entry = entry if isinstance(entry, dict) else {}
    label, css = SERVER_STATUS.get(entry.get("Status") or "", ("未知", "unknown"))
    ms = entry.get("ResponseTime")
    return {"status": label, "css": css, "ms": f"{ms} ms" if isinstance(ms, int) and ms > 0 else ""}


def servers_page(data: dict[str, Any]) -> dict[str, Any]:
    sections = [
        {
            "label": label,
            "cells": [_server((data.get(key) or {}).get(region)) for region, _ in SERVER_REGIONS],
        }
        for key, label in SERVER_SECTIONS
    ]
    platforms = [
        {"label": label, **_server((data.get("otherPlatforms") or {}).get(key))}
        for key, label in OTHER_PLATFORMS
    ]
    cells = [c for s in sections for c in s["cells"]] + platforms
    bad = sum(c["css"] in ("slow", "down") for c in cells)
    return {
        **_common(),
        "regions": [label for _, label in SERVER_REGIONS],
        "sections": sections,
        "platforms": platforms,
        "summary": f"{bad} 项异常" if bad else "全部正常",
        "healthy": not bad,
    }


def simplified(text: str) -> str:
    """EA 只有繁体中文版，转成简体。"""

    return zhconv.convert(html_lib.unescape(text).strip(), "zh-cn")


def parse_news(page: str) -> list[dict[str, str]]:
    """从 EA 繁中新闻页解析文章卡片，页面里最新的在前。

    shortcut: 页面没有公开接口，只能按链接、图片、日期、标题的结构抓；EA 改版后要跟着改。
    """

    items, seen = [], set()
    for path, body in re.findall(
        r'<a href="(/zh-hant/games/apex-legends/apex-legends/news/[^"#?]+)"[^>]*>(.*?)</a>',
        page,
        re.S,
    ):
        title = re.search(r"<h3[^>]*>(?:\s*<span[^>]*>)?([^<]+)", body)
        if not title or path in seen:
            continue
        seen.add(path)
        date = re.search(r">(\d{4})年(\d{1,2})月(\d{1,2})日<", body)
        cover = re.search(r'<img[^>]*src="([^"]+)"', body)
        items.append(
            {
                "title": simplified(title.group(1)),
                "date": "-".join(f"{int(n):02d}" for n in date.groups()) if date else "",
                "cover": html_lib.unescape(cover.group(1)) if cover else "",
                "url": f"https://www.ea.com{path}",
            }
        )
    return items


def description(page: str) -> str:
    """文章页 meta 里的简介。"""

    found = re.search(r'<meta content="([^"]*)" name="description"', page) or re.search(
        r'<meta name="description" content="([^"]*)"', page
    )
    text = simplified(found.group(1)) if found else ""
    return text if len(text) <= SUMMARY else text[:SUMMARY].rstrip() + "…"


def news_page(items: list[dict[str, str]]) -> dict[str, Any]:
    return {**_common(), "items": items}


def _host_font(service: Any) -> str | None:
    # 宿主自带思源黑体 GB2312 子集；拿不到时用系统字体。
    path = Path(getattr(service, "template_dir", "")) / "assets/fonts/NotoSansSC-GB2312.woff2"
    return _data_uri(path) if path.is_file() else None


def html(template: str, context: dict[str, Any], sc_font: str | None = None) -> str:
    return _env.get_template(template).render(sc_font=sc_font, **context)


async def _screenshot(service: Any, page_html: str) -> bytes:
    # shortcut: 宿主的 render_template 只认宿主自带模板，这里借它的共享浏览器截插件自己的页面；
    # tenko-api 提供渲染任意 HTML 的接口后改用它。
    if not await service.start() or service.browser is None:
        raise RuntimeError("浏览器不可用")
    context = await service.browser.new_context(
        # 整页截图不会比视口矮，视口给小一点，内容短时才不会在下面留一截黑底。
        viewport={"width": WIDTH, "height": 100},
        device_scale_factor=1,
    )
    try:
        page = await context.new_page()
        try:
            await page.set_content(page_html, wait_until="load", timeout=15_000)
        except Exception as error:  # 个别远程图片拉不到时照样截
            logger.debug("Apex 图片有资源没加载完：{!r}", error)
        return await page.screenshot(full_page=True, type="jpeg", quality=80)
    finally:
        await context.close()


async def render(service: Any, template: str, context: dict[str, Any]) -> bytes | None:
    """渲染失败返回 None，由调用方回退文字。"""

    if service is None:
        return None
    try:
        page_html = html(template, context, _host_font(service))
        return await asyncio.wait_for(_screenshot(service, page_html), RENDER_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning("Apex 图片渲染失败，回退文字：{!r}", error)
        return None
