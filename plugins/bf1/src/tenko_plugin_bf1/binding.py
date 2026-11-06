"""QQ 号和 EA 账号的绑定，以及“这条命令查的是谁”。"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from loguru import logger
from sqlalchemy import BigInteger, String, select
from sqlalchemy.orm import Mapped, mapped_column
from tenko_api.storage import Model, Store, get_session

from . import api
from .api import Bf1Error, NoSuchPlayer


class Binding(Model):
    """QQ 号 → 绑定的 EA 账号（按 PID 查，改名不影响）。"""

    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    pid: Mapped[int] = mapped_column(BigInteger)
    # 从旧 bot 迁过来的绑定有些没有名字
    name: Mapped[str | None] = mapped_column(String(64))


# 0.1.4–0.1.5 把绑定存在这个 JSON 里，第一次用到绑定时导入数据库
_legacy = Store("bindings", version=1)
_legacy_lock = asyncio.Lock()


async def _import_legacy() -> None:
    if not _legacy.path.exists():
        return
    async with _legacy_lock:
        if not _legacy.path.exists():
            return
        data = _legacy.load(default={})
        async with get_session() as db:
            existing = set(await db.scalars(select(Binding.user_id)))
            db.add_all(
                Binding(user_id=user_id, pid=bound["pid"], name=bound.get("name"))
                for user_id, bound in data.items()
                if user_id not in existing
            )
            await db.commit()
        _legacy.path.rename(_legacy.path.with_name(f"{_legacy.path.name}.imported"))
        logger.info("已把 {} 条绑定从 {} 导入数据库", len(data), _legacy.path.name)


async def bound(user_id: str) -> Binding | None:
    await _import_legacy()
    async with get_session() as db:
        return await db.get(Binding, user_id)


async def bind(user_id: str, persona: dict[str, Any]) -> None:
    await _import_legacy()
    async with get_session() as db:
        await db.merge(
            Binding(user_id=user_id, pid=persona["persona_id"], name=persona["display_name"])
        )
        await db.commit()


_BIND_HINT = "使用 /bf1绑定 你的EA名字，或者在命令后面带上名字"
# 群名片开头的 [战队]、【战队】、(xx) 这类前缀
_CARD_PREFIX = re.compile(r"^(?:\s*[\[【(（<《「『][^\]】)）>》」』]*[\]】)）>》」』])+\s*")
# EA 名字只有字母、数字、- 和 _
_EA_NAME = re.compile(r"[A-Za-z0-9_-]{2,32}")


def card_name(card: str | None) -> str | None:
    """群名片去掉前缀后像 EA 名字就用它，不像（中文、带空格）返回 None。"""

    name = _CARD_PREFIX.sub("", card or "").strip()
    return name if _EA_NAME.fullmatch(name) else None


async def find_player(name: str | None, user_id: str, card: str | None = None) -> dict[str, Any]:
    """带名字按名字查，#PID 按 PID 查；不带就查发消息的人绑定的号（按 PID，改名也不影响），
    没绑定时把群名片当 EA 名字查（只认精确同名）。
    """

    if name and name.startswith("#"):
        if not name[1:].isdigit():
            raise Bf1Error("PID 只能是数字，例如 #1004198901469")
        return await api.get(f"/players/{name[1:]}")
    if name:
        return await api.find_persona(name)
    found = await bound(user_id)
    if found:
        return await api.get(f"/players/{found.pid}")
    guess = card_name(card)
    if not guess:
        raise Bf1Error(f"还没绑定：{_BIND_HINT}")
    try:
        return await api.find_persona(guess)
    except NoSuchPlayer as error:
        raise Bf1Error(f"还没绑定，按群名片 {guess} 查：{error}。{_BIND_HINT}") from error


async def bind_reply(name: str | None, user_id: str) -> str:
    if not name:
        current = await bound(user_id)
        if not current:
            return "用法：/bf1绑定 <EA 名字>"
        return f"当前绑定：{current.name or '名字未知'}（PID {current.pid}）"
    persona = await api.find_persona(name)
    before = await bound(user_id)
    await bind(user_id, persona)
    pid, new = persona["persona_id"], persona["display_name"]
    reply = f"已绑定 {new}（PID {pid}）"
    if before is None:
        return reply
    old = before.name or "名字未知"
    if before.pid != pid:
        return f"绑定变更：{old}（PID {before.pid}）→ {new}（PID {pid}）"
    if before.name and before.name != new:
        return f"{reply}，改过名：{old} → {new}"
    return reply
