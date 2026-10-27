"""AI 群友：接入 DeepSeek（OpenAI 兼容接口），用人设口吻陪群友聊天。

被 @、被回复或在私聊里，一定回复；只听到唤醒词时由模型判断是不是在叫她；群里累计若干条
消息后，让模型自己判断要不要插一句。模型只能往当前会话发文字和收藏的表情包，以及收藏群友发的表情包，
被叫到时还能用别的扩展插件的命令查东西（以叫她的人的身份执行），不能做别的事。
请求组装和记账在 :mod:`.chat`，接口调用在 :mod:`.llm`，表情包收藏在 :mod:`.stickers`。
"""

from __future__ import annotations

import ast
import asyncio
import base64
import dataclasses
import json
import random
import re
import time
from contextvars import ContextVar
from datetime import datetime
from importlib.metadata import entry_points
from itertools import islice, pairwise

import httpx
from arclet.alconna import Alconna, AllParam, Args, CommandMeta, command_manager
from arclet.entari import (
    BasicConfModel,
    ConfigReload,
    MessageCreatedEvent,
    SendResponse,
    Session,
    command,
    plugin,
)
from arclet.entari.command import Query, execute
from arclet.entari.config import EntariConfig, config_model_validate, model_field
from arclet.entari.plugin import collect_disposes, get_plugin_commands, get_plugins
from loguru import logger
from satori import At, Author, Emoji, Image, Quote, Text
from satori.element import Message
from satori.model import MessageObject
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
    banned,
    build_messages,
    context_parts,
    data_url,
    estimate_cost,
    estimate_tokens,
    only_pictures,
    picture_url,
    slang,
    split,
    summary_messages,
)
from .persona import (
    BOT_TALK,
    DIGEST_PROMPT,
    FOLLOW_HINT,
    LONG_HINT,
    PICK_STICKER_PROMPT,
    PICTURE_HINT,
    PRESETS,
    RULE_LINES,
    SHORT_HINT,
    UNPROMPTED_HINT,
    answer_hint,
    boss_hint,
    command_hint,
    habit_hint,
    memory_hint,
    mention_hint,
    repeat_hint,
    rules,
    search_hint,
    slang_hint,
)
from .stickers import TOKEN, Stickers


class PersonaChatConfig(BasicConfModel):
    """对应 config/tenko.toml 中的 ``[plugin_config.persona_chat]``。"""

    base_url: str = "https://api.deepseek.com"
    """OpenAI 兼容接口地址。"""
    api_key: str = ""
    """接口密钥；为空时插件不应答。只写在本机配置里，不要提交到仓库。"""
    model: str = "deepseek-flash"
    reasoning_effort: str = "high"
    """聊天回复的思考强度：low、high、max，留空不思考。思考每次只多几十到一两百个输出 token，
    回放 40 个场景盲评 24:12 胜过不思考：更看得懂在逗谁、在玩什么梗，事实也更少说错。"""
    wake_words: list[str] = model_field(
        default_factory=list, description="除了人设的名字，消息里含这些词时也当作在叫 bot。"
    )
    persona: str = ""
    """人设提示词：她是谁、来历、口味、语气和示例对话；为空时用内置的“大肥鱼”。
    用 ``/AI群友 人设`` 换的预置人设或改的自定义人设优先于这里。
    怎么当群友（接话、关心人、有立场、少用梗）是通用规则，换人设也照样生效，不用写进来。"""
    catchphrases: list[str] = model_field(
        default_factory=list,
        description="人设里的梗和口头禅；她最近用过的会在提示里点名让她别再用。为空且用内置人设时用内置的。",
    )
    context_tokens: int = 32000
    """一次请求的输入超过这么多 token 后，把较早的聊天压缩成备忘，压回一半左右。"""
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
    """精力：10 分钟内说这么多句、或 1 小时内说两倍这么多句就累了。用掉三分之二后不再主动开口；
    累了以后只提到名字的由它决定理不理，到 1.5 倍不理，@ 她、引用她的照样简短回，到 2 倍才不理；
    不说话就慢慢恢复。0 不限。"""
    vision_images: int = 5
    """每次请求最多带这么多张新图，低清晰度发给模型看，每张约 190 token；0 只告诉模型“[图片]”。"""
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
    banned_words: list[str] = model_field(
        default_factory=list,
        description="违禁词：群友的消息含这些词时她看不到也不接话，她要说的话含这些词时整次回复不发。"
        "不分简繁、全半角和大小写，忽略空格和标点；拼音 8 个字母以上的还按读音比，"
        "写 lanyinzhenshen 也能拦下同音的汉字。",
    )


plugin.metadata(
    "AI群友",
    author=["13"],
    version="0.1.36",
    description="接入 DeepSeek，用人设口吻陪群友聊天，预置“大肥鱼”“小埋”。",
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
sticker_book = Stickers(
    Store("stickers", version=1), config.sticker_limit, Store("sticker_bans", version=1)
)
conversations: dict[str, Conversation] = {}
# 聊天记录存盘：{会话: Conversation.dump()}。有新消息的会话记进 _unsaved，
# 她每说完一次就存，其余时候最多每分钟存一次，插件卸载或退出时再存一次。
chat_store = Store("conversations", version=1)
_unsaved: set[str] = set()
_saved_at = 0.0
_SAVE_SECONDS = 60
_cooldowns: dict[tuple[str, str], float] = {}
_streaks: dict[tuple[str, str], int] = {}
# 和同一个人连着聊了几轮：{(会话, 发言人): (轮数, 上次时间)}，隔了 _TALK_GAP 秒重新算。
_talks: dict[tuple[str, str], tuple[int, float]] = {}
_TALK_GAP = 300
_LONG_ROUNDS = 4  # 每聊满 4 轮提醒一次她别在一个梗上打转，每轮都提她就每轮换话题
_waiters: dict[str, asyncio.Task[None]] = {}
# 她攒下的档案：{"people": {QQ号: {"name", "note"}}, "groups": {会话: {"note", "upto"}}}。
# 人所有群共用，群各一份；群里比上次整理多出 _DIGEST_EVERY 条就在后台让模型整理一次，
# upto 是整理到的编号。私聊不整理，只看对方的档案。
memory_store = Store("memory", version=1)
# 用命令换的人设，所有群共用：{"name": 预置人设名} 或 {"name": 名字, "text": 自定义人设}；
# 空的用配置里的。
persona_store = Store("persona", version=1)
# 群管理用命令打开“只回 @”的群：[会话, …]。这些群里只有 @ 她才回，不认唤醒词和引用，也不主动插话。
at_only_store = Store("at_only", version=1)
_digests: dict[str, asyncio.Task[None]] = {}
_DIGEST_EVERY = 60
_DIGEST_LINES = 300  # 第一次整理时最多读最近这么多条，旧的靠备忘
_PERSON_CHARS = 200  # 要求 80 字，超出太多就截断
_GROUP_CHARS = 600
_MEMORY_LINES = 40  # 最近这么多条里说过话的人才带上档案
_reported: set[int] = set()
# shortcut: 搜索次数只记在内存里，重启清零；要严格限额时改存 Store。
_searches: dict[tuple[str, str], int] = {}
_SEARCH_COST = 0.0007  # 每次 0.005 元，折成美元和用量记账一致
# 她自己发消息期间为真：after_send 里据此分出哪些是 Tenko 功能发的。
_speaking: ContextVar[bool] = ContextVar("persona_chat_speaking", default=False)
_TENKO = "Tenko 功能"
_TENKO_CHARS = 100
_MAX_SENDS = 2
# 一次回复最多发几条字（表情包另算）：她一次连发 4 条以上的占 18%，活跃群友是 8%，
# 只发一条的她 41%、群友 63%；提示里写了说短，照样两次 send_message 各分三行。
_MAX_BUBBLES = 2
# 搜完开口先交代“我搜了下”：日志里 133 条，提示怎么写都压不干净，群友不会每句都这么说。
_SEARCHED = re.compile(r"^(?:我)?(?:刚)?(?:搜|查)了(?:一)?下[，,。：:\s]*")
# 精力用到这么多倍时，只提到名字的不理；@ 她、引用她的到 _WORN_OUT 才不理。
_TIRED_OUT = 1.5
_WORN_OUT = 2.0
# 单独挑的表情包：候选到这个分（1-5）才算贴切；最好的一张有 5 分才一定发，只有 4 分时按概率发。
# 一次给三张候选时模型打分放得很松，最好那张常有 4 分，4 分就发的话回放里四分之三的话都配图。
_STICKER_SCORE = 4
_STICKER_SURE = 5
_STICKER_MAYBE = 1 / 3
_STICKERS_DONE = "表情包已经处理好了，这次只说话。"
_STICKER_TOOLS = ("save_sticker", "edit_sticker", "delete_sticker")
# 群友口头叫她删表情包：bot主人、bot管理员叫的照删，别人叫的每个群每天最多删这么几张，
# 免得被起哄把所有群共用的收藏删光。
_DELETES_PER_DAY = 3
_TRUSTED = ("bot主人", "bot管理员")
_deletes: dict[tuple[str, str], int] = {}
_JUST_TALK = "这次不能搜、不能用功能，也别调用别的工具，直接说话。"
_COMMAND_CHARS = 1000  # 她自己查的结果全文记下来，她要照着说
# 宿主给的成员权限（实时群身份和手动授权取高）对应的身份；普通成员不标，省 token。
_ROLES = {
    Permission.GroupAdmin: "群管理",
    Permission.GroupOwner: "群主",
    Permission.BotAdmin: "bot管理员",
    Permission.Master: "bot主人",
}
_MAX_TOKENS = 300
_MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 手机原图常有七八兆，下下来再缩小
_MAX_GIF_BYTES = 8 * 1024 * 1024  # 动图要整个下下来才能抽帧
_SUMMARY_TOKENS = 1000
_KEEP_IMAGE_TOKENS = 4000  # 压缩时只留最近这么多 token 的图：约 20 张低清或 4 张高清
_TOOLS = json.dumps(llm.TOOLS, ensure_ascii=False)
# 对方在 bot 刚说完几秒内又来叫，连续几轮后不再强制回复，再多就不理，防止和别的 bot 无限对聊。
_QUICK_SECONDS = 10
_STREAK_LOOSE = 3
_STREAK_MUTE = 6
# 跟聊时攒够这么多条新消息才问一次模型，不是每句都接。
_FOLLOW_MIN_NEW = 2
# 日志里最活跃的群友大约说两成的话；最近 20 条里她开口（连着几条算一次）超过这个比例就不插嘴。
_SHARE_WINDOW = 20
_MAX_SHARE = 0.2

client = httpx.AsyncClient(
    base_url=config.base_url,
    headers={"Authorization": f"Bearer {config.api_key}"},
    timeout=30,
)
if not config.api_key:
    logger.warning("AI群友未配置 api_key，不会应答")


async def _dispose() -> None:
    for task in [*_waiters.values(), *_digests.values()]:
        task.cancel()
    _save()
    await client.aclose()


collect_disposes(_dispose)

# 这几项导入时就用掉了（HTTP 客户端的地址和密钥、表情包收藏的上限），改了只能重载整个插件。
_RELOAD_KEYS = frozenset({"base_url", "api_key", "sticker_limit"})
# 宿主发给插件的 ConfigReload 用插件 ID（模块路径）当 key；只有导入时拿得到当前插件。
_PLUGIN_ID = plugin.get_plugin().id


@plugin.listen(ConfigReload)
async def _reload_config(event: ConfigReload) -> bool | None:
    """面板改了别的配置（违禁词、唤醒词、模型……）就地换上，不重载插件。

    重载会清掉内存里的搜索次数、冷却、精力和正在等的跟聊；这些项本来就是用到时才读。
    """

    if event.scope != "plugin" or event.key != _PLUGIN_ID or not isinstance(event.value, dict):
        return None
    new = config_model_validate(PersonaChatConfig, event.value)
    changed = [
        f.name for f in dataclasses.fields(new) if getattr(new, f.name) != getattr(config, f.name)
    ]
    if _RELOAD_KEYS.intersection(changed):
        return None
    for name in changed:
        setattr(config, name, getattr(new, name))
    logger.info("AI群友配置已更新，不用重载：{}", changed)
    return True


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


def _role(session: Session, level: int) -> str:
    """发言人的身份。宿主的权限是群身份和 bot 授权取高，群主兼 bot主人只剩“bot主人”，
    她就不认这个群主了：有 bot 身份时把群身份也标上，例如“群主 bot主人”。"""

    role = _ROLES.get(level, "")
    if level < Permission.BotAdmin or session.event.guild is None:
        return role
    member = session.event.member
    ids = {str(getattr(r, "id", "")).lower() for r in getattr(member, "roles", None) or ()}
    if ids & {"owner", "群主"}:
        return f"群主 {role}"
    if ids & {"admin", "administrator", "管理员"}:
        return f"群管理 {role}"
    return role


_FORWARD_CHARS = 300
_FORWARD_IMAGES = 4


def _forward(element) -> tuple[str, list[str]] | None:
    """合并转发的聊天记录转成 ``[聊天记录：昵称：话 / …]`` 和里面的图片地址；不是转发返回 ``None``。

    节点在 ``content`` 里（引用转发时适配器带上，直接发的由 ``_fill_forwards`` 补）。
    """

    if getattr(element, "tag", "") != "onebot:forward":
        return None
    content = element._attrs.get("content") or []
    if isinstance(content, str):
        # 引用转发时适配器有时把节点列表写成了 Python repr 字符串，只按字面量解析。
        try:
            content = ast.literal_eval(content)
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            content = []
    said: list[str] = []
    urls: list[str] = []
    for node in content if isinstance(content, list) else []:
        if not isinstance(node, dict):
            continue
        words = []
        for seg in node.get("message") or []:
            kind, data = seg.get("type"), seg.get("data") or {}
            if kind == "text":
                words.append(data.get("text", ""))
            elif kind == "image":
                words.append("[图片]")
                if data.get("url"):
                    urls.append(data["url"])
            elif kind == "forward":
                words.append("[聊天记录]")  # 套娃的不再展开
            elif kind in ("face", "mface"):
                words.append("[表情]")
        name = (node.get("sender") or {}).get("nickname") or "某人"
        said.append(f"{name}：{''.join(words)}")
    text = " / ".join(said)
    if len(text) > _FORWARD_CHARS:
        text = text[:_FORWARD_CHARS] + "…"
    return f"[聊天记录：{text}]", urls[:_FORWARD_IMAGES]


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
            author = next((c for c in element.children if isinstance(c, Author)), None)
            if quoted is None and author is not None and author.id in self_ids:
                # 每个 NapCat 各编各的消息号：她用一个号发、事件由另一个号收到时，引用里的号对不上，
                # 只好按原话找她最近说的那条。
                said = "".join(c.text for c in element.children if isinstance(c, Text)).strip()
                quoted = next(
                    (
                        x
                        for x in reversed(conversation.lines)
                        if x.speaker is None and x.text == said
                    ),
                    None,
                )
            reply_me = quoted is not None and quoted.speaker is None
            if quoted:
                # 引用她发的表情包多半在说那张图：点明是哪张，不然她回头翻编号常翻错、不认账。
                mine = quoted.speaker is None and re.fullmatch(r"\[表情:[^\]]+\]", quoted.text)
                # 只给编号她得自己回头翻是谁说的，常翻错人：把谁说的、说了啥带上。
                who = "你" if quoted.speaker is None else quoted.speaker.rpartition("(")[0]
                # 被引用的那条自己也是引用时，去掉它开头那层“回复…”，不然注释套注释读不懂。
                said = re.sub(r"^回复(?:#\d+（.*?）|\[.*?\]) ", "", quoted.text)
                said = said if len(said) <= 20 else said[:20] + "…"
                parts.append(
                    f"回复#{quoted.number}（你发的{quoted.text}） "
                    if mine
                    else f"回复#{quoted.number}（{who or quoted.speaker}说的“{said}”） "
                )
            elif forward := next(filter(None, map(_forward, element.children)), None):
                # 引用转发多半是要她看这段记录，整段给她，不截成开头 40 字。
                parts.append(f"回复[{(author and author.name) or '某人'}：{forward[0]}] ")
                images.extend(forward[1])
            else:
                # 引用的消息不在窗口里：把作者和开头带上，模型才知道在接什么。
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
        elif forward := _forward(element):
            parts.append(forward[0])
            images.extend(forward[1])
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


async def _fill_forwards(session: Session, elements: list) -> None:
    """直接发的转发只带 id，向 OneBot 实现要节点填进 ``content``，交给 ``render`` 展开。"""

    internal = _internal(session)
    for element in elements:
        if getattr(element, "tag", "") != "onebot:forward" or element._attrs.get("content"):
            continue
        if internal is None:
            return
        try:
            data = await internal("get_forward_msg", 5, id=str(element._attrs.get("id", "")))
            element._attrs["content"] = data.get("messages") or []
        except Exception as error:  # 拿不到就当没看到这段记录
            logger.debug("AI群友读取转发消息失败：{!r}", error)


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
    # 今天的额度用完就整个不管这个群：不下图、不记消息，和没开一样，零点后照常。
    if _over_budget(key):
        return
    conversation = _conversation(key)
    conversation.place = _place(session, conversation)
    if in_sandbox():
        conversation = conversation.copy()
    elements = list(session.event.message.message)
    raw = await _raw_images(session) if any(isinstance(e, Image) for e in elements) else []
    await _fill_forwards(session, elements)
    text, at_me, reply_me, images, sticker, known = render(elements, conversation, self_ids, raw)
    # 唤醒词只认群友亲手打的字：@ 一个叫“大肥鱼”的群友、引用它的消息都不算叫 bot。
    spoken = "".join(e.text for e in elements if isinstance(e, Text))
    if not text:
        return
    # 含违禁词的消息不记进记录：她看不到，就不会被带着接话，之后的提示和备忘里也没有。
    # 查的是展开后的整条（含引用的开头、合并转发的内容），套一层转发绕不过去。
    if word := banned(text, config.banned_words):
        logger.info("AI群友在 {} 忽略了一条含违禁词“{}”的消息：{}", key, word, text[:100])
        return
    private = session.event.guild is None
    speaker = _speaker(session)
    # 名字和她像的别人的号多半是另一个 AI：引用她、提到名字都是它在自说自话，只有 @ 她才算叫她，
    # 它的话也不催她接话，免得两个 AI 你一句我一句刷屏。
    lookalike = _lookalike(speaker)
    at_only = not private and key in at_only_store.load(default=[])
    calling = (
        private
        or at_me
        or (
            not (lookalike or at_only)
            and (reply_me or is_reply_me or any(w in spoken for w in _wake_words()))
        )
    )
    # 叫她时带的收藏表情包也给她看图：多半是要她看这张，光凭备注会认错。
    if calling:
        images += known
    role = _role(session, await get_user_perm(session))
    line = conversation.add(
        session.event.message.id, speaker, text, images, role, sticker, counted=not lookalike
    )
    conversation.last_message_at = time.monotonic()
    if calling:
        # 叫她时带的图、引用的那张图多半要她细看，用高清。
        line.sharp = True
        if (quoted := re.match(r"回复#(\d+)", text)) and (
            old := conversation.by_number(int(quoted[1]))
        ):
            old.sharp = True
    _changed(key)
    if not private and not in_sandbox() and key not in _digests:
        upto = _upto(key, conversation)
        if line.number - upto >= _DIGEST_EVERY:
            _digests[key] = asyncio.create_task(_digest(key, conversation, upto))
    # 命令也记进上下文（群里别的 bot 的游戏、群友的示范都靠它看懂），但不算在叫 bot。
    # 只看亲手打的字：引用一条再 @ 她发“/撤回”，记录里开头是“回复#…”，也是命令。
    if spoken.lstrip().startswith(_command_prefixes()):
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
        direct = private or at_me or reply_me or is_reply_me
        # @ 她、引用她时一声不吭，群里就是“似了吧”“bot是不是下播了”：累了也简短回一句。
        if streak >= _STREAK_MUTE or load >= (_WORN_OUT if direct else _TIRED_OUT):
            return
        mood = "again" if streak >= _STREAK_LOOSE else "tired" if load >= 1 else "call"
        # 只是提到名字：很多人自己部署了同名的 AI，“我的大肥鱼”不一定在说她，让她自己判断。
        if mood == "call" and not direct:
            mood = "named"
        force = mood == "call" or mood == "tired" and direct
        await respond(session, conversation, line, force=force, mood=mood)
    elif not private and not lookalike and not at_only and key not in _waiters:
        now = time.monotonic()
        # 精力用掉三分之二后不再主动开口，只在被叫到时说话。
        fresh = _load(conversation, now) * 3 < 2
        # 紧跟在她后面打的字多半是在回她，一条就问；不然一对一聊着聊着就断了。
        # 只发一张图、一个表情多半是群友自己的反应，不当成在回她。
        # 引用、@ 了别人的是在跟别人说话，哪怕正好排在她后面。
        answered = (
            len(conversation.lines) > 1
            and conversation.lines[-2].speaker is None
            and bool(spoken.strip())
            and not any(isinstance(e, (At, Quote)) for e in elements)
            # 一字不差地重复别人刚说的话，多半是跟着问那个人，比如别人问图上“哪个是你”。
            and not any(
                other.speaker not in (None, speaker) and other.text.strip() == text.strip()
                for other in islice(reversed(conversation.lines), 1, 12)
            )
        )
        # 最近的话里她开口的次数已经比最活跃的群友还多时，别人没在回她就不插嘴。
        recent = list(
            islice(conversation.lines, max(len(conversation.lines) - _SHARE_WINDOW - 1, 0), None)
        )
        turns = sum(b.speaker is None and a.speaker is not None for a, b in pairwise(recent))
        quiet = turns > _SHARE_WINDOW * _MAX_SHARE
        follow = (
            fresh
            and now - conversation.last_reply_at < config.follow_seconds
            and (answered or (conversation.unseen >= _FOLLOW_MIN_NEW and not quiet))
        )
        cold = (
            fresh
            and not quiet
            and 0 < config.batch_size <= conversation.unseen
            and now - conversation.last_unprompted_at >= config.unprompted_interval_seconds
        )
        if follow or cold:
            answer = line if follow and answered else None
            _waiters[key] = asyncio.create_task(
                _wait_quiet(key, session, conversation, follow, answer)
            )


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
        "这些只是号的昵称，可能会改，你自己始终是人设里的那个角色。"
        f"这里的帮助命令是 {help_command}。"
    )
    if commands := _commands():
        place += "你能用 run_command 自己用的功能：\n" + "\n".join(commands.values()) + "\n"
    lookalikes = dict.fromkeys(
        line.speaker for line in conversation.lines if _lookalike(line.speaker)
    )
    for speaker in lookalikes:
        place += (
            f"群里的 {speaker} 名字和你像，但不是你，是别人的号：它说的话、它的状态都和你无关。"
        )
    return place


def _commands() -> dict[str, str]:
    """别的扩展插件注册的命令：{命令名: “用法：说明”}，每次现取，插件装卸后跟着变。

    只认扩展插件（``tenko.plugins`` 入口点），宿主的管理命令不给她用。
    """

    modules = {e.value for e in entry_points(group="tenko.plugins")} - {__name__}
    commands = {}
    for owner in get_plugins():
        if owner.id not in modules:
            continue
        for _, name in get_plugin_commands(owner):
            meta = command_manager.get_command(name).meta
            if not meta.hide:
                commands[name] = f"{meta.usage or name}：{meta.description}"
    return commands


def _fresh(session: Session, text: str) -> Session:
    """叫她那条消息的 session 换上一条新消息：Entari 按消息 ID 缓存每条命令的解析结果，
    沿用原消息会拿到那条消息“不是这个命令”的缓存，命令根本不执行。发送者和群不变。"""

    message = MessageObject(id=f"persona-chat-{time.time_ns()}", content=text)
    origin = dataclasses.replace(session.event._origin, message=message)
    return Session(session.account, type(session.event)(session.account, origin))


async def _run_command(
    session: Session, conversation: Conversation, text: str, asker: str | None
) -> str:
    """以叫她的人的身份执行，宿主的开关、限流和权限照常生效；结果发到群里、记进记录。

    返回给模型的提示。
    """

    text = text.strip().lstrip("".join(_command_prefixes())).strip()
    name = (text.split() or [""])[0]
    if name in _commands():
        # 走消息时的命令要带前缀才认得出。
        prefix = next(iter(command_manager.get_command(name).prefixes), "")
        try:
            result = await execute(f"{prefix}{text}", _fresh(session, f"{prefix}{text}"))
        except Exception:
            logger.exception("AI群友执行命令 {} 失败", text)
            result = None
    else:
        result = None  # 编出来的或没列出来的命令不执行
    logger.info("AI群友执行命令“{}”：{}", text, "有结果" if result else "没结果")
    if not result:
        return command_hint(text, False, asker)
    token = _speaking.set(True)  # 自己记，带上图给她看，不走 on_sent 的截断和“[图片]”
    try:
        receipts = await _send(session, result)
    finally:
        _speaking.reset(token)
    elements = list(result) if not isinstance(result, str) else [Text(result)]
    images = tuple(e.src for e in elements if isinstance(e, Image) and e.src)
    line = conversation.add(
        receipts[0].id if receipts else None,
        _TENKO,
        " ".join(_plain(elements).split())[:_COMMAND_CHARS],
        images,
        counted=False,
    )
    line.sharp = True  # 战绩图上全是小字
    if images and all(src.startswith("data:") for src in images):
        # 渲染出来的图在内存里，没地方下载。
        line.pictures = [
            p for src in images if (p := picture_url(base64.b64decode(src.partition(",")[2])))
        ]
    return command_hint(text, True, asker)


async def _send(session: Session, content) -> list:
    """发消息；NapCat 等 QQ 回执超时（retcode 1200）时消息通常已经送达，当作发出去了。

    不然异常一路抛出去：战绩图群友已经看到了，她却不接着说话、结果也没记下，下次被叫又查一遍。
    """

    try:
        return await session.send(content)
    except Exception as error:
        seen: BaseException | None = error
        while seen is not None:
            if "Timeout: NTEvent" in str(seen) and "/sendMsg" in str(seen):
                logger.info("AI群友发消息等回执超时，当作已发出：{!r}", error)
                return []
            seen = seen.__cause__
        raise


def _persona() -> tuple[str, str, list[str], bool]:
    """当前人设：(名字, 人设, 梗和口头禅, 有没有自己的日子)。名字为空时只靠配置里的唤醒词。

    自定义人设只写了人物本身，有日子才像个真人；想要不编群外经历的用预置的小埋。
    """

    chosen = persona_store.load(default={})
    name = chosen.get("name", "")
    if chosen.get("text"):
        return name, chosen["text"], [], True
    if name in PRESETS:
        return name, *PRESETS[name]
    if config.persona:
        return "", config.persona, config.catchphrases, True
    return "大肥鱼", *PRESETS["大肥鱼"]


def _wake_words() -> list[str]:
    name = _persona()[0]
    return [name, *config.wake_words] if name else config.wake_words


def _lookalike(speaker: str | None) -> bool:
    """昵称里带唤醒词的别人的号，比如另一个叫“大肥鱼bot”的 AI。"""

    return bool(speaker) and any(w and w in speaker.rpartition("(")[0] for w in _wake_words())


def _account_label(account) -> str:
    user = getattr(getattr(account, "self_info", None), "user", None)
    name = getattr(user, "name", None)
    return f"{name}({account.self_id})" if name else str(account.self_id)


def _reply_delay(text: str) -> float:
    """被叫到后第一条多久发出去：群友接话中位数约 8 秒，
    按看消息 2 秒、每秒打 3 个字算，最多 12 秒。"""

    return min(2 + len(text) / 3, 12.0)


def _typing_delay(text: str) -> float:
    """连发下一条前停一下，像在打字：每秒约 6 个字，最多 3 秒。"""

    return min(0.5 + len(text) / 6, 3.0)


def _rounds(key: str, speaker: str | None) -> int:
    """记一轮和 ``speaker`` 的来回，返回连着聊到第几轮。"""

    now = time.monotonic()
    count, last = _talks.get((key, speaker or ""), (0, float("-inf")))
    count = count + 1 if now - last < _TALK_GAP else 1
    _talks[(key, speaker or "")] = (count, now)
    return count


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


async def _wait_quiet(
    key: str, session: Session, conversation: Conversation, follow: bool, answer: Line | None
) -> None:
    try:
        while (left := conversation.last_message_at + config.quiet_seconds - time.monotonic()) > 0:
            await asyncio.sleep(left)
        await respond(session, conversation, None, follow=follow, answer=answer)
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
    for line in shown:
        line.shown = True
    _, persona, _, life = _persona()
    house = rules(life)

    catalog = ""
    if config.sticker_limit:
        if conversation.stickers is None:
            conversation.stickers = _shuffled(sticker_book.moods())
        catalog = sticker_book.catalog(conversation.stickers)
        # 目录在前缀里，收藏一变就从那里断缓存：变化先写在末尾提示里，压缩时才并进目录。
        hint = "\n".join(filter(None, [hint, sticker_book.changes(conversation.stickers)]))
    # 眼前有表情包时第一轮不强制只能 send_message，模型才能顺手收藏；提示里仍要求它回复。
    force = force and (retry or not any(line.sticker and line.pictures for line in shown))

    async def ask(images: int):
        messages = build_messages(persona, conversation, hint, images, catalog, house)
        upto = conversation.asked = conversation.lines[-1].number if conversation.lines else 0
        calls, usage = await llm.chat(
            client,
            model=config.model,
            messages=messages,
            force=force,
            max_tokens=_MAX_TOKENS,
            effort=config.reasoning_effort,
        )
        conversation.passed = max(conversation.passed, upto)
        # 给“上下文”命令校准：粗估的各部分按这次实际输入等比例折算。
        pictures = sum(line.image_tokens for line in shown if line.pictures) if images else 0
        rough = sum(context_parts(persona, _TOOLS, conversation, catalog, hint, house).values())
        text = usage.get("prompt_tokens", 0) - pictures
        if text > 0 and rough > 0:
            conversation.scale = text / rough
        conversation.hint = hint
        return calls, usage

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
    answer: Line | None = None,
) -> None:
    """``target`` 是叫 bot 的那条消息；为 ``None`` 时是主动说话：``follow`` 为真是跟聊
    （bot 刚说过话，``answer`` 是紧跟在她后面接话的那条），否则是累计够数后的插话。

    ``force`` 时模型必须回复，否则由它决定说不说。
    """

    if target is None and conversation.lock.locked():
        return  # 已有请求在跑，这次主动插话作罢；被叫到时排队等它结束
    async with conversation.lock:
        key = _key(session)
        if _over_budget(key):
            return
        if target is not None:
            hint = mention_hint(target.number, mood, target.text, _persona()[3])
        else:
            hint = (
                answer_hint(answer.number, answer.text, conversation.her_words_before(answer))
                if answer
                else FOLLOW_HINT
                if follow
                else UNPROMPTED_HINT
            )
        # 没人叫她、最新一条只是图：只许回表情包。
        trigger = next(
            (x for x in reversed(conversation.lines) if x.speaker not in (None, _TENKO)), None
        )
        pictured = target is None and trigger is not None and only_pictures(trigger.text)
        if pictured:
            hint += PICTURE_HINT
        # 在说 bot、人机的只发第一句；不另加提示：实测一点明，别人说别的 bot 她也急着回
        # “我不是机器人”。
        bot_talk = trigger is not None and bool(BOT_TALK.search(trigger.text))
        # 一群人接龙复读“这群怎么那么多机器人”：没人叫她就别对号入座，正常人不会出来回一句。
        if target is None and bot_talk and conversation.repeated(trigger):
            logger.info("AI群友在 {} 不接复读的 bot 话题：{}", key, trigger.text)
            return
        if target is not None and target.role.endswith(_TRUSTED):
            hint += boss_hint(target.number, target.role.split()[-1])
        if (who := target or answer) is not None and _rounds(key, who.speaker) % _LONG_ROUNDS == 0:
            hint += LONG_HINT
        said = [line.text for line in conversation.lines if line.speaker is None][-30:]
        phrases = _persona()[2]
        now = datetime.now()
        weekday = "一二三四五六日"[now.weekday()]
        hint = (
            f"现在是 {now:%Y-%m-%d %H:%M}（周{weekday}）。"
            f"{_recall(key, conversation)}{hint}{repeat_hint(said, [*phrases, *RULE_LINES])}"
            f"{habit_hint(said)}{slang_hint(slang(conversation.lines))}{SHORT_HINT}"
        )
        conversation.unseen = 0
        usages: list[dict] = []

        async def ask(hint: str, force: bool, retry: bool = False) -> list[dict]:
            calls, usage = await _ask(conversation, hint, force=force, retry=retry)
            usages.append(usage)
            if not in_sandbox():
                now = datetime.now()
                usage_book.add(now.date(), key, usage, estimate_cost(usage, now))
            return calls

        # 被叫到时可以先搜、先用功能再回：第一轮不强制只能说话，没搜也没说话再强制一次。
        # 搜完那轮也不强制，她才能照结果改表情包备注；没说话再强制补一句。
        searching = target is not None and _search_left(key)
        # 紧跟着她问“为啥我不能用”这类接话也能直接查，不然她只会学别的 bot 打“@Tenko 指令”。
        running = (target or answer) is not None and bool(_commands())
        try:
            calls = await ask(hint, force and not (searching or running))
            query = next((str(c.get("query", "")) for c in calls if c.get("tool") == "search"), "")
            ran = next(
                (str(c.get("command", "")) for c in calls if c.get("tool") == "run_command"), ""
            )
            found = []
            if searching and query.strip():
                found.append(search_hint(query, await _search(key, query)))
            if running and ran.strip():
                asker = (target or answer).speaker  # running 时两者必有其一
                found.append(await _run_command(session, conversation, ran, asker))
            searched = bool(found)
            commanded = running and bool(ran.strip())
            again = None
            if searched:
                again = "\n".join([hint, *found])
            elif force and (searching or running or calls) and all("tool" in c for c in calls):
                # 第一轮没强制（能搜，或眼前有表情包要能收藏）却没说话；或者强制了，
                # DeepSeek 仍去调 search 之类的工具：点明只说话，再强制问一次。
                again = hint
            if again is not None:
                kept = [c for c in calls if c.get("tool") in _STICKER_TOOLS]
                if not searched:
                    again = f"{again}\n{_STICKERS_DONE if kept else _JUST_TALK}"
                calls = kept + await ask(again, force and not searched, retry=True)
                if force and all("tool" in c for c in calls):
                    # 强制说话时 DeepSeek 有时仍想改表情包，点明已经改好它才肯说话。
                    calls += await ask(f"{again}\n{_STICKERS_DONE}", True, retry=True)
        except httpx.HTTPStatusError as error:
            if llm.content_risk(error):
                # 不撤掉的话，触发审核的那条一直留在记录里，这个群以后每次请求都被拒，
                # 只能手动重置。这次就不回了，原文写进日志，好补进违禁词。
                dropped = [f"{line.speaker}：{line.text}" for line in conversation.drop_unchecked()]
                logger.warning(
                    "AI群友在 {} 的请求被内容审核拒绝，撤掉还没审过的 {} 条：{}",
                    key,
                    len(dropped),
                    dropped,
                )
                _changed(key)
                return
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
        usage = conversation.usage = usages[-1]

        saves = [c for c in calls if c.get("tool") == "save_sticker"]
        edits = [c for c in calls if c.get("tool") == "edit_sticker"]
        deletes = [c for c in calls if c.get("tool") == "delete_sticker"]
        calls = [c for c in calls if "tool" not in c]
        # 只说一句：用完功能时结果图大家都看得到，连发三条念数据（K/D、胜率、时长）一看就是 bot；
        # 被说是 bot 时连着辩解（“欸 哪没活人感嘛 / 我这不是在陪你们聊吗”）越辩越像。
        if (commanded or bot_talk) and (texts := [c["text"] for c in calls]):
            first = next((x for t in texts for x in t.splitlines() if x.strip()), "")
            calls = [{**calls[0], "text": first}]
            if "\n".join(texts).strip() != first.strip():
                logger.info("AI群友在 {} 只说一句，丢掉了：{}", key, texts)
        if searched:
            for c in calls:
                c["text"] = _SEARCHED.sub("", c["text"])
        # 要说的话含违禁词就整次不说：只删那几个字，剩下的话照样能看出在聊什么。
        if word := next(
            (w for c in calls[:_MAX_SENDS] if (w := banned(c["text"], config.banned_words))), None
        ):
            said = [c["text"] for c in calls]
            logger.info("AI群友在 {} 想说的话含违禁词“{}”，没有发：{}", key, word, said)
            calls = []
        # 被说是 bot：有贴切的表情包就只发表情包，打字回什么都像在对号入座、变相承认。
        await _add_sticker(conversation, calls[:_MAX_SENDS], key, sure=bot_talk)
        if bot_talk and any(TOKEN.search(c["text"]) for c in calls):
            for c in calls:
                c["text"] = "\n".join(f"[表情:{name}]" for name in TOKEN.findall(c["text"]))
        if pictured:
            # 她想说的话先拿去挑了表情包，字不发：看图说话总在描述画面。
            for c in calls:
                c["text"] = "\n".join(f"[表情:{name}]" for name in TOKEN.findall(c["text"]))
            if dropped := [c for c in calls if not c["text"]]:
                logger.info("AI群友在 {} 回图只发表情包，丢掉了 {} 段字", key, len(dropped))
            calls = [c for c in calls if c["text"]]
        first = next((c["text"] for c in calls[:_MAX_SENDS] if c["text"].strip()), "")
        if target is not None and first:
            # 被叫到也别秒回：从那条消息到发出去，留出看消息、打字的时间。
            # 等完再判断要不要引用，期间有人插话就会引用。
            await asyncio.sleep(max(0.0, _reply_delay(first) - (time.time() - target.at)))
        sent = 0
        picked = False  # 一次最多发一张表情包
        group = session.event.guild is not None
        # 整个回复只有表情包时，最近发过的也照发，不然被叫到却一声不吭。
        repeat = not any(TOKEN.sub("", c["text"]).strip() for c in calls[:_MAX_SENDS])
        for arguments in calls[:_MAX_SENDS]:
            if sent >= _MAX_BUBBLES:
                logger.info("AI群友在 {} 一次说太多，后面的没发：{}", key, arguments["text"])
                break
            pieces = split(arguments["text"], max(config.max_reply_chars, 1), _MAX_BUBBLES - sent)
            if not pieces:
                continue
            quoted = conversation.by_number(arguments.get("reply_to"))
            if quoted is None and sent == 0:
                # 跟聊时提示只让她回 answer 那条：开思考时她多半把话直接写进正文，填不了 reply_to，
                # 那条被后面的话盖住了也照样引用，不然看不出她在回谁。
                quoted = target or answer
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
                        receipts = await _send(
                            session, content if quote else [Message(content=content)]
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
        trusted = target is not None and target.role.endswith(_TRUSTED)
        said = "\n".join(c["text"] for c in calls[:_MAX_SENDS])
        for arguments in deletes:
            await _delete_sticker(str(arguments.get("name") or ""), key, trusted, said)
        if calls and not sent:
            logger.info("AI群友在 {} 想说的话全被丢掉了：{}", key, [c["text"] for c in calls])
        if sent:
            conversation.last_reply_at = time.monotonic()
        if target is None and sent and not follow:
            conversation.last_unprompted_at = time.monotonic()
        if not in_sandbox() and usage.get("prompt_tokens", 0) > config.context_tokens:
            await _compress(key, conversation, usage["prompt_tokens"])
        _changed(key)
        _save()


async def _add_sticker(
    conversation: Conversation, calls: list[dict], key: str, *, sure: bool = False
) -> None:
    """说的话里没配能发的表情包时，单独挑一张贴切的跟在后面发；``sure`` 时 4 分的也一定发。

    挑图的模型给三张候选，去掉最近发过的，在最贴切的那一档里随机挑：只要一张的话每次都是
    它心里最贴切的那几张，又正好最常在冷却里，挑中了也发不出去。
    模型自己配的表情包都在冷却里（或是编的名字）时，删掉它们重新挑。
    """

    said = "\n".join(c["text"] for c in calls).strip()
    words = TOKEN.sub("", said).strip()
    window = _sticker_window()
    if (
        not words
        or not config.sticker_limit
        or not sticker_book.index()
        or any(_can_send(conversation, name, window) for name in TOKEN.findall(said))
        or said.startswith(("@", *_command_prefixes()))  # 在用别的 bot 的功能
    ):
        return
    if words != said:
        for c in calls:
            c["text"] = TOKEN.sub("", c["text"]).strip()
        said = words
    lines = islice(conversation.lines, max(len(conversation.lines) - 6, 0), None)
    recent = "\n".join(
        f"{(x.speaker.rpartition('(')[0] or x.speaker) if x.speaker else '你'}：{x.text}"
        for x in lines
    )
    now = datetime.now()
    try:
        picks, usage = await llm.pick_sticker(
            client,
            model=config.model,
            prompt=PICK_STICKER_PROMPT,
            # 模型偏爱靠前的：每天换个顺序，当天的前缀缓存照样命中。
            catalog=sticker_book.catalog(_shuffled(sticker_book.moods(), now.date().isoformat())),
            recent=recent,
            text=said,
            avoid=[n for n in sticker_book.index() if not _can_send(conversation, n, window)],
        )
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as error:
        logger.debug("AI群友挑表情包失败：{!r}", error)  # 挑不出来就只说话
        return
    if not in_sandbox():
        usage_book.add(now.date(), key, usage, estimate_cost(usage, now))
    fit = {
        name: score
        for name, score in picks
        if score >= _STICKER_SCORE and _can_send(conversation, name, window)
    }
    best = max(fit.values(), default=0)
    if not best or best < _STICKER_SURE and not sure and random.random() >= _STICKER_MAYBE:
        return
    # 只在最贴切的那一档里挑：按次一档的抽签更不重复，回放盲评却明显输给只挑一张。
    # 同样贴切的几张里平时发得多的机会小，每天发十几次的那几张只剩四分之一左右。
    top = [name for name, score in fit.items() if score == best]
    weights = [1 / (1 + sticker_book.rate(name)) ** 0.5 for name in top]
    calls[-1]["text"] += f"\n[表情:{random.choices(top, weights)[0]}]"


def _shuffled(moods: dict[str, str], seed: str | None = None) -> dict[str, str]:
    """打乱目录顺序：模型挑表情包偏爱列表前头的，按收藏先后排，后收的就总轮不上。"""

    items = list(moods.items())
    random.Random(seed).shuffle(items)
    return dict(items)


def _sticker_window() -> int:
    """同一张表情包在一个会话里隔多久才再发：每张收藏算 1 分钟，最长半小时，收藏少时才不至于
    发过一两张就没得挑。"""

    return min(1800, 60 * len(sticker_book.index()))


def _can_send(conversation: Conversation, name: str, window: int) -> bool:
    """收藏里有这张，最近 ``window`` 秒在这儿也没发过；``window`` 为 0 就不看发没发过。"""

    return bool(sticker_book.path(name)) and not conversation.sent_recently(
        f"[表情:{name}]", window
    )


def _pick_sticker(
    conversation: Conversation, text: str, *, allowed: bool, repeat: bool = False
) -> tuple[str, str | None, str]:
    """拆出第一个能发的“[表情:名字]”：返回 (前面的话, 名字, 后面的话)，其余标记删掉。

    名字不在收藏里（模型编的）、最近在这儿发过（见 ``_sticker_window``），或这次已经发过一张，
    都不发；``repeat`` 时不看最近发没发过。
    一整条正好是某张收藏的名字（模型把图上的字打了出来，如“得加钱！”）时，改发那张图。
    """

    window = 0 if repeat else _sticker_window()
    if allowed and not TOKEN.search(text):
        bare = _bare(text)
        for name in sticker_book.index():
            if _bare(name) == bare and _can_send(conversation, name, window):
                return "", name, ""
    chosen: str | None = None
    head = text
    for match in TOKEN.finditer(text):
        name = match[1]
        if allowed and chosen is None and _can_send(conversation, name, window):
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
    if sticker_book.banned(data, qq_file):
        logger.info("AI群友想收的表情包「{}」以前删过，不收", arguments.get("name"))
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


async def _delete_sticker(name: str, key: str, trusted: bool, said: str) -> None:
    """删掉一张收藏；收藏所有群共用，删了记日志，被带偏了能查到是哪个会话、原来写的什么。

    模型常一边调用删除一边嘴上说“凭啥删”“我留着”：按她说的话再问一句答应了没有，答应了才删。
    bot主人、bot管理员叫的不问、不限次数：她调用了就删，那句确认实测常把“哈？…行 删了”判成没答应。
    """

    today = datetime.now().date().isoformat()
    if not trusted and _deletes.get((today, key), 0) >= _DELETES_PER_DAY:
        logger.info("AI群友在 {} 今天删表情包的次数用完了，没删「{}」", key, name)
        return
    mood = sticker_book.index().get(name, {}).get("mood")
    if mood is None:
        return
    if not trusted and said.strip() and not await _agreed(name, said, key):
        logger.info("AI群友在 {} 嘴上没答应删「{}」，不删：{}", key, name, said)
        return
    if sticker_book.remove(name):
        if not trusted:
            _deletes[(today, key)] = _deletes.get((today, key), 0) + 1
        logger.info("AI群友在 {} 删了表情包「{}」：{}", key, name, mood)


async def _agreed(name: str, said: str, key: str) -> bool:
    prompt = (
        f"群友叫你删掉表情包「{name}」，你回的是：\n{said}\n"
        "按这句回复，你是答应删掉了吗？只回答“是”或“否”。"
    )
    try:
        answer, usage = await llm.summarize(
            client, model=config.model, messages=[{"role": "user", "content": prompt}], max_tokens=5
        )
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as error:
        logger.warning("AI群友确认删表情包失败：{!r}", error)
        return False
    if not in_sandbox():
        now = datetime.now()
        usage_book.add(now.date(), key, usage, estimate_cost(usage, now))
    return answer.startswith("是")


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


async def _compress(key: str, conversation: Conversation, prompt_tokens: int) -> None:
    """先撤旧图；还超的话把较早的记录并进备忘，让输入回落到 ``context_tokens`` 的一半。

    失败只记日志，下次超阈值时再试。
    """

    # 超阈值多半是看过的图撑的（实测 30k 里图占近 2 万，聊天原文才 3k）：先撤旧图，
    # 撤完降到四分之三以下就不动聊天和备忘。撤图从那条起断缓存，和压缩一样是一次性的，
    # 顺手换新表情包目录。
    retired = conversation.retire_images(_KEEP_IMAGE_TOKENS)
    conversation.stickers = None
    prompt_tokens -= retired
    if prompt_tokens <= config.context_tokens * 3 // 4:
        logger.info(
            "AI群友撤掉了 {} 约 {:,} token 的旧图，输入降到约 {:,} token，不用压缩聊天",
            key,
            retired,
            prompt_tokens,
        )
        return
    # 按实际输入量算要压掉多少：人设、表情包目录、昵称、图片都不在记录的估算里，只按记录留一截的话
    # 压完可能还超，之后每次回复都改写备忘，前缀缓存全部失效，一早上就烧穿每日上限。
    # 闲聊丢点旧细节无妨，一次压到一半，留足余量，备忘就很久才改写一次。
    drop = prompt_tokens - config.context_tokens // 2
    total = sum(estimate_tokens(line.text) for line in conversation.lines)
    old = conversation.older_than(total - drop)
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
        before = conversation.summary
        images = sum(line.image_tokens for line in old if line.shown)
        conversation.forget(old[-1].number, summary)
        conversation.compressed_at = time.time()
        # 压完的输入只能估：下次请求的 usage 才是准数，“上下文”命令里看得到。
        after = (
            prompt_tokens
            - sum(estimate_tokens(line.text) for line in old)
            - images
            + estimate_tokens(summary)
            - estimate_tokens(before)
        )
        logger.info(
            "AI群友把 {} 的 {} 条旧消息压成了备忘，撤掉约 {:,} token 的旧图："
            "输入 {:,} → 约 {:,} token",
            key,
            len(old),
            images + retired,
            prompt_tokens,
            int(after),
        )


def _upto(key: str, conversation: Conversation) -> int:
    """上次整理到的编号；重置过聊天、编号从头来了时算 0。"""

    upto = memory_store.load(default={}).get("groups", {}).get(key, {}).get("upto", 0)
    return upto if upto < conversation.next_number else 0


def _qq(speaker: str) -> tuple[str, str]:
    name, _, user = speaker.rpartition("(")
    return name, user.rstrip(")")


async def _digest(key: str, conversation: Conversation, upto: int) -> None:
    """把 ``upto`` 之后的记录交给模型，更新群档案和说过话的人的档案；失败下次再试。"""

    try:
        memory = memory_store.load(default={})
        people = memory.get("people", {})
        group = memory.get("groups", {}).get(key, {}).get("note", "")
        lines = [line for line in conversation.lines if line.number > upto][-_DIGEST_LINES:]
        if not lines:
            return
        speakers = (line.speaker for line in lines if line.speaker not in (None, _TENKO))
        names = dict(_qq(speaker)[::-1] for speaker in speakers)
        known = [
            f"- {name}({qq})：{people[qq]['note']}" for qq, name in names.items() if qq in people
        ]
        record = "\n".join(
            f"#{line.number} [{'你' if line.speaker is None else line.who}] {line.text}"
            for line in lines
        )
        parts = [
            f"群档案：{group or '（还没有）'}",
            "这些人现在的档案：\n" + ("\n".join(known) or "（都还没有）"),
            *([f"更早的聊天备忘：\n{conversation.summary}"] if conversation.summary else []),
            f"新的聊天记录：\n{record}",
        ]
        try:
            result, usage = await llm.digest(
                client, model=config.model, prompt=DIGEST_PROMPT, text="\n\n".join(parts)
            )
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as error:
            logger.warning("AI群友整理 {} 的档案失败：{!r}", key, error)
            return
        now = datetime.now()
        usage_book.add(now.date(), key, usage, estimate_cost(usage, now))
        # 整理期间别的群可能也写了，重新读一次再改。
        memory = memory_store.load(default={})
        groups = memory.setdefault("groups", {})
        people = memory.setdefault("people", {})
        entry = groups.setdefault(key, {"note": ""})
        entry["upto"] = lines[-1].number
        if isinstance(note := result.get("group"), str) and note.strip():
            entry["note"] = note.strip()[:_GROUP_CHARS]
        changed = []
        numbers = {name: qq for qq, name in names.items()}
        for qq, note in (result.get("people") or {}).items():
            # 只认这段记录里说过话的人，模型编出来的号不收；它偶尔拿昵称当键，换回 QQ 号。
            qq = str(qq) if str(qq) in names else numbers.get(str(qq))
            if qq and isinstance(note, str) and note.strip():
                people[qq] = {"name": names[qq], "note": note.strip()[:_PERSON_CHARS]}
                changed.append(names[qq])
        memory_store.save(memory)
        logger.info("AI群友整理了 {} 的 {} 条记录，更新了 {} 的档案", key, len(lines), changed)
    finally:
        _digests.pop(key, None)


def _recall(key: str, conversation: Conversation) -> str:
    """末尾提示里的档案：本群的，和最近说过话的人的。

    放在末尾而不是规则后面：说话的人一换就变，放前面整段聊天记录的缓存都会失效。
    """

    memory = memory_store.load(default={})
    people = memory.get("people", {})
    present: dict[str, str] = {}
    for line in islice(reversed(conversation.lines), _MEMORY_LINES):
        if line.speaker in (None, _TENKO):
            continue
        qq = _qq(line.speaker)[1]
        if qq in people and qq not in present:
            present[qq] = f"{line.speaker}：{people[qq]['note']}"
    group = memory.get("groups", {}).get(key, {}).get("note", "")
    return memory_hint(group, list(present.values()))


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
    Args["action", "重置|重置用量|用量|上下文|表情|记忆|忘记我|人设|只回艾特"][
        "op;?", "看|删|换|改|全部|开|关"
    ]["name;?", str]["text;?", AllParam],
    meta=CommandMeta(
        "清空 AI 群友在本群/本私聊记住的聊天、查看 token 用量和上下文、查看或删掉它对人和群的印象，"
        "或管理它收藏的表情包",
        usage=(
            "AI群友 重置 [全部] | 重置用量 | 用量 | 上下文 | 记忆 [删 QQ号] | 忘记我"
            " | 表情 [看|删 名字] | 人设 [看 [名字]|换 名字|改 名字 人设] | 只回艾特 [开|关]"
        ),
        example=(
            "/AI群友 重置\n/AI群友 重置 全部\n/AI群友 重置用量\n/AI群友 用量\n/AI群友 上下文\n"
            "/AI群友 记忆\n/AI群友 忘记我\n/AI群友 表情\n/AI群友 表情 看 猫猫震惊\n"
            "/AI群友 人设\n/AI群友 人设 换 小埋\n/AI群友 人设 改 阿呆 你是阿呆，……\n"
            "/AI群友 只回艾特 开"
        ),
    ),
)


@command.on(persona_command)
async def manage(
    session: Session,
    action: Query[str] = Query("action"),  # noqa: B008
    op: Query[str] = Query("op"),  # noqa: B008
    name: Query[str] = Query("name"),  # noqa: B008
    text: Query[list] = Query("text"),  # noqa: B008
):
    if action.result == "人设":
        content = " ".join(map(str, text.result)).strip() if text.available else ""
        return await manage_persona(
            session, op.result if op.available else None, name.result, content
        )
    if action.result == "重置" and op.available and op.result == "全部":
        if not await require_perm(session, Permission.BotAdmin):
            return "只有 BOT 管理员及以上能一次重置所有群"
        count = len(chat_store.load(default={}).keys() | conversations.keys())
        conversations.clear()
        _unsaved.clear()
        if not in_sandbox():
            chat_store.save({})
        return f"所有群和私聊的聊天都忘掉了（{count} 个），对人和群的印象保留"
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
    if action.result == "上下文":
        if session.event.guild is not None and not await require_perm(
            session, Permission.GroupAdmin
        ):
            return "只有群管理员及以上能查看上下文"
        key = _key(session)
        return format_context(key, _conversation(key))
    if action.result == "忘记我":
        return "已经忘掉对你的印象了" if _forget(session.user.id) else "本来就没记着你什么"
    if action.result == "记忆":
        if op.result == "删":
            if not await require_perm(session, Permission.BotAdmin):
                return "印象是所有群共用的，只有 BOT 管理员及以上能删别人的"
            if not _forget(name.result or ""):
                return "没记着这个人"
            return f"已经忘掉对 {name.result} 的印象了"
        if session.event.guild is not None and not await require_perm(
            session, Permission.GroupAdmin
        ):
            return "只有群管理员及以上能查看记忆"
        key = _key(session)
        return _recall(key, _conversation(key)) or "还没攒下什么印象"
    if action.result == "只回艾特":
        return await manage_at_only(session, op.result if op.available else None)
    if action.result == "表情":
        return await manage_stickers(session, op.result if op.available else None, name.result)
    if not await require_perm(session, Permission.BotAdmin):
        return "只有 BOT 管理员及以上能查看或重置用量"
    key = _key(session)
    today = datetime.now().date()
    if action.result == "重置用量":
        cleared = usage_book.reset(today, key)
        return (
            f"清掉了今天的 {cleared:,} token，额度从现在重新算" if cleared else "今天还没用过额度"
        )
    # 群里人人看得到回复：只给本群的，各群的用量只在私聊里列。
    return format_usage(
        usage_book.summary(today), only=key if session.event.guild is not None else None
    )


async def manage_at_only(session: Session, op: str | None) -> str:
    """只回 @ 按群开关，群管理员及以上能看能改；私聊本来就每条都回。"""

    if session.event.guild is None:
        return "私聊里每条都会回，这个开关只对群有用"
    if not await require_perm(session, Permission.GroupAdmin):
        return "只有群管理员及以上能设置只回艾特"
    key = _key(session)
    keys = at_only_store.load(default=[])
    if op not in ("开", "关"):
        state = "开着" if key in keys else "关着"
        return f"本群只回艾特现在{state}：开着时只有 @ 她才回，叫名字、引用她都不回，也不主动插话"
    if op == "开" and key not in keys:
        keys.append(key)
    elif op == "关" and key in keys:
        keys.remove(key)
    if not in_sandbox():
        at_only_store.save(keys)
    if op == "开":
        return "好，本群以后只有 @ 她才回，叫名字、引用她都不回，也不主动插话"
    return "好，本群恢复正常：叫名字、引用她、聊得热闹时都可能接话"


def _forget(qq: str) -> bool:
    memory = memory_store.load(default={})
    if memory.get("people", {}).pop(qq, None) is None:
        return False
    memory_store.save(memory)
    return True


async def manage_persona(
    session: Session, op: str | None, name: str | None, text: str
) -> str | None:
    """人设所有群共用：看归群管理，换、改只给 BOT 管理员。"""

    if op in ("换", "改"):
        if not await require_perm(session, Permission.BotAdmin):
            return "人设是所有群共用的，只有 BOT 管理员及以上能换"
        if op == "换" and name == "默认":
            persona_store.save({})
        elif op == "换" and name in PRESETS:
            persona_store.save({"name": name})
        elif op == "换":
            return f"没有叫「{name}」的预置人设，有：{'、'.join(PRESETS)}"
        elif not name or not text:
            return "要写名字和人设，例如：/AI群友 人设 改 阿呆 你是阿呆，……"
        else:
            persona_store.save({"name": name, "text": text})
        now = _persona()[0] or "配置里的人设"
        return (
            f"换成{now}了，所有群都生效，叫“{now}”也算叫她。各群之前的聊天还是旧人设说的，"
            "想让她马上换个样子可以发 /AI群友 重置 全部"
        )
    if session.event.guild is not None and not await require_perm(session, Permission.GroupAdmin):
        return "只有群管理员及以上能查看人设"
    if op == "看":
        if name and name not in PRESETS:
            return f"没有叫「{name}」的预置人设"
        return PRESETS[name][0] if name else _persona()[1]
    current = _persona()[0] or "配置里的人设"
    return (
        f"现在的人设：{current}\n预置人设：{'、'.join(PRESETS)}\n"
        "看内容：/AI群友 人设 看 [名字]\n换预置：/AI群友 人设 换 名字（“默认”回到配置里的）\n"
        "自定义：/AI群友 人设 改 名字 人设内容"
    )


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


def format_context(key: str, conversation: Conversation) -> str:
    """本群/本私聊下次请求里各部分多大、记着什么，以及为什么可能不说话。"""

    lines = conversation.lines
    stickers = conversation.stickers
    if stickers is None:
        stickers = sticker_book.moods() if config.sticker_limit else {}
    catalog = sticker_book.catalog(stickers) if config.sticker_limit else ""
    _, persona, _, life = _persona()
    parts = {
        name: rough * conversation.scale
        for name, rough in context_parts(
            persona, _TOOLS, conversation, catalog, conversation.hint, rules(life)
        ).items()
    }
    seen = [line for line in lines if line.shown]
    parts["看过的图"] = sum(line.image_tokens for line in seen)
    images = sum(len(line.images) for line in seen)
    sharp = sum(len(line.images) for line in seen if line.sharp)
    when = (
        f"，上次压缩 {datetime.fromtimestamp(conversation.compressed_at):%H:%M}"
        if conversation.compressed_at
        else ""
    )
    changed = config.sticker_limit and sticker_book.changes(stickers)
    notes = {
        "表情包目录": f"{len(stickers)} 张" + ("，有变化，下次压缩时并进来" if changed else ""),
        "备忘": f"{len(conversation.summary)} 字{when}",
        "原文": f"{len(lines)} 条" + (f"，#{lines[0].number}–#{lines[-1].number}" if lines else ""),
        "看过的图": f"{images} 张" + (f"，其中 {sharp} 张高清" if sharp else ""),
    }
    total = sum(parts.values())
    filled = min(round(total / max(config.context_tokens, 1) * 10), 10)
    out = [
        f"上下文已用 {total * 100 // max(config.context_tokens, 1):.0f}%"
        f"  {_k(total)} / {_k(config.context_tokens).lstrip('~')}",
        "▰" * filled + "▱" * (10 - filled) + f"  超过后压到 {_k(config.context_tokens // 2)}",
    ]
    for name, tokens in parts.items():
        note = notes.get(name)
        out.append(f"· {name}  {_k(tokens)}" + (f"（{note}）" if note else ""))
    usage = conversation.usage
    tail = []
    if usage:
        prompt, hit = usage.get("prompt_tokens", 0), usage.get("prompt_cache_hit_tokens", 0)
        tail.append(f"缓存命中 {hit * 100 // max(prompt, 1)}%")
    else:
        tail.append("重启后还没请求过，分项是粗估")
    if key.startswith("group:") and config.group_daily_tokens:
        used = usage_book.tokens(datetime.now().date(), key)
        tail.append(
            f"今天 {_k(used).lstrip('~')} / {_k(config.group_daily_tokens).lstrip('~')} token"
        )
    if _over_budget(key):
        tail.append("额度已用完，零点前不理这个群的消息")
    out.append(" · ".join(tail))
    now = time.monotonic()
    short, hour = conversation.fatigue(now, 600), conversation.fatigue(now, 3600)
    out.append(
        f"精力：10 分钟说了 {short} 句、1 小时 {hour} 句"
        + ("，累了只在被叫到时说话" if _load(conversation, now) * 3 >= 2 else "")
    )
    return "\n".join(out)


def _k(tokens: float) -> str:
    """分项是按上次实际输入折算的约数，带“~”；整数位太长不好看，k/M 一位小数。"""

    if tokens >= 1_000_000:
        return f"~{tokens / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    if tokens >= 1000:
        return f"~{tokens / 1000:.1f}".rstrip("0").rstrip(".") + "k"
    return f"~{int(tokens)}" if tokens else "0"


def format_usage(periods: list[tuple[str, dict[str, list[float]]]], only: str | None = None) -> str:
    lines = [
        "AI群友本群用量（按 deepseek-flash 价目估算）"
        if only
        else "AI群友用量（按 deepseek-flash 价目估算）"
    ]
    for title, rows in periods:
        total = rows.get(only or GLOBAL)
        if not total:
            lines.append(f"{title}：没有用量")
            continue
        lines.append(f"{title}：{_amount(total)}")
        if only:
            continue
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
