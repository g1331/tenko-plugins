"""AI 群友：接入 DeepSeek（OpenAI 兼容接口），用人设口吻陪群友聊天。

被 @、被回复、听到唤醒词或在私聊里，一定回复；群里累计若干条消息后，让模型自己判断要不要
插一句。模型只能调用 send_message 往当前会话发文字，不能做别的事。请求组装和记账在
:mod:`.chat`，接口调用在 :mod:`.llm`。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

import httpx
from arclet.alconna import Alconna, Args, CommandMeta
from arclet.entari import BasicConfModel, MessageCreatedEvent, Session, command, plugin
from arclet.entari.command import Query
from arclet.entari.config import EntariConfig, model_field
from arclet.entari.plugin import collect_disposes
from loguru import logger
from satori import At, Author, Emoji, Image, Quote, Text
from satori.element import Message
from tenko_api.accounts import online_accounts
from tenko_api.features import declare, feature_required
from tenko_api.messages import report_error
from tenko_api.perm import Permission, require_perm
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
    shape,
    summary_messages,
)
from .persona import FOLLOW_HINT, PERSONA, UNPROMPTED_HINT, mention_hint


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
    max_reply_chars: int = 120
    """单条回复的字数上限，超出时在句末截断。"""
    group_daily_tokens: int = 200000
    """每个群每天的 token 上限（未命中缓存的输入+输出）；0 不限。"""
    global_daily_tokens: int = 0
    """所有群和私聊合计每天的 token 上限，口径同上；0 不限。"""


plugin.metadata(
    "AI群友",
    author=["13"],
    version="0.1.0",
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
conversations: dict[str, Conversation] = {}
_cooldowns: dict[tuple[str, str], float] = {}
_streaks: dict[tuple[str, str], int] = {}
_waiters: dict[str, asyncio.Task[None]] = {}
_reported: set[int] = set()
_MAX_SENDS = 2
_MAX_TOKENS = 300
_MAX_IMAGE_BYTES = 2 * 1024 * 1024
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
    await client.aclose()


collect_disposes(_dispose)


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
    elements: list, conversation: Conversation, self_ids: set[str]
) -> tuple[str, bool, bool, tuple[str, ...]]:
    """转成给模型看的文字，并返回 (文字, 是否 @ 了 bot, 是否回复了 bot, 图片地址)。"""

    parts: list[str] = []
    images: list[str] = []
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
            parts.append("[图片]")
            if element.src:
                images.append(element.src)
        elif isinstance(element, Emoji):
            parts.append(f"[{element.name or '表情'}]")
        else:
            parts.append("[表情]" if getattr(element, "tag", "") == "onebot:mface" else "")
    return " ".join("".join(parts).split()), at_me, reply_me, tuple(images)


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
    conversation = conversations.setdefault(key, Conversation())
    if in_sandbox():
        conversation = conversation.copy()
    elements = list(session.event.message.message)
    text, at_me, reply_me, images = render(elements, conversation, self_ids)
    # 唤醒词只认群友亲手打的字：@ 一个叫“大肥鱼”的群友、引用它的消息都不算叫 bot。
    spoken = "".join(e.text for e in elements if isinstance(e, Text))
    if not text or text.lstrip("@你 ").startswith(_command_prefixes()):
        return
    line = conversation.add(session.event.message.id, _speaker(session), text, images)
    conversation.last_message_at = time.monotonic()

    private = session.event.guild is None
    if private or at_me or reply_me or is_reply_me or any(w in spoken for w in config.wake_words):
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
        follow = (
            fresh
            and now - conversation.last_reply_at < config.follow_seconds
            and conversation.unseen >= _FOLLOW_MIN_NEW
        )
        cold = (
            fresh
            and 0 < config.batch_size <= conversation.unseen
            and now - conversation.last_unprompted_at >= config.unprompted_interval_seconds
        )
        if follow or cold:
            _waiters[key] = asyncio.create_task(_wait_quiet(key, session, conversation, follow))


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


async def _download(http: httpx.AsyncClient, url: str) -> str | None:
    data = bytearray()
    try:
        async with http.stream("GET", url) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                data += chunk
                if len(data) > _MAX_IMAGE_BYTES:
                    return None
    except (httpx.HTTPError, httpx.InvalidURL) as error:
        logger.debug("AI群友下载图片失败：{!r}", error)
        return None
    return data_url(bytes(data))


async def _fetch_pictures(lines: list[Line]) -> None:
    pending = [line for line in lines if line.pictures is None]
    if not pending:
        return
    # 另开一个不带密钥的客户端：图片在 QQ 的服务器上，API key 不能发过去。
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as http:
        for line in pending:
            got = await asyncio.gather(*(_download(http, url) for url in line.images))
            line.pictures = [picture for picture in got if picture]


async def _ask(conversation: Conversation, hint: str, *, force: bool):
    budget = max(config.vision_images, 0)
    shown = conversation.with_images(budget)
    await _fetch_pictures(shown)
    persona = config.persona or PERSONA

    async def ask(images: int):
        messages = build_messages(persona, conversation, hint, images)
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
            hint = mention_hint(target.number, mood)
        else:
            hint = FOLLOW_HINT if follow else UNPROMPTED_HINT
        conversation.unseen = 0
        try:
            calls, usage = await _ask(conversation, hint, force=force)
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
        if not in_sandbox():
            now = datetime.now()
            usage_book.add(now.date(), key, usage, estimate_cost(usage, now))

        sent = 0
        for arguments in calls[:_MAX_SENDS]:
            text = shape(arguments["text"], max(config.max_reply_chars, 1))
            if not text:
                continue
            quoted = conversation.by_number(arguments.get("reply_to"))
            if quoted is None and sent == 0:
                quoted = target
            group = session.event.guild is not None
            # 按编号找人，模型只能 @ 窗口里说过话的群友，编不出 QQ 号也 @ 不了全体。
            called = conversation.by_number(arguments.get("mention"))
            content: list = [Text(text)]
            if group and called and called.speaker:
                name, _, user = called.speaker.rpartition("(")
                content = [At(user.rstrip(")"), name=name), Text(" " + text)]
                text = f"@{name} {text}"
            # 只在接的那条已经被后面的消息盖住时才引用，像真人一样大多直接说。
            if group and quoted and quoted.message_id and conversation.buried(quoted):
                message = [Quote(quoted.message_id), *content]
            else:
                # 包在独立的 <message> 里，宿主就不会自动引用触发消息。
                message = [Message(content=content)]
            receipts = await session.send(message)
            conversation.add(receipts[0].id if receipts else None, None, text)
            conversation.spoken.append(time.monotonic())
            sent += 1
        if sent:
            conversation.last_reply_at = time.monotonic()
        if target is None and sent and not follow:
            conversation.last_unprompted_at = time.monotonic()
        if not in_sandbox() and usage.get("prompt_tokens", 0) > config.context_tokens:
            await _compress(key, conversation)


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


persona_command = Alconna(
    "AI群友",
    Args["action", "重置|用量"],
    meta=CommandMeta(
        "清空 AI 群友在本群/本私聊记住的聊天，或查看 token 用量",
        usage="AI群友 重置 | 用量",
        example="/AI群友 重置\n/AI群友 用量",
    ),
)


@command.on(persona_command)
async def manage(session: Session, action: Query[str] = Query("action")):  # noqa: B008
    if action.result == "重置":
        if session.event.guild is not None and not await require_perm(
            session, Permission.GroupAdmin
        ):
            return "只有群管理员及以上能重置"
        conversations.pop(_key(session), None)
        return "已经忘掉刚才聊的了"
    if not await require_perm(session, Permission.BotAdmin):
        return "只有 BOT 管理员及以上能查看用量"
    return format_usage(usage_book.summary(datetime.now().date()))


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
