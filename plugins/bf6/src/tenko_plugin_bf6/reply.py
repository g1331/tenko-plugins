"""回复的通用部分：出图，渲染不了回文字；查询错误原样回给群友。"""

from __future__ import annotations

from collections.abc import Awaitable

from arclet.entari import MessageChain
from satori import Image, Text

from . import pages
from .api import Bf6Error


async def render(
    service, template: str, context, fallback: str, link: str | None = None
) -> MessageChain | str:
    """出图时网页链接跟在图后面；回退文字时附在文字末尾。"""

    image = await pages.render(service, template, context)
    if not image:
        return f"{fallback}\n{link}" if link else fallback
    chain = MessageChain(Image.of(raw=image, mime="image/jpeg"))
    return chain + Text(link) if link else chain


async def answer(query: Awaitable[MessageChain | str]) -> MessageChain | str:
    try:
        return await query
    except Bf6Error as error:
        return str(error)
