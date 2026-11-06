"""百科和小贴士：游戏里的典籍和载入画面小贴士，群里有人戳 bot 时随机发一条。"""

from __future__ import annotations

import random
import time
from typing import Any

from arclet.entari import MessageChain
from arclet.entari.event.base import InternalEvent
from satori import At, Element, Text
from tenko_api.accounts import Account, accounts_for_group
from tenko_api.features import feature_enabled

from . import codex, pages
from .api import Bf1Error
from .reply import as_elements, render

WIKI_LIST = 20
TIP_LIST = 5
POKE_COOLDOWN = 10
_WIKI_HINT = "可以搜武器、载具、地名、战役、人物，或者发分类名：" + "、".join(codex.CATEGORIES)


def format_entry(entry: codex.Entry) -> str:
    lines = [f"#{entry.no} {entry.title}（{entry.category}）", *entry.taglines]
    return "\n".join([*lines, f"解锁：{entry.unlock}", entry.description])


def _wiki_line(entry: codex.Entry) -> str:
    tagline = f" · {entry.taglines[0]}" if entry.taglines else ""
    return f"#{entry.no} {entry.title}{tagline}（{entry.category}）"


def format_tip(tip: codex.Tip) -> str:
    return f"【{'·'.join(tip.tags)}】{tip.text}" if tip.tags else tip.text


async def _wiki_card(entry: codex.Entry, service) -> MessageChain | str:
    context = pages.wiki_page(entry, len(codex.WIKI))
    return await render(service, "wiki.html", context, format_entry(entry))


async def wiki_reply(keyword: str, service) -> MessageChain | str:
    if not keyword:
        return await _wiki_card(codex.random_entry(), service)
    if keyword.strip() in ("目录", "分类"):
        counts = "、".join(
            f"{c} {sum(e.category == c for e in codex.WIKI)} 条" for c in codex.CATEGORIES
        )
        return f"百科共 {len(codex.WIKI)} 条：{counts}。发 /百科 分类名 看目录，/百科 序号 看条目"
    entries = codex.search_wiki(keyword)
    if not entries:
        raise Bf1Error(f"百科里没找到“{keyword}”。{_WIKI_HINT}")
    if len(entries) == 1:
        return await _wiki_card(entries[0], service)
    head = f"“{keyword}”找到 {len(entries)} 条，发 /百科 序号 查看"
    if (category := keyword.strip()) in codex.CATEGORIES:
        text = f"{head}：\n" + "、".join(f"{e.no} {e.name}" for e in entries)
        return await render(service, "catalog.html", pages.catalog_page(category, entries), text)
    lines = [_wiki_line(e) for e in entries[:WIKI_LIST]]
    if len(entries) > WIKI_LIST:
        lines.append(f"……还有 {len(entries) - WIKI_LIST} 条，换个更准的词")
    return f"{head}：\n" + "\n".join(lines)


def tip_reply(keyword: str) -> str:
    if not keyword:
        return format_tip(codex.random_tip())
    tips = codex.search_tips(keyword)
    if not tips:
        raise Bf1Error(f"没有带“{keyword}”的小贴士，可以搜地图名、模式名或者正文里的词")
    if len(tips) <= TIP_LIST:
        head = f"带“{keyword}”的小贴士有 {len(tips)} 条："
    else:
        head = f"带“{keyword}”的小贴士有 {len(tips)} 条，随机 {TIP_LIST} 条："
        tips = random.sample(tips, TIP_LIST)
    return head + "".join(f"\n· {format_tip(t)}" for t in tips)


# —— 戳一戳 ——


async def poke_reply(user_id: str, service) -> list[Element]:
    """@ 戳的人，跟一条随机的小贴士或百科卡片。"""

    item = codex.pick()
    if isinstance(item, codex.Tip):
        return [At(user_id), Text(f"\n{format_tip(item)}")]
    card = await _wiki_card(item, service)
    return [At(user_id), Text("\n"), *as_elements(card)]


_poked: dict[tuple[str, str], float] = {}


async def on_poke(event: InternalEvent, service) -> None:
    # satori 的 onebot11 适配器不转换戳一戳，原样作为 INTERNAL 事件送来
    origin = event._origin
    data = getattr(origin, "_data", None)
    if getattr(origin, "_type", None) == "notice.notify.poke" and isinstance(data, dict):
        await poked(event.account, data, service)


async def poked(receiver: Account, data: dict[str, Any], service) -> bool:
    """群里有人戳 bot 时回一条；回了返回 True。"""

    if data.get("group_id") is None:
        return False
    group, user, target = (str(data.get(k)) for k in ("group_id", "user_id", "target_id"))
    if not feature_enabled("bf1", group):
        return False
    # 宿主按群选路，事件只交给一个号；被戳的可能是群里另一个 bot 号，由它来回
    accounts = {str(a.self_id): a for a in accounts_for_group(group, platform=receiver.platform)}
    accounts.setdefault(str(receiver.self_id), receiver)
    if target not in accounts or user in accounts:
        return False
    now = time.monotonic()
    if now - _poked.get((group, user), -POKE_COOLDOWN) < POKE_COOLDOWN:
        return False
    if len(_poked) > 1000:
        _poked.clear()
    _poked[(group, user)] = now
    await accounts[target].protocol.send_message(group, await poke_reply(user, service))
    return True
