"""复读机：群友开始复读时，偶尔跟一句，偶尔打断一下。

判定逻辑在 :mod:`.chain`；这里负责把群消息拆成可比较的内容，并把结果发回群里。
"""

from __future__ import annotations

import hashlib
from collections import defaultdict

import aiohttp
from arclet.entari import BasicConfModel, MessageCreatedEvent, Session, plugin
from arclet.entari.config import EntariConfig, model_field
from loguru import logger
from satori import Emoji, EventType, Image, Text
from satori.client import Account, get_app
from satori.element import Custom, Message
from satori.model import Event
from tenko_api.accounts import online_accounts
from tenko_api.features import declare, feature_required

from .chain import Group, MessageOrder, Repeater, Said, Settings


class RepeaterConfig(BasicConfModel):
    """对应 config/tenko.toml 中的 ``[plugin_config.repeater]``。"""

    min_times: int = 2
    """几个不同的人发了同一句之后，才开始考虑复读。"""
    repeat_probability: float = 0.15
    """达到 min_times 时跟一句的概率；之后每多一人复读，“不跟”的概率再乘以 (1 - 它)。"""
    interrupt_probability: float = 0.05
    """每次考虑时先判断是否打断：文字打乱字序发出，其他内容发一句打断语。"""
    cooldown_seconds: int = 600
    """同一个群出声一次后，这么久之内不再出声。"""
    max_length: int = 100
    """超过这个字数的消息不参与复读。"""
    interrupt_lines: list[str] = model_field(
        default_factory=lambda: ["打断！", "不许复读！", "复读到此为止~"],
        description="无法打乱字序时，随机挑一句发出。",
    )


plugin.metadata(
    "复读机",
    author=["13"],
    version="0.3.1",
    description="群友复读时偶尔跟一句，偶尔打断一下。",
    urls={
        "homepage": "https://github.com/g1331/tenko-plugins/tree/main/plugins/repeater",
        "repository": "https://github.com/g1331/tenko-plugins",
    },
    config=RepeaterConfig,
)
declare(scope="group", default_enabled=True)

config = plugin.get_config(RepeaterConfig)

_IMAGE_LIMIT = 4 * 1024 * 1024


async def _digest(src: str) -> str | None:
    """同一张图被不同人发出时地址不同，只能按内容比较；超时或过大就放弃。"""

    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.get(src) as response:
                if response.status != 200:
                    return None
                hasher, size = hashlib.sha256(), 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    size += len(chunk)
                    if size > _IMAGE_LIMIT:
                        return None
                    hasher.update(chunk)
                return hasher.hexdigest()
    except (aiohttp.ClientError, TimeoutError, ValueError) as error:
        logger.debug("复读机拉取图片失败：{}", type(error).__name__)
        return None


repeater = Repeater(
    Settings(
        min_times=max(config.min_times, 2),
        repeat_probability=config.repeat_probability,
        interrupt_probability=config.interrupt_probability,
        cooldown_seconds=config.cooldown_seconds,
        interrupt_lines=tuple(config.interrupt_lines),
    ),
    _digest,
)


def _command_prefixes() -> tuple[str, ...]:
    try:
        prefixes = EntariConfig.instance.basic.prefix
    except AttributeError:
        prefixes = []
    return tuple(p for p in prefixes if p) or ("/",)


def parse(elements: list, user: str, max_length: int) -> Said | None:
    """拆出可比较的内容；命令、@、引用、长文和混排图片等不参与复读，返回 None。"""

    key: list[str] = []
    text: list[str] = []
    send: list = []
    images: list[str] = []
    for element in elements:
        if isinstance(element, Text):
            key.append(element.text)
            text.append(element.text)
            send.append(element)
        elif isinstance(element, Emoji):
            key.append(f"<face:{element.id}>")
            send.append(element)
        elif isinstance(element, Image):
            images.append(element.src)
        elif isinstance(element, Custom) and element.tag == "onebot:mface":
            attrs = getattr(element, "_attrs", {})
            emoji = attrs.get("emojiId")
            if not emoji or not attrs.get("url"):
                return None
            key.append(f"<mface:{attrs.get('emojiPackageId')}:{emoji}>")
            send.append(Image(src=attrs["url"]))  # 发送端不支持商城表情，以图片发回
        else:
            return None
    content = "".join(text).strip()
    if images:
        if len(images) > 1 or content or len(key) != len(text):
            return None  # 只有“单独一张图”才按图片比较
        return Said(user, None, [Image(src=images[0])], image=images[0])
    joined = "".join(key).strip()
    if not joined or len(content) > max_length or content.startswith(_command_prefixes()):
        return None
    return Said(user, joined, send, text=content)


message_order: defaultdict[Group, MessageOrder] = defaultdict(MessageOrder)


def _message_key(event: Event, message_id: str) -> int | str:
    # NapCat 的 message_id 随接收账号变化，real_seq 是 QQ 的群消息序号。
    sequence = str((event._data or {}).get("real_seq", ""))
    return int(sequence) if sequence.isdecimal() else message_id


async def observe_message(_account: Account, event: Event) -> None:
    if event.type != EventType.MESSAGE_CREATED or event.guild is None:
        return
    if event.channel is not None and event.message is not None:
        group = (event.login.platform, event.channel.id)
        message_order[group].record(_message_key(event, event.message.id))


# 只记消息的顺序。Tenko 过滤的消息和 Entari 还在取引用的消息也会占一个位置。
app = get_app()
app.event_callbacks.insert(0, observe_message)
plugin.collect_disposes(lambda: app.event_callbacks.remove(observe_message))


@plugin.listen(MessageCreatedEvent)
@feature_required()
async def on_message(session: Session[MessageCreatedEvent]):
    event = session.event
    if event.guild is None:
        return
    group = (event.login.platform, event.channel.id)
    order = message_order[group]
    async with order.lock:  # 查图片内容时，同群后续消息也按顺序更新复读状态
        consecutive = order.advance(_message_key(event._origin, event.message.id))
        if consecutive is None:
            return

        user_id = session.user.id
        # 其他 Tenko 账号的消息可以打断复读。
        own_account = user_id in {str(account.self_id) for account in online_accounts()}
        said = (
            None if own_account else parse(list(event.message.message), user_id, config.max_length)
        )
        if not consecutive or said is None:
            repeater.forget(group)
        if said is None:
            return
        reply = await repeater.feed(group, said)

    if reply:
        # 复读不引用原消息：宿主只给“对触发消息的回复”补引用，包在独立的 <message> 里就会跳过。
        await session.send([Message(content=reply)])
