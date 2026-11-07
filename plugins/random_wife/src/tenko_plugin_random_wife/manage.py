"""老婆库的维护：管理员直接添加、删除，群员投稿交给审核群或 Master，以及老婆列表。"""

from __future__ import annotations

import re
import time
from typing import Any

from arclet.entari import MessageChain, MessageCreatedEvent, Session
from arclet.entari.command import Query
from loguru import logger
from satori import At, Author, Element, Image, Quote, Text
from tenko_api.accounts import accounts_for_group, online_accounts
from tenko_api.notify import master_reply, notify_master
from tenko_api.perm import Permission, require_perm
from tenko_api.storage import Store

from . import library, pages
from .config import config
from .entry import display_name
from .library import LibraryError, lock

REVIEW_HOURS = 24
KEY = "submission:"
"""发给 Master 的审核通知用的 key 前缀，后面接投稿编号。"""
_request_store = Store("requests", version=1)


def _image_src(elements: list[Element]) -> str | None:
    """消息里的图片，或者引用的那条消息里的图片。"""

    for element in elements:
        if isinstance(element, Image):
            return element.src
    for element in elements:
        if isinstance(element, Quote):
            found = _image_src(list(element.children))
            if found:
                return found
    return None


def wife_name(name: Query[str], rest: Query[Any]) -> str:
    """名字里可以有空格：命令名后面的文字都算名字，图片不算。"""

    words = [name.result] if name.available else []
    if rest.available:
        words += [e.text for e in rest.result if isinstance(e, Text)]
    return library.check_name(" ".join(w.strip() for w in words if w.strip()))


def _pending() -> dict[str, Any]:
    """未处理的投稿；过期的顺手清掉。"""

    data = _request_store.load({"next": 1, "items": {}})
    now = time.time()
    for key, item in list(data["items"].items()):
        if now - item["at"] > REVIEW_HOURS * 3600:
            _drop_image(item)
            del data["items"][key]
    return data


def _drop_image(item: dict[str, Any]) -> None:
    (library.folder("pending") / item["file"]).unlink(missing_ok=True)


def _review_text(number: int, item: dict[str, Any]) -> str:
    return (
        f"收到群 {item['group']} 的 {item['user_name']}({item['user']}) "
        f"添加老婆【{item['wife']}】的申请（编号 {number}）\n"
    )


async def _send(group: str, platform: str, content: list[Element]) -> bool:
    for account in accounts_for_group(group, platform=platform):
        try:
            await account.protocol.send_message(group, content)
            return True
        except Exception as error:  # 掉线、被禁言：换下一个号
            logger.warning("随机老婆用 {} 往群 {} 发消息失败：{!r}", account.self_id, group, error)
    return False


async def submit(
    group: str, platform: str, user: str, user_name: str, wife: str, data: bytes
) -> str:
    async with lock:
        if wife in library.wives():
            return f"{wife} 已经在老婆库里了"
        pending = _pending()
        if any(item["wife"] == wife for item in pending["items"].values()):
            return f"已经有人投稿了{wife}，等管理员审核吧"
        number = pending["next"]
        item = {
            "group": group,
            "platform": platform,
            "user": user,
            "user_name": user_name,
            "wife": wife,
            "file": f"{number}{library.suffix(data)}",
            "at": time.time(),
        }
        (library.folder("pending") / item["file"]).write_bytes(data)
        pending["items"][str(number)] = item
        pending["next"] = number + 1
        _request_store.save(pending)
    photo = Image.of(raw=data, mime=library.MIME[library.suffix(data)])
    hint = Text(f"\n{REVIEW_HOURS} 小时内引用这条消息回复 y 同意，回复其他内容作为理由拒绝")
    failure = await _ask_review(number, platform, [Text(_review_text(number, item)), photo, hint])
    if failure:
        async with lock:
            pending = _pending()
            if dropped := pending["items"].pop(str(number), None):
                _drop_image(dropped)
            _request_store.save(pending)
        return failure
    return "投稿已提交给管理员，审核通过后会加进老婆库"


async def _ask_review(number: int, platform: str, content: list[Element]) -> str | None:
    """配了审核群就发到群里，否则发给 Master；发不出去时返回给投稿人的话。"""

    if config.review_group:
        if await _send(config.review_group, platform, content):
            return None
        return "投稿没能发到审核群，稍后再试"
    notice = await notify_master(content, key=f"{KEY}{number}")
    if notice.delivered:
        return None
    if notice.reason == "rate_limited":
        return "最近投稿的人太多了，过一会儿再来"
    return "投稿没能发给管理员，稍后再试"


async def add_wife(session: Session, wife: str) -> str:
    src = _image_src(list(session.event.message.message))
    if src is None:
        return "要带上老婆的图片：/添加老婆 名字 [图片]，或者引用一张图片发这条命令"
    admin = await require_perm(session, Permission.BotAdmin)
    if not admin and session.event.guild is None:
        return "投稿要在群里发"
    data = await library.download(src)
    library.suffix(data)  # 先确认是图片，再决定怎么处理
    if admin:
        async with lock:
            library.add(wife, data)
        return f"已把{wife}加进老婆库"
    return await submit(
        session.event.channel.id,
        session.event.login.platform,
        session.user.id,
        display_name(session),
        wife,
        data,
    )


async def decide(number: str, said: str) -> str:
    """审核群里管理员或 Master 的回复：y 同意，其他文字作为理由拒绝。"""

    async with lock:
        pending = _pending()
        item = pending["items"].pop(number, None)
        if item is None:
            return f"编号 {number} 的申请已经处理过或过期了"
        approve = said.casefold() == "y"
        image = library.folder("pending") / item["file"]
        error = None
        if approve:
            try:
                library.add(item["wife"], image.read_bytes())
            except (LibraryError, OSError) as failure:
                error = str(failure)
        _drop_image(item)
        _request_store.save(pending)
    wife, user = item["wife"], item["user"]
    if error:
        notice = f" 你投稿的老婆【{wife}】没能加进老婆库：{error}"
        reply = f"添加{wife}失败：{error}"
    elif approve:
        notice = f" 你投稿的老婆【{wife}】审核通过了！"
        reply = f"已同意 {item['user_name']}({user}) 投稿的{wife}"
    else:
        notice = f" 你投稿的老婆【{wife}】没有通过，理由：{said}"
        reply = f"已拒绝 {item['user_name']}({user}) 投稿的{wife}"
    if not await _send(item["group"], item["platform"], [At(user), Text(notice)]):
        reply += "（没能通知到投稿的人）"
    return reply


def _quoted_request(elements: list[Element], self_ids: set[str]) -> str | None:
    """回复的是 bot 发的审核消息时，取出申请编号。

    各个号的消息号互不相通，所以不比消息号，看引用里的原文。
    """

    quote = next((e for e in elements if isinstance(e, Quote)), None)
    if quote is None:
        return None
    author = next((c for c in quote.children if isinstance(c, Author)), None)
    if author is None or author.id not in self_ids:
        return None
    said = "".join(c.text for c in quote.children if isinstance(c, Text))
    found = re.search(r"添加老婆【.*】的申请（编号 (\d+)）", said)
    return found[1] if found else None


async def on_group_reply(session: Session[MessageCreatedEvent]):
    """审核群里引用审核消息的回复。"""

    if not config.review_group or session.event.guild is None:
        return
    if session.event.channel.id != config.review_group:
        return
    elements = list(session.event.message.message)
    self_ids = {str(a.self_id) for a in online_accounts()} | {str(session.account.self_id)}
    number = _quoted_request(elements, self_ids)
    if number is None:
        return
    said = "".join(e.text for e in elements if isinstance(e, Text)).strip()
    if not said or not await require_perm(session, Permission.GroupAdmin):
        return
    await session.send(await decide(number, said))


async def on_master_reply(session: Session[MessageCreatedEvent]):
    """没配审核群时投稿发给 Master，Master 引用那条通知回复。"""

    reply = await master_reply(session)
    if reply is None or not reply.key.startswith(KEY) or not reply.text:
        return
    await session.send(await decide(reply.key.removeprefix(KEY), reply.text))


async def remove_wife(wife: str) -> str:
    async with lock:
        removed = library.remove(wife)
    return f"已把{wife}从老婆库删掉" if removed else f"老婆库里没有{wife}"


async def wife_list(keyword: str | None, page: int, service=None) -> MessageChain | str:
    image, context = await pages.render(service, library.wives(), keyword, page)
    if image:
        return MessageChain(Image.of(raw=image, mime="image/jpeg"))
    return pages.format_list(context)
