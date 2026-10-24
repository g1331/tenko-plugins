"""AI 群友：接入 DeepSeek（OpenAI 兼容接口），用人设口吻陪群友聊天。

被 @、被回复、听到唤醒词或在私聊里，一定回复；群里累计若干条消息后，让模型自己判断要不要
插一句。模型只能往当前会话发文字和收藏的表情包，以及收藏群友发的表情包，不能做别的事。
请求组装和记账在 :mod:`.chat`，接口调用在 :mod:`.llm`，表情包收藏在 :mod:`.stickers`。
"""

from __future__ import annotations

import asyncio
import re
import time
from contextvars import ContextVar
from datetime import datetime

import httpx
from arclet.alconna import Alconna, Args, CommandMeta
from arclet.entari import (
    BasicConfModel,
    MessageCreatedEvent,
    SendResponse,
    Session,
    command,
    plugin,
)
from arclet.entari.command import Query
from arclet.entari.config import EntariConfig, model_field
from arclet.entari.plugin import collect_disposes
from loguru import logger
from satori import At, Author, Emoji, Image, Quote, Text
from satori.element import Message
from tenko_api.accounts import accounts_for_group, online_accounts
from tenko_api.features import declare, feature_required
from tenko_api.messages import report_error
from tenko_api.perm import Permission, get_user_perm, require_perm
from tenko_api.sandbox import in_sandbox
from tenko_api.storage import Store

from . import llm
from .chat import (
    GLOBAL,
    Conversation,
    Line,
    UsageBook,
    build_messages,
    data_url,
    estimate_cost,
    picture_url,
    split,
    summary_messages,
)
from .persona import FOLLOW_HINT, PERSONA, UNPROMPTED_HINT, mention_hint, search_hint
from .stickers import TOKEN, Stickers


class PersonaChatConfig(BasicConfModel):
    """对应 config/tenko.toml 中的 ``[plugin_config.persona_chat]``。"""

    base_url: str = "https://api.deepseek.com"
    """OpenAI 兼容接口地址。"""
    api_key: str = ""
    """接口密钥；为空时插件不应答。只写在本机配置里，不要提交到仓库。"""
    model: str = "deepseek-flash"
    wake_words: list[str] = model_field(
        default_factory=lambda: ["大肥鱼"], description="消息里含这些词时当作在叫 bot。"
    )
    persona: str = ""
    """人设提示词；为空时用内置的“大肥鱼”。"""
    context_tokens: int = 16000
    """一次请求的输入超过这么多 token 后，把较早的聊天压缩成备忘，只留最近约三分之一原文。"""
    batch_size: int = 8
    """没在聊天时，群里累计这么多条新消息后让模型自己判断要不要插话；0 关闭。"""
    follow_seconds: int = 180
    """bot 说话后这么久之内算在聊天里：每来一条新消息就让模型判断要不要接着聊；0 关闭。"""
    quiet_seconds: int = 3
    """要主动说话前，等群里安静这么久，避免在别人连发中间插嘴。"""
    unprompted_interval_seconds: int = 300
    """没在聊天时主动插过一次话后，这么久之内不再这样插话（被叫到、跟聊不受影响）。"""
    user_cooldown_seconds: int = 5
    """同一个人叫 bot 的最短间隔。"""
    stamina: int = 15
    """精力：10 分钟内说这么多句、或 1 小时内说两倍这么多句就累了。用掉三分之二后不再主动开口，
    累了以后被叫到也由它决定理不理，到 1.5 倍完全不理；不说话就慢慢恢复。0 不限。"""
    vision_images: int = 3
    """把最近这么多张群友发的图片低清晰度发给模型看，每张约数百 token；0 只告诉模型“[图片]”。"""
    search_daily: int = 20
    """每个群或私聊每天最多上网搜几次，只在被叫到时能搜，每次约 0.005 元；0 关闭搜索。"""
    sticker_limit: int = 100
    """表情包收藏上限，所有群共用一份；满了丢掉用得最少的。0 关闭表情包。"""
    max_reply_chars: int = 120
    """单条回复的字数上限，超出时在句末截断。"""
    group_daily_tokens: int = 200000
    """每个群每天的 token 上限（未命中缓存的输入+输出）；0 不限。"""
    global_daily_tokens: int = 0
    """所有群和私聊合计每天的 token 上限，口径同上；0 不限。"""


plugin.metadata(
    "AI群友",
    author=["13"],
    version="0.1.8",
    description="接入 DeepSeek，用人设口吻陪群友聊天，内置“大肥鱼”。",
    urls={
        "homepage": "https://github.com/g1331/tenko-plugins/tree/main/plugins/persona_chat",
        "repository": "https://github.com/g1331/tenko-plugins",
    },
    config=PersonaChatConfig,
)
# 每次回复都花钱：所有群默认关闭，只有 BOT 管理员能打开，私聊也只给 BOT 管理员。
declare(scope="group", controlled=True)

config = plugin.get_config(PersonaChatConfig)
usage_book = UsageBook(Store("usage", version=1))
# 别叫 stickers：热重载时导入子模块会把同名的包属性覆盖成 .stickers 模块。
sticker_book = Stickers(Store("stickers", version=1), config.sticker_limit)
conversations: dict[str, Conversation] = {}
# 聊天记录存盘：{会话: Conversation.dump()}。有新消息的会话记进 _unsaved，
# 她每说完一次就存，其余时候最多每分钟存一次，插件卸载或退出时再存一次。
chat_store = Store("conversations", version=1)
_unsaved: set[str] = set()
_saved_at = 0.0
_SAVE_SECONDS = 60
_cooldowns: dict[tuple[str, str], float] = {}
_streaks: dict[tuple[str, str], int] = {}
_waiters: dict[str, asyncio.Task[None]] = {}
_reported: set[int] = set()
# shortcut: 搜索次数只记在内存里，重启清零；要严格限额时改存 Store。
_searches: dict[tuple[str, str], int] = {}
_SEARCH_COST = 0.0007  # 每次 0.005 元，折成美元和用量记账一致
# 她自己发消息期间为真：after_send 里据此分出哪些是 Tenko 功能发的。
_speaking: ContextVar[bool] = ContextVar("persona_chat_speaking", default=False)
_TENKO = "Tenko 功能"
_TENKO_CHARS = 100
_MAX_SENDS = 2
_STICKERS_DONE = "表情包已经处理好了，这次只说话。"
_JUST_TALK = "这次不能搜，也别调用别的工具，直接说话。"
# 宿主给的成员权限（实时群身份和手动授权取高）对应的身份；普通成员不标，省 token。
_ROLES = {
    Permission.GroupAdmin: "群管理",
    Permission.GroupOwner: "群主",
    Permission.BotAdmin: "bot管理员",
    Permission.Master: "bot主人",
}
_MAX_TOKENS = 300
_MAX_IMAGE_BYTES = 2 * 1024 * 1024
_MAX_GIF_BYTES = 8 * 1024 * 1024  # 动图要整个下下来才能抽帧
_SUMMARY_TOKENS = 1000
# 对方在 bot 刚说完几秒内又来叫，连续几轮后不再强制回复，再多就不理，防止和别的 bot 无限对聊。
_QUICK_SECONDS = 10
_STREAK_LOOSE = 3
_STREAK_MUTE = 6
# 跟聊时攒够这么多条新消息才问一次模型，不是每句都接。
_FOLLOW_MIN_NEW = 2

client = httpx.AsyncClient(
    base_url=config.base_url,
    headers={"Authorization": f"Bearer {config.api_key}"},
    timeout=30,
)
if not config.api_key:
    logger.warning("AI群友未配置 api_key，不会应答")


async def _dispose() -> None:
    for task in _waiters.values():
        task.cancel()
    _save()
    await client.aclose()


collect_disposes(_dispose)


def _conversation(key: str) -> Conversation:
    """重启后某个会话第一次来消息时，从存盘恢复它的聊天记录和备忘。"""

    if key not in conversations:
        saved = chat_store.load(default={}).get(key)
        conversations[key] = Conversation.restore(saved) if saved else Conversation()
    return conversations[key]


def _save(*, forget: str | None = None) -> None:
    """把有新消息的会话写进存盘；``forget`` 的会话从存盘里删掉。沙盒里不写。"""

    global _saved_at
    if in_sandbox() or not (_unsaved or forget):
        return
    data = chat_store.load(default={})
    for key in _unsaved:
        if key in conversations:
            data[key] = conversations[key].dump()
    data.pop(forget, None)
    chat_store.save(data)
    _unsaved.clear()
    _saved_at = time.monotonic()


def _changed(key: str) -> None:
    if in_sandbox():
        return
    _unsaved.add(key)
    if time.monotonic() - _saved_at >= _SAVE_SECONDS:
        _save()


def _command_prefixes() -> tuple[str, ...]:
    try:
        prefixes = EntariConfig.instance.basic.prefix
    except AttributeError:
        prefixes = []
    return tuple(p for p in prefixes if p) or ("/",)


def _key(session: Session) -> str:
    if session.event.guild is None:
        return f"private:{session.user.id}"
    return f"group:{session.event.channel.id}"


def _speaker(session: Session) -> str:
    member = session.event.member
    name = (member and member.nick) or session.user.nick or session.user.name or ""
    return f"{name}({session.user.id})"


def render(
    elements: list, conversation: Conversation, self_ids: set[str], raw: list[dict] | None = None
) -> tuple[str, bool, bool, tuple[str, ...], tuple[str, str] | None, tuple[str, ...]]:
    """转成给模型看的文字。

    返回 (文字, 是否 @ 了 bot, 是否回复了 bot, 图片地址, 表情包, 收藏里已有的表情包地址)。

    ``raw`` 是 OneBot 原始的图片段，按顺序对应消息里的图片，用来分出表情包和照片；
    表情包是第一张还没收藏的 (QQ 文件名, 地址)。
    """

    parts: list[str] = []
    images: list[str] = []
    known: list[str] = []
    raw = raw or []
    sticker: tuple[str, str] | None = None
    at_me = reply_me = False
    for element in elements:
        if isinstance(element, Quote):
            quoted = conversation.by_message_id(element.id)
            reply_me = quoted is not None and quoted.speaker is None
            if quoted:
                parts.append(f"回复#{quoted.number} ")
            else:
                # 引用的消息不在窗口里：把作者和开头带上，模型才知道在接什么。
                author = next((c for c in element.children if isinstance(c, Author)), None)
                said = "".join(
                    c.text if isinstance(c, Text) else "[图片]" if isinstance(c, Image) else ""
                    for c in element.children
                )
                parts.append(f"回复[{(author and author.name) or '某人'}：{said[:40]}] ")
        elif isinstance(element, At):
            at_me = at_me or element.id in self_ids
            name = element.name or conversation.name_of(element.id) or element.id
            parts.append("@你 " if element.id in self_ids else f"@{name} ")
        elif isinstance(element, Text):
            parts.append(element.text)
        elif isinstance(element, Image):
            info = raw.pop(0) if raw else {}
            qq_file = str(info.get("file") or "")
            if str(info.get("sub_type")) == "1" or "emoji_id" in info:
                if name := sticker_book.name_of(qq_file):
                    parts.append(f"[表情包:{name}]")  # 收藏里有，平时看备注就够，不用再看图
                    if element.src:
                        known.append(element.src)
                    continue
                parts.append("[表情包]")
                if sticker is None and element.src:
                    sticker = (qq_file, element.src)
            else:
                parts.append("[图片]")
            if element.src:
                images.append(element.src)
        elif isinstance(element, Emoji):
            parts.append(f"[{element.name or '表情'}]")
        else:
            parts.append("[表情]" if getattr(element, "tag", "") == "onebot:mface" else "")
    text = " ".join("".join(parts).split())
    return text, at_me, reply_me, tuple(images), sticker, tuple(known)


def _internal(session: Session):
    """OneBot 原始接口：``await call(动作, 参数=...)``；拿不到（别的适配器）时返回 ``None``。

    直接走 ``call_api``：Entari 包装 ``protocol.internal`` 时会把关键字参数整个包成一项
    ``kwargs``，NapCat 收不到参数，报“Expected required property”。Entari 的包装还会把
    异常当返回值交回来，这里改成抛出；超时由调用方给，免得卡住整个会话。
    """

    # shortcut: 绕开 Entari 的包装缺陷，ArcletProject/Entari#32 修复后改回 protocol.internal。
    call_api = getattr(getattr(session.account, "protocol", None), "call_api", None)
    if not callable(call_api):
        return None

    async def call(action: str, timeout: float, **params):
        result = await asyncio.wait_for(call_api(f"internal/{action}", params), timeout)
        if isinstance(result, Exception):
            raise result
        return result

    return call


async def _raw_images(session: Session) -> list[dict]:
    """satori 适配器转图片时丢了 sub_type 等字段，只好向 OneBot 实现要原始消息段。"""

    # shortcut: 每条带图消息多一次 get_msg；satori-python#43 修复后直接读 Image 的属性。

    internal = _internal(session)
    if internal is None:
        return []
    try:
        data = await internal("get_msg", 5, message_id=int(session.event.message.id))
        return [seg.get("data") or {} for seg in data["message"] if seg.get("type") == "image"]
    except Exception as error:  # 拿不到就当普通图片，不影响聊天
        logger.debug("AI群友读取原始消息失败：{!r}", error)
        return []


@plugin.listen(MessageCreatedEvent)
@feature_required()
async def on_message(session: Session[MessageCreatedEvent], is_reply_me: bool = False):
    if not config.api_key:
        return
    user_id = session.user.id
    # 宿主每条群消息只挑一个账号处理，不看 @ 的是谁，所以 @ 任一 Tenko 账号都算叫 bot；
    # 这些账号发的话也不当作群友。
    self_ids = {str(a.self_id) for a in online_accounts()} | {str(session.account.self_id)}
    if user_id in self_ids:
        return
    key = _key(session)
    conversation = _conversation(key)
    conversation.place = _place(session, conversation)
    if in_sandbox():
        conversation = conversation.copy()
    elements = list(session.event.message.message)
    raw = await _raw_images(session) if any(isinstance(e, Image) for e in elements) else []
    text, at_me, reply_me, images, sticker, known = render(elements, conversation, self_ids, raw)
    # 唤醒词只认群友亲手打的字：@ 一个叫“大肥鱼”的群友、引用它的消息都不算叫 bot。
    spoken = "".join(e.text for e in elements if isinstance(e, Text))
    if not text:
        return
    private = session.event.guild is None
    calling = (
        private or at_me or reply_me or is_reply_me or any(w in spoken for w in config.wake_words)
    )
    # 叫她时带的收藏表情包也给她看图：多半是要她看这张，光凭备注会认错。
    if calling:
        images += known
    role = _ROLES.get(await get_user_perm(session), "")
    line = conversation.add(
        session.event.message.id, _speaker(session), text, images, role, sticker
    )
    conversation.last_message_at = time.monotonic()
    _changed(key)
    # 命令也记进上下文（群里别的 bot 的游戏、群友的示范都靠它看懂），但不算在叫 bot。
    if text.lstrip("@你 ").startswith(_command_prefixes()):
        return

    if calling:
        now = time.monotonic()
        if now - _cooldowns.get((key, user_id), float("-inf")) < config.user_cooldown_seconds:
            return
        _cooldowns[(key, user_id)] = now
        quick = now - conversation.last_reply_at < _QUICK_SECONDS
        streak = _streaks.get((key, user_id), 0) + 1 if quick else 0
        _streaks[(key, user_id)] = streak
        load = _load(conversation, now)
        if streak >= _STREAK_MUTE or load >= 1.5:
            return
        mood = "again" if streak >= _STREAK_LOOSE else "tired" if load >= 1 else "call"
        await respond(session, conversation, line, force=mood == "call", mood=mood)
    elif not private and key not in _waiters:
        now = time.monotonic()
        # 精力用掉三分之二后不再主动开口，只在被叫到时说话。
        fresh = _load(conversation, now) * 3 < 2
        # 紧跟在她后面的那条多半是在回她，一条就问；不然一对一聊着聊着就断了。
        answered = len(conversation.lines) > 1 and conversation.lines[-2].speaker is None
        follow = (
            fresh
            and now - conversation.last_reply_at < config.follow_seconds
            and (answered or conversation.unseen >= _FOLLOW_MIN_NEW)
        )
        cold = (
            fresh
            and 0 < config.batch_size <= conversation.unseen
            and now - conversation.last_unprompted_at >= config.unprompted_interval_seconds
        )
        if follow or cold:
            _waiters[key] = asyncio.create_task(_wait_quiet(key, session, conversation, follow))


def _place(session: Session, conversation: Conversation) -> str:
    """在哪儿聊、用的是哪些号：放在规则后面，群名、账号或撞名的人变了才变，不影响前缀缓存。

    号的昵称随时可能改，所以每次现取，不写进人设；她的名字始终是人设里的大肥鱼。
    记录里有别人的昵称含唤醒词（比如另一个叫“大肥鱼bot”的号）时点名说明那不是她，
    否则模型会把它的话和状态当成自己的。
    """

    guild = session.event.guild
    if guild is None:
        accounts = [session.account]
        place = "你现在在和对方私聊。"
    else:
        platform = getattr(getattr(session.event, "login", None), "platform", None) or "qq-client"
        accounts = list(accounts_for_group(guild.id, platform=platform)) or [session.account]
        place = f"你现在在 QQ 群「{guild.name or guild.id}」里。"
    names = "、".join(_account_label(a) for a in accounts)
    help_command = f"{_command_prefixes()[0]}帮助"
    place = (
        f"{place}你用的 QQ 号是 {names}，群友叫这些名字或 @ 它们都是在叫你；"
        f"这些只是号的昵称，可能会改，你自己始终是大肥鱼。这里的帮助命令是 {help_command}。"
    )
    lookalikes = dict.fromkeys(
        line.speaker
        for line in conversation.lines
        if line.speaker
        and any(w and w in line.speaker.rpartition("(")[0] for w in config.wake_words)
    )
    for speaker in lookalikes:
        place += (
            f"群里的 {speaker} 名字和你像，但不是你，是别人的号：它说的话、它的状态都和你无关。"
        )
    return place


def _account_label(account) -> str:
    user = getattr(getattr(account, "self_info", None), "user", None)
    name = getattr(user, "name", None)
    return f"{name}({account.self_id})" if name else str(account.self_id)


def _typing_delay(text: str) -> float:
    """连发下一条前停一下，像在打字：每秒约 6 个字，最多 3 秒。"""

    return min(0.5 + len(text) / 6, 3.0)


def _load(conversation: Conversation, now: float) -> float:
    """用掉的精力比例，1 是累了。

    按日志里活跃群友的说话量定的：10 分钟里能连说十几句，一小时也就二三十句，
    所以短窗口按 ``stamina``、1 小时按两倍算，取更累的那个。
    """

    if not config.stamina:
        return 0.0
    return max(
        conversation.fatigue(now, 600) / config.stamina,
        conversation.fatigue(now, 3600) / (2 * config.stamina),
    )


async def _wait_quiet(key: str, session: Session, conversation: Conversation, follow: bool) -> None:
    try:
        while (left := conversation.last_message_at + config.quiet_seconds - time.monotonic()) > 0:
            await asyncio.sleep(left)
        await respond(session, conversation, None, follow=follow)
    finally:
        _waiters.pop(key, None)


def _over_budget(key: str) -> bool:
    today = datetime.now().date()
    if config.global_daily_tokens and (
        usage_book.tokens(today, GLOBAL) >= config.global_daily_tokens
    ):
        return True
    return bool(
        key.startswith("group:")
        and config.group_daily_tokens
        and usage_book.tokens(today, key) >= config.group_daily_tokens
    )


async def _download(http: httpx.AsyncClient, url: str) -> bytes | None:
    data = bytearray()
    try:
        async with http.stream("GET", url) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                data += chunk
                limit = _MAX_GIF_BYTES if data[:4] == b"GIF8" else _MAX_IMAGE_BYTES
                if len(data) > limit:
                    return None
    except (httpx.HTTPError, httpx.InvalidURL) as error:
        logger.debug("AI群友下载图片失败：{!r}", error)
        return None
    return bytes(data)


async def _picture(http: httpx.AsyncClient, url: str) -> str | None:
    data = await _download(http, url)
    # 解动图费 CPU，放到线程里，别卡住别的消息。
    return data and await asyncio.to_thread(picture_url, data)


async def _fetch_pictures(lines: list[Line]) -> None:
    pending = [line for line in lines if line.pictures is None]
    if not pending:
        return
    # 另开一个不带密钥的客户端：图片在 QQ 的服务器上，API key 不能发过去。
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as http:
        for line in pending:
            got = await asyncio.gather(*(_picture(http, url) for url in line.images))
            line.pictures = [picture for picture in got if picture]


async def _ask(conversation: Conversation, hint: str, *, force: bool, retry: bool = False):
    budget = max(config.vision_images, 0)
    shown = conversation.with_images(budget)
    await _fetch_pictures(shown)
    persona = config.persona or PERSONA

    catalog = sticker_book.catalog() if config.sticker_limit else ""
    # 眼前有表情包时第一轮不强制只能 send_message，模型才能顺手收藏；提示里仍要求它回复。
    force = force and (retry or not any(line.sticker and line.pictures for line in shown))

    async def ask(images: int):
        messages = build_messages(persona, conversation, hint, images, catalog)
        return await llm.chat(
            client, model=config.model, messages=messages, force=force, max_tokens=_MAX_TOKENS
        )

    try:
        return await ask(budget)
    except httpx.HTTPStatusError as error:
        if error.response.status_code != 400 or not any(line.pictures for line in shown):
            raise
        # 某张图格式或尺寸不被接受时整个请求都会被拒：丢掉这批图，只用文字再问一次。
        logger.warning("AI群友带图请求被拒，去掉图片重试：{}", error.response.text[:200])
        for line in shown:
            line.pictures = []
        return await ask(0)


async def respond(
    session: Session,
    conversation: Conversation,
    target: Line | None,
    *,
    force: bool = False,
    follow: bool = False,
    mood: str = "call",
) -> None:
    """``target`` 是叫 bot 的那条消息；为 ``None`` 时是主动说话：``follow`` 为真是跟聊
    （bot 刚说过话），否则是累计够数后的插话。

    ``force`` 时模型必须回复，否则由它决定说不说。
    """

    if target is None and conversation.lock.locked():
        return  # 已有请求在跑，这次主动插话作罢；被叫到时排队等它结束
    async with conversation.lock:
        key = _key(session)
        if _over_budget(key):
            return
        if target is not None:
            hint = mention_hint(target.number, mood, target.text)
        else:
            hint = FOLLOW_HINT if follow else UNPROMPTED_HINT
        now = datetime.now()
        weekday = "一二三四五六日"[now.weekday()]
        hint = f"现在是 {now:%Y-%m-%d %H:%M}（周{weekday}）。{hint}"
        conversation.unseen = 0
        usages: list[dict] = []

        async def ask(hint: str, force: bool, retry: bool = False) -> list[dict]:
            calls, usage = await _ask(conversation, hint, force=force, retry=retry)
            usages.append(usage)
            if not in_sandbox():
                now = datetime.now()
                usage_book.add(now.date(), key, usage, estimate_cost(usage, now))
            return calls

        # 被叫到时可以先搜再回：第一轮不强制只能说话，没搜也没说话再强制一次。
        # 搜完那轮也不强制，她才能照结果改表情包备注；没说话再强制补一句。
        searching = target is not None and _search_left(key)
        try:
            calls = await ask(hint, force and not searching)
            query = next((str(c.get("query", "")) for c in calls if c.get("tool") == "search"), "")
            searched = bool(searching and query.strip())
            again = None
            if searched:
                again = f"{hint}\n{search_hint(query, await _search(key, query))}"
            elif force and (searching or calls) and all("tool" in c for c in calls):
                # 第一轮没强制（能搜，或眼前有表情包要能收藏）却没说话；或者强制了，
                # DeepSeek 仍去调 search 之类的工具：点明只说话，再强制问一次。
                again = hint
            if again is not None:
                kept = [c for c in calls if c.get("tool") in ("save_sticker", "edit_sticker")]
                if not searched:
                    again = f"{again}\n{_STICKERS_DONE if kept else _JUST_TALK}"
                calls = kept + await ask(again, force and not searched, retry=True)
                if force and searched and all("tool" in c for c in calls):
                    # 强制说话时 DeepSeek 有时仍想改表情包，点明已经改好它才肯说话。
                    calls += await ask(f"{again}\n{_STICKERS_DONE}", True, retry=True)
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            logger.warning("AI群友请求失败：HTTP {} {}", status, error.response.text[:200])
            # 密钥失效、余额不足这类需要人处理的错误，每种只报告一次。
            if status in (400, 401, 402, 403) and status not in _reported:
                _reported.add(status)
                await report_error(error, session)
            return
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as error:
            logger.warning("AI群友请求失败：{!r}", error)
            return
        usage = usages[-1]

        saves = [c for c in calls if c.get("tool") == "save_sticker"]
        edits = [c for c in calls if c.get("tool") == "edit_sticker"]
        calls = [c for c in calls if "tool" not in c]
        sent = 0
        picked = False  # 一次最多发一张表情包
        group = session.event.guild is not None
        # 整个回复只有表情包时，最近发过的也照发，不然被叫到却一声不吭。
        repeat = not any(TOKEN.sub("", c["text"]).strip() for c in calls[:_MAX_SENDS])
        for arguments in calls[:_MAX_SENDS]:
            pieces = split(arguments["text"], max(config.max_reply_chars, 1))
            if not pieces:
                continue
            quoted = conversation.by_number(arguments.get("reply_to"))
            if quoted is None and sent == 0:
                quoted = target
            # 按编号找人，模型只能 @ 窗口里说过话的群友，编不出 QQ 号也 @ 不了全体。
            called = conversation.by_number(arguments.get("mention"))
            # 叫别的 bot 时模型照群友的写法打“@昵称 指令”，要换成真的 @ 对方才会响应。
            named, rest = conversation.called_in(pieces[0])
            if group and named and (called is None or called.speaker == named.speaker):
                called, pieces[0] = named, rest
            for index, piece in enumerate(pieces):
                before, sticker, after = _pick_sticker(
                    conversation, piece, allowed=not picked, repeat=repeat
                )
                picked = picked or sticker is not None
                text = before + (f"[表情:{sticker}]" if sticker else "") + after
                if not text:
                    continue
                mention: list = []
                # @ 和引用只放在连发的第一条上。
                if index == 0 and group and called and called.speaker:
                    name, _, user = called.speaker.rpartition("(")
                    mention = [At(user.rstrip(")"), name=name)]
                    before, text = " " + before, f"@{name} {text}"
                content: list = [*mention, *(Text(part) for part in (before, after) if part)]
                if sticker:
                    content.insert(len(mention) + bool(before), Image(src=_sticker_url(sticker)))
                # 只在接的那条已经被后面的消息盖住时才引用，像真人一样大多直接说。
                quote = (
                    index == 0
                    and group
                    and quoted
                    and quoted.message_id
                    and conversation.buried(quoted)
                )
                if quote:
                    content = [Quote(quoted.message_id), *content]
                if sent:
                    await asyncio.sleep(_typing_delay(text))
                message_id = await _send_sticker(session, content) if sticker else None
                if message_id is None:
                    # 包在独立的 <message> 里，宿主就不会自动引用触发消息。
                    token = _speaking.set(True)
                    try:
                        receipts = await session.send(
                            content if quote else [Message(content=content)]
                        )
                    finally:
                        _speaking.reset(token)
                    message_id = receipts[0].id if receipts else None
                conversation.add(message_id or None, None, text)
                if sticker:
                    sticker_book.use(sticker)
                sent += 1
            conversation.spoken.append(time.monotonic())  # 一口气连发的几条算一句话
        for arguments in saves:
            await _save_sticker(arguments, conversation)
        for arguments in edits:
            _edit_sticker(arguments, key)
        if calls and not sent:
            logger.info("AI群友在 {} 想说的话全被丢掉了：{}", key, [c["text"] for c in calls])
        if sent:
            conversation.last_reply_at = time.monotonic()
        if target is None and sent and not follow:
            conversation.last_unprompted_at = time.monotonic()
        if not in_sandbox() and usage.get("prompt_tokens", 0) > config.context_tokens:
            await _compress(key, conversation)
        _changed(key)
        _save()


def _pick_sticker(
    conversation: Conversation, text: str, *, allowed: bool, repeat: bool = False
) -> tuple[str, str | None, str]:
    """拆出第一个能发的“[表情:名字]”：返回 (前面的话, 名字, 后面的话)，其余标记删掉。

    名字不在收藏里（模型编的）、最近在这儿发过，或这次已经发过一张，都不发。“最近”按收藏多少定：
    每张算 1 分钟，最长半小时，收藏少时才不至于发过一两张就没得挑；``repeat`` 时不看最近发没发过。
    一整条正好是某张收藏的名字（模型把图上的字打了出来，如“得加钱！”）时，改发那张图。
    """

    window = min(1800, 60 * len(sticker_book.index()))
    if allowed and not TOKEN.search(text):
        bare = _bare(text)
        for name in sticker_book.index():
            if (
                _bare(name) == bare
                and sticker_book.path(name)
                and (repeat or not conversation.sent_recently(f"[表情:{name}]", window))
            ):
                return "", name, ""
    chosen: str | None = None
    head = text
    for match in TOKEN.finditer(text):
        name = match[1]
        if (
            allowed
            and chosen is None
            and sticker_book.path(name)
            and (repeat or not conversation.sent_recently(f"[表情:{name}]", window))
        ):
            chosen = name
            head, tail = text[: match.start()], text[match.end() :]
            break
    if chosen is None:
        return TOKEN.sub("", text).strip(), None, ""
    return TOKEN.sub("", head).strip(), chosen, TOKEN.sub("", tail).strip()


def _bare(text: str) -> str:
    """去掉标点、空白和句尾语气词，只比字面。"""

    return re.sub(r"[\W_]+", "", text).rstrip("哦啊呀啦嘛").lower()


def _sticker_url(name: str) -> str:
    path = sticker_book.path(name)
    return (path and data_url(path.read_bytes())) or ""


async def _send_sticker(session: Session, content: list) -> str | None:
    """走 OneBot 原始接口发：带上 sub_type=1 才显示成表情包，satori 发出去是一张照片。

    shortcut: 自己拼 OneBot 消息段；RF-Tar-Railt/satori-python#43 修复后改用 session.send。

    接口不可用或失败时返回 ``None``，由调用方改用 ``session.send``；超时返回空串，
    NapCat 可能已经发出去了，不再补发。
    """

    internal = _internal(session)
    if in_sandbox() or internal is None:
        return None
    message = []
    for element in content:
        if isinstance(element, Quote):
            message.append({"type": "reply", "data": {"id": element.id}})
        elif isinstance(element, At):
            message.append({"type": "at", "data": {"qq": element.id}})
        elif isinstance(element, Image):
            data = {"file": "base64://" + element.src.partition(",")[2]}
            message.append(
                {"type": "image", "data": {**data, "sub_type": 1, "summary": "[动画表情]"}}
            )
        else:
            message.append({"type": "text", "data": {"text": element.text}})
    try:
        if session.event.guild is not None:
            result = await internal(
                "send_group_msg", 15, group_id=int(session.event.channel.id), message=message
            )
        else:
            result = await internal(
                "send_private_msg", 15, user_id=int(session.user.id), message=message
            )
    except TimeoutError:
        logger.warning("AI群友发表情包超时，可能已经发出，不再补发")
        return ""
    except Exception as error:
        logger.warning("AI群友按表情包发送失败，改发普通图片：{!r}", error)
        return None
    return str((result or {}).get("message_id") or "")


async def _save_sticker(arguments: dict, conversation: Conversation) -> None:
    """收藏模型挑中的表情包：只收这次真给它看过的表情包，照片和截图不收。"""

    line = conversation.by_number(arguments.get("number"))
    if not config.sticker_limit or line is None or not line.sticker or not line.pictures:
        return
    qq_file, src = line.sticker
    line.sticker = None
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as http:
        data = await _download(http, src)
    if data is None or data_url(data) is None:
        return
    saved = sticker_book.add(
        str(arguments.get("name", "")),
        str(arguments.get("mood", "")),
        data,
        qq_file,
        str(arguments.get("replace") or ""),
    )
    if saved:
        name, dropped = saved
        logger.info("AI群友收藏了表情包「{}」", name)
        for old in dropped:
            logger.info("AI群友收藏满了，丢掉了表情包「{}」", old)


def _edit_sticker(arguments: dict, key: str) -> None:
    """收藏所有群共用，改动都记日志，被带偏了能查到是哪个会话改的、原来写的什么。"""

    if not config.sticker_limit:
        return
    # 模型偶尔给出空参数，None 不能写成“None”。
    name, mood, new_name = (str(arguments.get(k) or "") for k in ("name", "mood", "new_name"))
    old = sticker_book.index().get(name, {}).get("mood")
    final = sticker_book.edit(name, mood, new_name)
    if final:
        new = sticker_book.index()[final]["mood"]
        logger.info("AI群友在 {} 改了表情包「{}」→「{}」：{} → {}", key, name, final, old, new)


def _search_left(key: str) -> bool:
    today = datetime.now().date().isoformat()
    return 0 < config.search_daily and _searches.get((today, key), 0) < config.search_daily


async def _search(key: str, query: str) -> str:
    """搜一次，整理成给模型看的几行；失败当作没搜到。"""

    today = datetime.now().date()
    _searches[(today.isoformat(), key)] = _searches.get((today.isoformat(), key), 0) + 1
    try:
        results = await llm.search(client, query)
    except (httpx.HTTPError, ValueError, KeyError) as error:
        logger.warning("AI群友搜索失败：{!r}", error)
        return ""
    logger.info("AI群友搜索“{}”：{} 条结果", query, len(results))
    if results and not in_sandbox():
        usage_book.add(today, key, {}, _SEARCH_COST)  # 只有搜到结果才计费
    lines = []
    for r in results:
        source = f"{r.get('site_name') or '未知站点'}，{(r.get('date') or '')[:10]}"
        text = (r.get("summary") or r.get("snippet") or "")[:300]
        lines.append(f"- {r.get('title', '')}（{source}）：{text}")
    return "\n".join(lines)


async def _compress(key: str, conversation: Conversation) -> None:
    """把较早的记录并进备忘。失败只记日志，下次超阈值时再试。"""

    old = conversation.older_than(config.context_tokens // 3)
    if not old:
        return
    try:
        summary, usage = await llm.summarize(
            client,
            model=config.model,
            messages=summary_messages(conversation.summary, old),
            max_tokens=_SUMMARY_TOKENS,
        )
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as error:
        logger.warning("AI群友压缩聊天记录失败：{!r}", error)
        return
    now = datetime.now()
    usage_book.add(now.date(), key, usage, estimate_cost(usage, now))
    if summary:
        conversation.forget(old[-1].number, summary)
        logger.info("AI群友把 {} 的 {} 条旧消息压成了备忘", key, len(old))


@plugin.listen(SendResponse)
async def on_sent(event: SendResponse) -> None:
    """Tenko 功能从这些号发出的回复（游戏出题、查询结果等）也记进群聊记录，她才看得懂前后文。

    同一个号自己发的消息 Entari 不会再当作收到的消息分发，只能在发送后这里接。
    """

    if _speaking.get() or in_sandbox() or event.channel.startswith("private:"):
        return
    conversation = conversations.get(f"group:{event.channel}")
    if conversation is None:  # 这个群没开或还没聊过
        return
    text = " ".join(_plain(event.message).split())
    if not text:
        return
    if len(text) > _TENKO_CHARS:
        text = text[:_TENKO_CHARS] + "…"
    message_id = event.result[0].id if event.result else None
    conversation.add(message_id, _TENKO, text, counted=False)
    _changed(f"group:{event.channel}")


def _plain(elements) -> str:
    """命令回复只留文字：图片记成“[图片]”，不给模型看（多是渲染出来的长图）。"""

    parts = []
    for element in elements:
        if isinstance(element, Text):
            parts.append(element.text)
        elif isinstance(element, At):
            parts.append(f"@{element.name or element.id} ")
        elif isinstance(element, Image):
            parts.append("[图片]")
        elif isinstance(element, Message):
            parts.append(_plain(element.children))
    return "".join(parts)


persona_command = Alconna(
    "AI群友",
    Args["action", "重置|用量|表情"]["op;?", "看|删"]["name;?", str],
    meta=CommandMeta(
        "清空 AI 群友在本群/本私聊记住的聊天、查看 token 用量，或管理它收藏的表情包",
        usage="AI群友 重置 | 用量 | 表情 [看|删 名字]",
        example="/AI群友 重置\n/AI群友 用量\n/AI群友 表情\n/AI群友 表情 看 猫猫震惊",
    ),
)


@command.on(persona_command)
async def manage(
    session: Session,
    action: Query[str] = Query("action"),  # noqa: B008
    op: Query[str] = Query("op"),  # noqa: B008
    name: Query[str] = Query("name"),  # noqa: B008
):
    if action.result == "重置":
        if session.event.guild is not None and not await require_perm(
            session, Permission.GroupAdmin
        ):
            return "只有群管理员及以上能重置"
        key = _key(session)
        conversations.pop(key, None)
        _unsaved.discard(key)
        _save(forget=key)
        return "已经忘掉刚才聊的了"
    if action.result == "表情":
        return await manage_stickers(session, op.result if op.available else None, name.result)
    if not await require_perm(session, Permission.BotAdmin):
        return "只有 BOT 管理员及以上能查看用量"
    return format_usage(usage_book.summary(datetime.now().date()))


async def manage_stickers(session: Session, op: str | None, name: str | None) -> str | None:
    # 收藏所有群共用：看归群管理，删会影响别的群，只给 BOT 管理员。
    if op == "删":
        if not await require_perm(session, Permission.BotAdmin):
            return "收藏是所有群共用的，只有 BOT 管理员及以上能删"
        return (
            f"删掉了「{name}」" if name and sticker_book.remove(name) else f"收藏里没有「{name}」"
        )
    if session.event.guild is not None and not await require_perm(session, Permission.GroupAdmin):
        return "只有群管理员及以上能查看表情包收藏"
    if op == "看":
        url = _sticker_url(name or "")
        if not url:
            return f"收藏里没有「{name}」"
        await session.send([Image(src=url)])
        return None
    index = sticker_book.index()
    if not index:
        return "还没有收藏表情包"
    lines = [f"表情包收藏 {len(index)}/{config.sticker_limit}："]
    lines += [f"{n}（{s['mood']}）用过 {s['used']} 次" for n, s in index.items()]
    return "\n".join(lines)


def format_usage(periods: list[tuple[str, dict[str, list[float]]]]) -> str:
    lines = ["AI群友用量（按 deepseek-flash 价目估算）"]
    for title, rows in periods:
        total = rows.get(GLOBAL)
        if not total:
            lines.append(f"{title}：没有用量")
            continue
        lines.append(f"{title}：{_amount(total)}")
        others = sorted(
            ((k, v) for k, v in rows.items() if k != GLOBAL), key=lambda item: -sum(item[1][:3])
        )
        for key, row in others[:10]:
            kind, _, ident = key.partition(":")
            lines.append(f"  {'群' if kind == 'group' else '私聊'} {ident}：{_amount(row)}")
    return "\n".join(lines)


def _amount(row: list[float]) -> str:
    miss, hit, out, cost = row
    return f"{int(miss + hit + out):,} token（命中缓存 {int(hit):,}），约 ${cost:.4f}"
