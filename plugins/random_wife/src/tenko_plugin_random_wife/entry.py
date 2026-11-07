"""命令注册和从命令参数里取值的公共部分。"""

from __future__ import annotations

from arclet.alconna import Alconna, Args, CommandMeta, Option
from arclet.entari import MessageChain, Session, command

from .library import LibraryError


def commands(
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


async def answer(job) -> MessageChain | str:
    try:
        return await job
    except LibraryError as error:
        return str(error)


def display_name(session: Session) -> str:
    """群名片优先：OneBot 适配器把它放在 member.nick 里。"""

    member = getattr(session.event, "member", None)
    return getattr(member, "nick", None) or session.user.name or session.user.id
