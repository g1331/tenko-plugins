"""复读机：群友开始复读时，偶尔跟一句，偶尔打断一下。

判定逻辑在 :mod:`.chain`；这里负责把群消息拆成可比较的内容，并把结果发回群里。
"""

from __future__ import annotations

import hashlib

import aiohttp
from arclet.entari import BasicConfModel, MessageCreatedEvent, Session, plugin
from arclet.entari.config import EntariConfig, model_field
from loguru import logger
from satori import Emoji, Image, Text
from satori.element import Custom, Message
from tenko_api.accounts import online_accounts
from tenko_api.context import context_from_session
from tenko_api.features import declare, feature_required
from tenko_api.perm import Permission

from .chain import Repeater, Said, Settings


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
    version="0.1.1",
    description="群友复读时偶尔跟一句，偶尔打断一下。",
    urls={
        "homepage": "https://github.com/g1331/tenko-plugins/tree/main/plugins/repeater",
        "repository": "https://github.com/g1331/tenko-plugins",
    },
    config=RepeaterConfig,
)
declare(scope="group", default_enabled=True, level=Permission.ActiveGroup)

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


@plugin.listen(MessageCreatedEvent)
@feature_required()
async def on_message(session: Session[MessageCreatedEvent]):
    ctx = context_from_session(session)
    if ctx.chat_type != "group":
        return
    # 同一个群里的其他 Tenko 账号发的消息不算群友复读。
    if ctx.user_id in {str(account.self_id) for account in online_accounts()}:
        return
    said = parse(list(session.event.message.message), ctx.user_id, config.max_length)
    if said is None:
        repeater.forget(ctx.channel_id)
        return
    reply = await repeater.feed(ctx.channel_id, said)
    if reply:
        # 复读不引用原消息：宿主只给“对触发消息的回复”补引用，包在独立的 <message> 里就会跳过。
        await session.send([Message(content=reply)])
