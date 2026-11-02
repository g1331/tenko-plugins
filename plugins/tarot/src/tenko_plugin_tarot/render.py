"""把一次占卜排成图片：三张牌阵横排，凯尔特十字按标准布局；渲染不了时给文字版。

这里不碰 Entari：输入是抽到的牌，输出是模板上下文、图片字节或文字。截图流程和随机老婆插件一样。
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from loguru import logger

from . import deck
from .spreads import SPREADS, Draw, Kind

HERE = Path(__file__).parent
WIDTH = 1000
RENDER_TIMEOUT = 40


def label(draw: Draw) -> str:
    """“星星（正位）”。"""

    card_id, reversed = draw
    return f"{deck.cards()[card_id].name}（{deck.orientation(reversed)}）"


def context(
    kind: Kind,
    draws: list[Draw],
    *,
    question: str | None = None,
    user: str | None = None,
    at: float | None = None,
    note: str | None = None,
    images: bool = True,
) -> dict[str, Any]:
    spread = SPREADS[kind]
    cards = []
    for number, (position, (card_id, reversed)) in enumerate(
        zip(spread.positions, draws, strict=True), 1
    ):
        card = deck.cards()[card_id]
        cards.append(
            {
                "no": number,
                "position": position,
                "name": card.name,
                "name_en": card.name_en,
                "reversed": reversed,
                "orientation": deck.orientation(reversed),
                "meaning": card.meaning(reversed),
                "img": _data_uri(card.path) if images else None,
            }
        )
    return {
        "kind": kind,
        "title": spread.name,
        "question": question,
        "user": user,
        "time": datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M") if at else None,
        "note": note,
        "cards": cards,
    }


def text(context: dict[str, Any]) -> str:
    """渲染不了时的文字版。"""

    head = context["title"]
    if context["question"]:
        head += f"「{context['question']}」"
    lines = [head]
    for card in context["cards"]:
        lines.append(
            f"{card['no']}. {card['position']}：{card['name']}（{card['orientation']}）"
            f"——{card['meaning']}"
        )
    if context["note"]:
        lines.append(context["note"])
    return "\n".join(lines)


def _data_uri(path: Path, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


_env = Environment(
    loader=FileSystemLoader(HERE / "templates"), autoescape=True, undefined=StrictUndefined
)


def _host_font(service: Any) -> str | None:
    # 宿主自带思源黑体 GB2312 子集；拿不到时用系统字体。
    path = Path(getattr(service, "template_dir", "")) / "assets/fonts/NotoSansSC-GB2312.woff2"
    if not path.is_file():
        return None
    return _data_uri(path, "font/woff2")


def html(context: dict[str, Any], sc_font: str | None = None) -> str:
    return _env.get_template("spread.html").render(sc_font=sc_font, **context)


async def _screenshot(service: Any, page_html: str) -> bytes:
    # shortcut: 宿主的 render_template 只认宿主自带模板，这里借它的共享浏览器截插件自己的页面；
    # tenko-api 提供渲染任意 HTML 的接口后改用它。
    if not await service.start() or service.browser is None:
        raise RuntimeError("浏览器不可用")
    browser_context = await service.browser.new_context(
        viewport={"width": WIDTH, "height": 100}, device_scale_factor=1
    )
    try:
        page = await browser_context.new_page()
        await page.set_content(page_html, wait_until="load", timeout=15_000)
        return await page.screenshot(full_page=True, type="jpeg", quality=88)
    finally:
        await browser_context.close()


async def image(service: Any, context: dict[str, Any]) -> bytes | None:
    """排好的图片；没有浏览器或渲染失败时是 None，由调用方回退文字。"""

    if service is None:
        return None
    try:
        page_html = html(context, _host_font(service))
        return await asyncio.wait_for(_screenshot(service, page_html), RENDER_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning("塔罗牌阵渲染失败，回退文字：{!r}", error)
        return None
