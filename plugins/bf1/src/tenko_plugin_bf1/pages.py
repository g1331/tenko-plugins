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


def stats_page(
    persona: dict[str, Any],
    stats: dict[str, Any],
    weapons: list[dict[str, Any]],
    vehicles: list[dict[str, Any]],
    ban: dict[str, Any] | None,
    platoon: dict[str, Any] | None,
    online: dict[str, Any] | None,
    source: str,
) -> dict[str, Any]:
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
    ban = ban or {}
    bans = [
        {"source": "BFBAN", "logo": asset("bfban.webp"), "hit_text": "实锤"},
        {"source": "BFEAC", "logo": asset("bfeac.webp"), "hit_text": "已封禁"},
    ]
    for b in bans:
        state = ban.get(b["source"].lower())
        b["state"] = state if state in ("hit", "clean") else "unknown"
    banned_by = " / ".join(b["source"] for b in bans if b["state"] == "hit")

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
        "platoon": platoon,
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
        "gear": [("常用武器", _gear(weapons, 5)), ("常用载具", _gear(vehicles, 3))],
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


def items_page(
    kind: str,
    persona: dict[str, Any],
    platoon: dict[str, Any] | None,
    items: list[dict[str, Any]],
    source: str,
    count: int,
) -> dict[str, Any]:
    weapon = kind == "weapons"
    rows = []
    for item in top_kills(items, count):
        stars = item["kills"] // 100
        rows.append(
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
    return {
        **_common(source),
        "title": "武器" if weapon else "载具",
        "name": persona["display_name"],
        "tag": (platoon or {}).get("tag"),
        "avatar_url": persona.get("avatar_url") or "",
        "columns": ["击杀", "KPM", "命中率", "爆头率", "时长"]
        if weapon
        else ["击杀", "KPM", "摧毁", "时长"],
        "items": rows,
    }


def servers_page(
    keyword: str, servers: list[dict[str, Any]], total: int, source: str
) -> dict[str, Any]:
    rows = []
    for s in servers:
        players, most = s.get("player_count") or 0, s.get("max_player_count") or 0
        rows.append(
            {
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
        )
    shown = f"，按人数显示前 {len(rows)} 个" if total > len(rows) else ""
    return {
        **_common(source),
        "keyword": keyword,
        "summary": f"共找到 {total} 个有人的服务器{shown}",
        "servers": rows,
    }


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
        viewport={"width": WIDTH, "height": 800}, device_scale_factor=1
    )
    try:
        page = await context.new_page()
        try:
            await page.set_content(page_html, wait_until="load", timeout=15_000)
        except Exception as error:  # 个别远程图片拉不到时照样截，缺图的位置会隐藏
            logger.debug("战地一图片有资源没加载完：{!r}", error)
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
        logger.warning("战地一图片渲染失败，回退文字：{!r}", error)
        return None
