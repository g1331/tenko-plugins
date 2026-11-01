"""随机老婆：群友每天抽一个老婆，抽到两次别人的老婆就抢过来。

移植自旧版 xiaomai-bot 的 random_wife。抽老婆的规则在 :mod:`.marriage`，老婆库（图片文件）在
:mod:`.library`，老婆列表的排版在 :mod:`.pages`；
这里解析命令、处理群员投稿的审核，并把结果发回群里。
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from datetime import datetime
from typing import Any

from arclet.alconna import Alconna, AllParam, Args, CommandMeta, Option
from arclet.entari import (
    BasicConfModel,
    MessageChain,
    MessageCreatedEvent,
    Session,
    command,
    plugin,
)
from arclet.entari.command import Query
from loguru import logger
from satori import At, Author, Element, Image, Quote, Text
from tenko_api.accounts import accounts_for_group, online_accounts
from tenko_api.features import declare, rate_limit
from tenko_api.notify import master_reply, notify_master
from tenko_api.perm import Permission, group_only, require, require_perm
from tenko_api.render import RenderService
from tenko_api.storage import Store

from . import library, pages
from .library import LibraryError
from .marriage import Registry


class RandomWifeConfig(BasicConfModel):
    """对应 config/tenko.toml 中的 ``[plugin_config.random_wife]``。"""

    review_group: str = ""
    """群员投稿的老婆发到这个群审核，群管理员引用回复 y 同意；留空时发给 Master 审核。"""


plugin.metadata(
    "随机老婆",
    author=["13"],
    version="0.2.0",
    description="群里每天抽一个老婆，抽到两次别人的老婆就抢过来；老婆库由管理员维护，群员可投稿。",
    urls={
        "homepage": "https://github.com/g1331/tenko-plugins/tree/main/plugins/random_wife",
        "repository": "https://github.com/g1331/tenko-plugins",
    },
    config=RandomWifeConfig,
)
declare(scope="group", default_enabled=True)

config = plugin.get_config(RandomWifeConfig)

REVIEW_HOURS = 24
KEY = "submission:"
"""发给 Master 的审核通知用的 key 前缀，后面接投稿编号。"""
_registry_store = Store("marriages", version=1)
_request_store = Store("requests", version=1)
_lock = asyncio.Lock()
rng = random.Random()


def _today() -> str:
    return datetime.now().date().isoformat()


def _display_name(session: Session) -> str:
    """群名片优先：OneBot 适配器把它放在 member.nick 里。"""

    member = getattr(session.event, "member", None)
    return getattr(member, "nick", None) or session.user.name or session.user.id


def _photo(path) -> Image:
    data = path.read_bytes()
    return Image.of(raw=data, mime=library.mime(data, path))


# ---- 抽老婆、离婚 ----


async def draw(group: str, user: str, name: str) -> MessageChain | str:
    wives = library.wives()
    if not wives:
        return "老婆库还是空的，请管理员先用 /添加老婆 加几个"
    async with _lock:
        registry = Registry(_registry_store.load(), _today())
        outcome = registry.draw(group, user, name, rng.choice(list(wives)), set(wives))
        _registry_store.save(registry.state)
    wife, photo = outcome.wife, _photo(wives[outcome.wife])
    if outcome.kind in ("mine", "new"):
        return MessageChain([Text(f"你今天的老婆是{wife}哦~不想要了可以 /离婚\n"), photo])
    if outcome.kind == "stolen":
        return MessageChain(
            [Text(f"你抢走了【{outcome.owner}】的老婆{wife}~\n"), photo, Text("\n达成成就【NTR】")]
        )
    return MessageChain([Text(f"你抽到了{wife}，是【{outcome.owner}】的老婆哦~\n"), photo])


async def divorce(group: str, user: str) -> str:
    async with _lock:
        registry = Registry(_registry_store.load(), _today())
        wife = registry.divorce(group, user)
        _registry_store.save(registry.state)
    if wife is None:
        return "你还没有老婆，先 /抽老婆 抽一个吧~"
    return f"你已经和{wife}离婚了~"


# ---- 老婆库：添加、删除、投稿审核 ----


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


def _wife_name(name: Query[str], rest: Query[Any]) -> str:
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
    async with _lock:
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
        async with _lock:
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
        async with _lock:
            library.add(wife, data)
        return f"已把{wife}加进老婆库"
    return await submit(
        session.event.channel.id,
        session.event.login.platform,
        session.user.id,
        _display_name(session),
        wife,
        data,
    )


async def decide(number: str, said: str) -> str:
    """审核群里管理员或 Master 的回复：y 同意，其他文字作为理由拒绝。"""

    async with _lock:
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


@plugin.listen(MessageCreatedEvent)
async def on_review_reply(session: Session[MessageCreatedEvent]):
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


@plugin.listen(MessageCreatedEvent)
async def on_master_reply(session: Session[MessageCreatedEvent]):
    """没配审核群时投稿发给 Master，Master 引用那条通知回复。"""

    reply = await master_reply(session)
    if reply is None or not reply.key.startswith(KEY) or not reply.text:
        return
    await session.send(await decide(reply.key.removeprefix(KEY), reply.text))


# ---- 命令 ----


def _commands(
    names: list[str], args: Args, description: str, usage: str, example: str, *options: Option
):
    """主命令加旧 bot 的叫法：entari 按命令名派发，别名到不了处理函数，每个名字单独注册。"""

    def register(handler):
        main, aliases = names[0], names[1:]
        note = f"；也可以用 {'、'.join(aliases)}" if aliases else ""
        meta = CommandMeta(description + note, usage=usage, example=example)
        command.on(Alconna(main, args, *options, meta=meta))(handler)
        for alias in aliases:
            meta = CommandMeta(description, usage=usage.replace(main, alias, 1), hide=True)
            command.on(Alconna(alias, args, *options, meta=meta))(handler)
        return handler

    return register


async def _answer(job) -> MessageChain | str:
    try:
        return await job
    except LibraryError as error:
        return str(error)


@_commands(
    ["抽老婆", "随机老婆", "随机wife"],
    Args(),
    "抽一个今天的老婆；抽到别人的老婆两次就抢过来，每天零点重置",
    "抽老婆",
    "/抽老婆",
)
@group_only()
@rate_limit(3)
async def draw_wife(session: Session):
    return await draw(session.event.channel.id, session.user.id, _display_name(session))


@_commands(["离婚"], Args(), "放弃今天的老婆，可以重新抽", "离婚", "/离婚")
@group_only()
async def give_up_wife(session: Session):
    return await divorce(session.event.channel.id, session.user.id)


@_commands(
    ["添加老婆", "添加wife"],
    Args["name", str]["rest?", AllParam],
    "把一张图片加进老婆库，名字就是老婆的名字；BOT 管理员直接加，群员投稿交给审核群或 Master",
    "添加老婆 <名字> [图片]",
    "/添加老婆 芙莉莲 [图片]",
)
async def add_wife_command(
    session: Session,
    name: Query[str] = Query("name"),  # noqa: B008
    rest: Query[Any] = Query("rest"),  # noqa: B008
):
    try:
        wife = _wife_name(name, rest)
    except LibraryError as error:
        return str(error)
    return await _answer(add_wife(session, wife))


@_commands(
    ["删除老婆", "删除wife"],
    Args["name", str]["rest?", AllParam],
    "从老婆库里删掉一个老婆",
    "删除老婆 <名字>",
    "/删除老婆 芙莉莲",
)
@require(Permission.BotAdmin)
async def remove_wife_command(
    name: Query[str] = Query("name"),  # noqa: B008
    rest: Query[Any] = Query("rest"),  # noqa: B008
):
    try:
        wife = _wife_name(name, rest)
    except LibraryError as error:
        return str(error)
    async with _lock:
        removed = library.remove(wife)
    return f"已把{wife}从老婆库删掉" if removed else f"老婆库里没有{wife}"


@_commands(
    ["老婆列表"],
    Args["keyword?", str],
    "看看老婆库里有谁：缩略图、名字和文件大小；后面的文字都按名字搜，翻页用 -p",
    "老婆列表 [关键词] [-p 页码]",
    "/老婆列表 中野 -p 2",
    Option("-p|-page|-页", Args["page", int], dest="page", help_text="第几页"),
)
async def list_wives(
    keyword: Query[str] = Query("keyword"),  # noqa: B008
    page: Query[int] = Query("page.page"),  # noqa: B008
    *,
    render_service: RenderService,
):
    return await wife_list(
        keyword.result if keyword.available else None,
        page.result if page.available else 1,
        render_service,
    )


async def wife_list(keyword: str | None, page: int, service=None) -> MessageChain | str:
    image, context = await pages.render(service, library.wives(), keyword, page)
    if image:
        return MessageChain(Image.of(raw=image, mime="image/jpeg"))
    return pages.format_list(context)
