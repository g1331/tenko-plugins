"""回复的通用部分：出图，渲染不了回文字；查询错误原样回给群友。"""

from __future__ import annotations

from collections.abc import Awaitable

from arclet.entari import MessageChain
from satori import Element, Image, Text

from . import pages
from .api import Bf1Error


async def render(
    service,
    template: str,
    context,
    fallback: str,
    link: str | None = None,
    caption: str | None = None,
) -> MessageChain | str:
    """出图时 caption 跟在图后面，方便复制；回退文字时 fallback 里已经有了，不再重复。"""

    image = await pages.render(service, template, context)
    if not image:
        return f"{fallback}\n{link}" if link else fallback
    chain = MessageChain(Image.of(raw=image, mime="image/jpeg"))
    after = "\n".join(t for t in (caption, link) if t)
    return chain + Text(after) if after else chain


async def answer(query: Awaitable[MessageChain | str]) -> MessageChain | str:
    try:
        return await query
    except Bf1Error as error:
        return str(error)


def as_elements(reply: MessageChain | str | None) -> list[Element]:
    """拼进更长的消息里用。"""

    if reply is None:
        return []
    return [Text(reply)] if isinstance(reply, str) else list(reply)
