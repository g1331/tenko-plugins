"""老婆列表：搜索、分页，排成缩略图卡片截成图片。

这里不碰 Entari：输入是老婆库里的文件，输出是模板上下文或图片字节。截图流程和 Apex 插件一样。
"""

from __future__ import annotations

import asyncio
import base64
import math
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from loguru import logger
from PIL import Image, ImageOps

from . import library

HERE = Path(__file__).parent
WIDTH = 1000
PER_PAGE = 30
THUMB = (300, 400)
"""缩略图尺寸，卡片按 3:4 显示，截图时缩到一半，高清屏上也清楚。"""
RENDER_TIMEOUT = 40


def size_text(size: int) -> str:
    if size < 1024 * 1024:
        return f"{max(1, round(size / 1024))} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def search(wives: dict[str, Path], keyword: str | None) -> list[tuple[str, Path]]:
    if not keyword:
        return list(wives.items())
    word = keyword.casefold()
    found = [(name, path) for name, path in wives.items() if word in name.casefold()]
    # 名字完全一样的排最前：搜 02 时，叫 02 的那位不会被别的名字挤到后面
    return sorted(found, key=lambda item: item[0].casefold() != word)


def paginate(items: list, page: int) -> tuple[list, int, int]:
    """返回 (这一页的条目, 实际页码, 总页数)；页码超出范围时取最近的一页。"""

    pages = max(1, math.ceil(len(items) / PER_PAGE))
    page = min(max(page, 1), pages)
    return items[(page - 1) * PER_PAGE : page * PER_PAGE], page, pages


def _thumbnail(path: Path) -> bytes:
    """按文件名、大小和修改时间缓存，换了图会重新生成。"""

    stat = path.stat()
    cached = library.folder("thumbs") / f"{path.name}.{stat.st_size}.{int(stat.st_mtime)}.jpg"
    if cached.is_file():
        return cached.read_bytes()
    with Image.open(path) as image:
        image.seek(0)  # 动图取第一帧
        thumb = ImageOps.fit(
            image.convert("RGB"), THUMB, Image.Resampling.LANCZOS, centering=(0.5, 0.3)
        )
    buffer = BytesIO()
    thumb.save(buffer, "JPEG", quality=82)
    for stale in cached.parent.glob(f"{path.name}.*.jpg"):
        stale.unlink(missing_ok=True)
    cached.write_bytes(buffer.getvalue())
    return buffer.getvalue()


def _thumb_uri(path: Path) -> str | None:
    try:
        data = _thumbnail(path)
    except Exception as error:  # 坏图：卡片照样显示名字
        logger.warning("老婆 {} 的缩略图生成失败：{!r}", path.name, error)
        return None
    return "data:image/jpeg;base64," + base64.b64encode(data).decode()


def list_page(
    wives: dict[str, Path], keyword: str | None, page: int, *, thumbs: bool = True
) -> dict[str, Any]:
    found = search(wives, keyword)
    items, page, pages = paginate(found, page)
    cards = []
    for index, (name, path) in enumerate(items, (page - 1) * PER_PAGE + 1):
        size = path.stat().st_size
        cards.append(
            {
                "no": index,
                "name": name,
                "size": size_text(size),
                "format": library.mime(path.read_bytes()[:16], path).split("/")[1].upper(),
                "thumb": _thumb_uri(path) if thumbs else None,
            }
        )
    return {
        "keyword": keyword,
        "total": len(wives),
        "total_size": size_text(sum(p.stat().st_size for p in wives.values())),
        "found": len(found),
        "page": page,
        "pages": pages,
        "cards": cards,
        "now": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def format_list(context: dict[str, Any]) -> str:
    """渲染不了时的文字版。"""

    keyword, cards = context["keyword"], context["cards"]
    if not cards:
        return f"老婆库里没有名字带“{keyword}”的" if keyword else "老婆库还是空的"
    head = (
        f"名字带“{keyword}”的有 {context['found']} 位"
        if keyword
        else (f"老婆库里一共 {context['total']} 位")
    )
    lines = [f"{head}（第 {context['page']}/{context['pages']} 页）"]
    lines += [f"{c['no']}. {c['name']}  {c['size']}" for c in cards]
    if context["pages"] > 1:
        lines.append("翻页：/老婆列表 [关键词] -p <页码>")
    return "\n".join(lines)


_env = Environment(
    loader=FileSystemLoader(HERE / "templates"), autoescape=True, undefined=StrictUndefined
)


def _host_font(service: Any) -> str | None:
    # 宿主自带思源黑体 GB2312 子集；拿不到时用系统字体。
    path = Path(getattr(service, "template_dir", "")) / "assets/fonts/NotoSansSC-GB2312.woff2"
    if not path.is_file():
        return None
    return "data:font/woff2;base64," + base64.b64encode(path.read_bytes()).decode()


def html(context: dict[str, Any], sc_font: str | None = None) -> str:
    return _env.get_template("list.html").render(sc_font=sc_font, **context)


async def _screenshot(service: Any, page_html: str) -> bytes:
    # shortcut: 宿主的 render_template 只认宿主自带模板，这里借它的共享浏览器截插件自己的页面；
    # tenko-api 提供渲染任意 HTML 的接口后改用它。
    if not await service.start() or service.browser is None:
        raise RuntimeError("浏览器不可用")
    context = await service.browser.new_context(
        viewport={"width": WIDTH, "height": 100}, device_scale_factor=1
    )
    try:
        page = await context.new_page()
        await page.set_content(page_html, wait_until="load", timeout=15_000)
        return await page.screenshot(full_page=True, type="jpeg", quality=85)
    finally:
        await context.close()


async def render(service: Any, wives: dict[str, Path], keyword: str | None, page: int):
    """返回 (图片字节或 None, 模板上下文)；渲染失败时图片为 None，由调用方回退文字。"""

    if service is None:
        return None, list_page(wives, keyword, page, thumbs=False)
    context = await asyncio.to_thread(list_page, wives, keyword, page)
    if not context["cards"]:
        return None, context
    try:
        page_html = html(context, _host_font(service))
        image = await asyncio.wait_for(_screenshot(service, page_html), RENDER_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning("老婆列表渲染失败，回退文字：{!r}", error)
        return None, context
    return image, context
