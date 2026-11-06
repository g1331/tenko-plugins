"""入群审核：申请时把申请者填的 EA 名字查一遍发到群里，管理员回复这条消息同意或拒绝。

审核开着时，绑定过 EA 号的人进群后自动把群名片改成 EA 名字。
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from arclet.entari import MessageChain, MessageCreatedEvent, Session
from arclet.entari.event.base import GuildMemberAddedEvent
from loguru import logger
from satori import At, Author, Element, Quote, Text
from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column
from tenko_api.accounts import Account, accounts_for_group, online_accounts
from tenko_api.features import feature_enabled
from tenko_api.perm import Permission, require_perm
from tenko_api.storage import Model, get_session

from . import api, binding
from .api import Bf1Error
from .reply import as_elements
from .stats import recent_card, stats_card

_REVIEW_HINT = "回复本消息「y」同意，回复其他文字作为理由拒绝"


class JoinReview(Model):
    """开了入群审核的群。"""

    group_id: Mapped[str] = mapped_column(String(32), primary_key=True)


class JoinRequest(Model):
    """待审的入群申请。

    不设时限：flag 是 QQ 群通知的 seq，NapCat 处理时在收到申请那个号最近 100 条群通知里找，
    申请还在通知里就能处理，bot 重启也不影响。处理掉或者申请者已经进群才删。
    """

    group_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    applicant: Mapped[str] = mapped_column(String(32), primary_key=True)
    # 每个管理员号的群通知各编各的 seq，得用收到申请的那个号处理
    self_id: Mapped[str] = mapped_column(String(32))
    flag: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[float]
    # 查到的 EA 号，同意时给申请者绑定
    pid: Mapped[int | None] = mapped_column(BigInteger)
    name: Mapped[str | None] = mapped_column(String(64))


# 认领申请、管理员抢着回复时，查和写之间不能插进别的协程
_request_lock = asyncio.Lock()


# —— 开关 ——


async def _review_on(group_id: str) -> bool:
    async with get_session() as db:
        return await db.get(JoinReview, group_id) is not None


async def switch(group_id: str, value: str | None) -> str:
    """value 是「开」「关」；不给时报告当前状态。"""

    if value is None:
        return f"本群入群审核：{'开' if await _review_on(group_id) else '关'}"
    async with get_session() as db:
        row = await db.get(JoinReview, group_id)
        if value == "开" and row is None:
            db.add(JoinReview(group_id=group_id))
        elif value == "关" and row is not None:
            await db.delete(row)
        await db.commit()
    return f"本群入群审核已{value}"


# —— 收到申请：查人、发审核消息 ——


def _join_answer(comment: str) -> str:
    """QQ 把入群问题拼成「问题：…\\n答案：…」；没设问题时整段附言就是答案。"""

    return comment.rpartition("答案：")[2].strip()


async def _verdict(applicant: str, persona: dict[str, Any], stats: Any, ban: Any) -> str:
    if not stats["summary"].get("time_played_seconds"):
        return "有这个 EA 账号，但没玩过战地一"
    ban = ban or {}
    notes = [
        f"{source.upper()} 实锤 {ban.get(f'{source}_url') or ''}".rstrip()
        for source in ("bfeac", "bfban")
        if ban.get(source) == "hit"
    ]
    bound = await binding.bound(applicant)
    if bound and bound.pid != persona["persona_id"]:
        notes.append(f"这个 QQ 绑定的是 {bound.name or f'PID {bound.pid}'}，不是同一个号")
    return "；".join(notes) or "有效 ID"


async def review_request(
    applicant: str, nickname: str, comment: str, service
) -> tuple[list[Element], dict[str, Any] | None]:
    """审核消息和查到的玩家。审核消息第一行带申请者 QQ，管理员回复时靠它找回申请。"""

    answer = _join_answer(comment)
    persona = card = recent = None
    rounds_note = ""
    if not answer:
        verdict = "没填 EA 名字"
    else:
        try:
            persona = await api.find_persona(answer)
            (card, stats, ban), recent = await asyncio.gather(
                stats_card(persona, service), _optional_recent(persona, service)
            )
        except Bf1Error as error:
            verdict = str(error)
        else:
            verdict = await _verdict(applicant, persona, stats, ban)
            if recent is None and stats["summary"].get("time_played_seconds"):
                rounds_note = "最近对局：bf-manager 90 天内没记录到\n"
    head = (
        f"收到 {nickname}({applicant}) 的入群申请\n答案：{answer or '-'}\n检查：{verdict}\n"
        + rounds_note
    )
    parts = [Text(head), *as_elements(card)]
    if recent is not None:
        # 两张都回退成文字时中间空一行；战绩卡片已经带了玩家页链接，对局卡片不再带
        gap = "\n\n" if isinstance(card, str) and isinstance(recent, str) else ""
        parts += as_elements(f"{gap}{recent}" if isinstance(recent, str) else recent)
    return [*parts, Text(f"\n{_REVIEW_HINT}")], persona


async def _optional_recent(persona: dict[str, Any], service) -> MessageChain | str | None:
    """审核时对局记录查不到不耽误战绩卡片。"""

    try:
        return await recent_card(persona, service, link=False)
    except Bf1Error:
        return None


async def _claim(group_id: str, applicant: str, self_id: str, flag: str) -> bool:
    """同一份申请每个号各收到一次；几秒内已经有号接手的就不再处理。重复申请照常处理。"""

    async with _request_lock, get_session() as db:
        pending = await db.get(JoinRequest, (group_id, applicant))
        if pending is not None and pending.created_at > time.time() - 30:
            return False
        await db.merge(
            JoinRequest(
                group_id=group_id,
                applicant=applicant,
                self_id=self_id,
                flag=flag,
                created_at=time.time(),
                pid=None,
                name=None,
            )
        )
        await db.commit()
    return True


async def _remember_persona(group_id: str, applicant: str, persona: dict[str, Any]) -> None:
    async with get_session() as db:
        if pending := await db.get(JoinRequest, (group_id, applicant)):
            pending.pid, pending.name = persona["persona_id"], persona["display_name"]
            await db.commit()


async def _drop_request(group_id: str, applicant: str) -> None:
    async with get_session() as db:
        if pending := await db.get(JoinRequest, (group_id, applicant)):
            await db.delete(pending)
            await db.commit()


# —— 真进群：改群名片 ——


async def _onebot(account: Account, action: str, **params: Any) -> Any:
    """OneBot 原始接口；失败时 satori 抛 ActionFailed。

    直接走 ``call_api``：Entari 包装 ``protocol.internal`` 时会把关键字参数整个包成一项
    ``kwargs``，NapCat 收不到参数。
    """

    # shortcut: 绕开 Entari 的包装缺陷，ArcletProject/Entari#32 修复后改回 protocol.internal。
    return await asyncio.wait_for(account.protocol.call_api(f"internal/{action}", params), 10)


async def rename_member(group: str, user_id: str, accounts: list[Account]) -> str | None:
    """进群的人绑定过 EA 号，就把群名片改成它现在的名字；改成了返回名字。

    进群通知只交给选路选中的一个号，它不一定是管理员，所以挨个问账号在群里的身份，
    用第一个群主或管理员号改。
    """

    bound = await binding.bound(user_id)
    if bound is None:
        return None
    try:
        name = (await api.get(f"/players/{bound.pid}"))["display_name"]
    except Bf1Error:
        name = bound.name
    if not name:
        return None
    for account in accounts:
        try:
            me = await _onebot(
                account,
                "get_group_member_info",
                group_id=int(group),
                user_id=int(account.self_id),
                no_cache=True,
            )
            if (me or {}).get("role") not in ("owner", "admin"):
                continue
            await _onebot(
                account, "set_group_card", group_id=int(group), user_id=int(user_id), card=name
            )
        except Exception as error:  # 掉线、超时、不是 QQ 群：换下一个号
            logger.warning("用 {} 改 {} 的群名片失败：{!r}", account.self_id, user_id, error)
            continue
        return name
    logger.info("群 {} 里没有能改名片的管理员号，{} 的名片没改", group, user_id)
    return None


async def on_join(session: Session[GuildMemberAddedEvent], service) -> None:
    """加群申请和真进群都从这里进来。"""

    # shortcut: satori 的 onebot11 适配器把加群申请（request.group.add）也报成 GUILD_MEMBER_ADDED，
    # 只能靠带不带 message（flag 和附言）区分真入群；上游改成 GUILD_MEMBER_REQUEST 后改听那个事件
    request = getattr(session.event._origin, "message", None)
    group, applicant = session.guild.id, session.user.id
    if request is None:
        # 真进群了：在 QQ 里直接同意的申请不会再有人回复，这里清掉
        await _drop_request(group, applicant)
        if feature_enabled("bf1", group) and await _review_on(group):
            receiver = session.account
            others = accounts_for_group(group, platform=receiver.platform)
            accounts = [receiver, *(a for a in others if str(a.self_id) != str(receiver.self_id))]
            if name := await rename_member(group, applicant, accounts):
                await session.send(
                    MessageChain([At(applicant), Text(f" 已自动将你的群名片改为 {name}")])
                )
        return
    if not feature_enabled("bf1", group) or not await _review_on(group):
        return
    # QQ 把申请推给群里每个管理员号，宿主也都放行，第一个收到的处理
    if not await _claim(group, applicant, str(session.account.self_id), request.id):
        return
    message, persona = await review_request(
        applicant, session.user.name or applicant, request.content, service
    )
    if persona:
        await _remember_persona(group, applicant, persona)
    await session.send(message)


# —— 管理员回复审核消息 ——


def _quoted_applicant(elements: list[Element], self_ids: set[str]) -> str | None:
    """回复的是 bot 发的审核消息时，取出申请者 QQ。

    每个 NapCat 各编各的消息号，事件可能由另一个号收到，所以不比消息号，看引用里的原文。
    """

    quote = next((e for e in elements if isinstance(e, Quote)), None)
    if quote is None:
        return None
    author = next((c for c in quote.children if isinstance(c, Author)), None)
    if author is None or author.id not in self_ids:
        return None
    said = "".join(c.text for c in quote.children if isinstance(c, Text))
    found = re.match(r"收到 .*\((\d+)\) 的入群申请", said)
    return found[1] if found else None


async def decide(group: str, applicant: str, said: str) -> str:
    """管理员的回复：y 同意，其他文字作为理由拒绝。"""

    async with _request_lock, get_session() as db:
        pending = await db.get(JoinRequest, (group, applicant))
        if pending is None:
            return f"{applicant} 的入群申请已经处理过了"
        account = next((a for a in online_accounts() if str(a.self_id) == pending.self_id), None)
        if account is None:
            return f"收到申请的号 {pending.self_id} 不在线，上线后再回复"
        flag, pid, name = pending.flag, pending.pid, pending.name
        # 先删再调接口：失败多半是申请在 QQ 里已经处理过了，留着也没用
        await db.delete(pending)
        await db.commit()
    approve = said.casefold() == "y"
    try:
        await account.protocol.guild_member_approve(flag, approve, "" if approve else said)
    except Exception as error:  # NapCat 的报错（不是管理员、申请已被别人处理）原样告诉管理员
        logger.warning("处理入群申请失败：{!r}", error)
        return f"处理 {applicant} 的入群申请失败：{error}"
    if not approve:
        return f"已拒绝 {applicant} 的入群申请，理由：{said}"
    if pid is not None and not await binding.bound(applicant):
        await binding.bind(applicant, {"persona_id": pid, "display_name": name})
    return f"已同意 {applicant} 的入群申请"


async def on_reply(session: Session[MessageCreatedEvent]) -> None:
    """群里每条消息都过一遍：只处理群管理员回复审核消息的。"""

    if session.event.guild is None:
        return
    message = list(session.event.message.message)
    self_ids = {str(a.self_id) for a in online_accounts()} | {str(session.account.self_id)}
    applicant = _quoted_applicant(message, self_ids)
    if applicant is None:
        return
    async with get_session() as db:
        if await db.get(JoinRequest, (session.guild.id, applicant)) is None:
            return
    said = "".join(e.text for e in message if isinstance(e, Text)).strip()
    if not said or not await require_perm(session, Permission.GroupAdmin):
        return
    await session.send(await decide(session.guild.id, applicant, said))
