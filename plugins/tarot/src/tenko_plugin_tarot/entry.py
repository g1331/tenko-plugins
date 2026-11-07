"""命令注册和从命令参数里取值的公共部分。"""

from __future__ import annotations

from typing import Any

from arclet.alconna import Alconna, AllParam, Args, CommandMeta
from arclet.entari import Session, command
from arclet.entari.command import Query
from satori import Text

from .readings import QUESTION_LENGTH

TEXT_ARGS = Args["first?", str]["rest?", AllParam]


def commands(names: list[str], args: Args, description: str, usage: str, example: str):
    """entari 按命令名派发，别名到不了处理函数，每个名字单独注册。"""

    def register(handler):
        main, aliases = names[0], names[1:]
        note = f"；也可以用 {'、'.join(aliases)}" if aliases else ""
        meta = CommandMeta(description + note, usage=usage, example=example)
        command.on(Alconna(main, args, meta=meta))(handler)
        for alias in aliases:
            meta = CommandMeta(description, usage=usage.replace(main, alias, 1), hide=True)
            command.on(Alconna(alias, args, meta=meta))(handler)
        return handler

    return register


def display_name(session: Session) -> str:
    """群名片优先：OneBot 适配器把它放在 member.nick 里。"""

    member = getattr(session.event, "member", None)
    return getattr(member, "nick", None) or session.user.name or session.user.id


def words(first: Query[str], rest: Query[Any]) -> str:
    """命令名后面的文字，图片之类的不算。"""

    words = [first.result] if first.available else []
    if rest.available:
        words += [e.text for e in rest.result if isinstance(e, Text)]
    return " ".join(w.strip() for w in words if w.strip())


def question(first: Query[str], rest: Query[Any]) -> str | None:
    return words(first, rest)[:QUESTION_LENGTH] or None
