"""QQ 号和 EA 账号的绑定，以及“这条命令查的是谁”。

BF6 按 nucleus_id（EA 账号 ID）查，和战地一插件的 persona 绑定不通用，各绑各的。
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column
from tenko_api.storage import Model, get_session

from . import api
from .api import Bf6Error, NoSuchPlayer


class Binding(Model):
    """QQ 号 → 绑定的 EA 账号。名字、平台和头像是绑定时的，查询时会按名字刷新。"""

    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    nucleus_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(64))
    platform: Mapped[str | None] = mapped_column(String(16))
    avatar_url: Mapped[str | None] = mapped_column(String(256))

    def player(self) -> dict[str, Any]:
        return {
            "nucleus_id": self.nucleus_id,
            "display_name": self.name,
            "platform": self.platform,
            "avatar_url": self.avatar_url,
        }


async def bound(user_id: str) -> Binding | None:
    async with get_session() as db:
        return await db.get(Binding, user_id)


async def bind(user_id: str, player: dict[str, Any]) -> None:
    async with get_session() as db:
        await db.merge(
            Binding(
                user_id=user_id,
                nucleus_id=player["nucleus_id"],
                name=player["display_name"],
                platform=player.get("platform"),
                avatar_url=player.get("avatar_url"),
            )
        )
        await db.commit()


_BIND_HINT = "使用 /bf6绑定 你的EA名字，或者在命令后面带上名字"
# 群名片开头的 [战队]、【战队】、(xx) 这类前缀
_CARD_PREFIX = re.compile(r"^(?:\s*[\[【(（<《「『][^\]】)）>》」』]*[\]】)）>》」』])+\s*")
# EA 名字只有字母、数字、- 和 _
_EA_NAME = re.compile(r"[A-Za-z0-9_-]{2,32}")


def card_name(card: str | None) -> str | None:
    """群名片去掉前缀后像 EA 名字就用它，不像（中文、带空格）返回 None。"""

    name = _CARD_PREFIX.sub("", card or "").strip()
    return name if _EA_NAME.fullmatch(name) else None


async def _refresh(found: Binding) -> dict[str, Any]:
    """按绑定时的名字再解析一次拿最新头像；改过名、解析到别人时用绑定时记下的。"""

    try:
        player = await api.resolve(found.name)
    except NoSuchPlayer:
        return found.player()
    return player if player["nucleus_id"] == found.nucleus_id else found.player()


async def find_player(name: str | None, user_id: str, card: str | None = None) -> dict[str, Any]:
    """带名字按名字查；不带就查发消息的人绑定的号，没绑定时把群名片当 EA 名字查。"""

    if name:
        return await api.resolve(name)
    found = await bound(user_id)
    if found:
        return await _refresh(found)
    guess = card_name(card)
    if not guess:
        raise Bf6Error(f"还没绑定：{_BIND_HINT}")
    try:
        return await api.resolve(guess)
    except NoSuchPlayer as error:
        raise Bf6Error(f"还没绑定，按群名片 {guess} 查：{error}。{_BIND_HINT}") from error


def _label(player: dict[str, Any] | Binding) -> str:
    if isinstance(player, Binding):
        player = player.player()
    platform = api.platform_label(player.get("platform"))
    return f"{player['display_name']}（{platform} 账号）" if platform else player["display_name"]


async def bind_reply(name: str | None, user_id: str) -> str:
    if not name:
        current = await bound(user_id)
        return f"当前绑定：{_label(current)}" if current else "用法：/bf6绑定 <EA 名字>"
    player = await api.resolve(name)
    before = await bound(user_id)
    await bind(user_id, player)
    if before is None:
        return f"已绑定 {_label(player)}"
    if before.nucleus_id != player["nucleus_id"]:
        return f"绑定变更：{_label(before)} → {_label(player)}"
    if before.name != player["display_name"]:
        return f"已绑定 {_label(player)}，改过名：{before.name} → {player['display_name']}"
    return f"已绑定 {_label(player)}"
