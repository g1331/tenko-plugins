"""命令注册的公共部分：同义命令的注册方式、共用选项，以及从命令参数里取值。

具体有哪些命令在 ``__init__``；这里不放查询逻辑。
"""

from __future__ import annotations

from typing import Any

from arclet.alconna import Alconna, Args, CommandMeta, Option
from arclet.entari import command
from arclet.entari.command import Query
from tenko_api.render import RenderService

from . import catalog, pages

# 和战地一插件一样：-t 只回文字，-c/-r 排几列几行，-n 按名字搜，-s 换排序
TEXT = Option("-t|-text|--text", help_text="只回文字，不出图")
COLS = Option(
    "-c|-col|-列", Args["cols", int], dest="cols", help_text=f"排几列，最多 {pages.MAX_COLS}"
)
ROWS = Option(
    "-r|-row|-行", Args["rows", int], dest="rows", help_text=f"排几行，最多 {pages.MAX_ROWS}"
)
SEARCH = Option(
    "-n|-name|-名字", Args["keyword", str], dest="search", help_text="按名字搜，没上榜的也列出"
)
SORT = Option("-s|-sort|-排序", Args["sort", str], dest="sort", help_text="换排序")
ITEM_OPTIONS = (TEXT, COLS, ROWS, SEARCH, SORT)


def commands(
    names: list[str],
    args: Args,
    description: str,
    usage: str,
    example: str,
    *options: Option,
):
    """一组同义命令：主命令进帮助列表，其余名字隐藏，共用同一个处理函数。

    每个名字单独注册一条，不用 Alconna 的 shortcut：entari 先按命令名前缀派发，别的命令名是
    别名的前缀时别名到不了处理函数（ArcletProject/Entari#34），所以这里的名字互相不能是前缀。
    """

    main, aliases = names[0], names[1:]

    def register(handler):
        also = "；也可以用 " + " ".join(f"/{a}" for a in aliases) if aliases else ""
        meta = CommandMeta(description + also, usage=usage, example=example)
        command.on(Alconna(main, args, *options, meta=meta))(handler)
        for alias in aliases:
            meta = CommandMeta(description, usage=usage.replace(main, alias, 1), hide=True)
            command.on(Alconna(alias, args, *options, meta=meta))(handler)
        return handler

    return register


def player_commands(names: list[str], what: str, flags: str = "", *options: Option):
    """查某个玩家的命令：都带可选的 EA 名字和 -t。"""

    return commands(
        names,
        Args["name?", str],
        f"查询战地六玩家的{what}；绑定过可以不带名字，加 -t 只回文字",
        f"{names[0]} [EA 名字]{flags} [-t]",
        f"/{names[0]} JackFrags",
        TEXT,
        *options,
    )


def items_commands(kind: str, names: list[str]):
    groups = "、".join(g.label for g in catalog.GROUPS[kind])
    sorts = "、".join(s.label for s in pages.SORTS[kind])
    what = "武器" if kind == "weapons" else "载具"
    sample = "卡宾枪" if kind == "weapons" else "空中"
    return commands(
        names,
        Args["first?", str]["second?", str],
        f"查询战地六玩家的{what}排行，可以按类别筛选（{groups}）；绑定过可以不带名字，"
        f"-n 按名字搜，-s 换排序（{sorts}），-c 几列 -r 几行，加 -t 只回文字",
        f"{names[0]} [类别] [EA 名字] [-n 名字] [-s 排序] [-c 列] [-r 行] [-t]",
        f"/{names[0]} {sample} JackFrags -s KPM",
        *ITEM_OPTIONS,
    )


# —— 取参数 ——


def name_of(name: Query[str]) -> str | None:
    return name.result if name.available else None


def card_of(session) -> str | None:
    """群名片：OneBot 适配器把它放在 member.nick 里；私聊没有。"""

    member = getattr(session.event, "member", None)
    return getattr(member, "nick", None) or None


def service_for(text: Query[Any], render_service: RenderService) -> RenderService | None:
    """带了 -t 就不给渲染服务，各个 reply 自然回退成文字。"""

    return None if text.available else render_service


def layout_of(cols: Query[int], rows: Query[int]) -> tuple[int, int]:
    """没给的用默认，超出范围的夹到 1 和上限之间。"""

    def pick(query: Query[int], default: int, most: int) -> int:
        return max(1, min(query.result, most)) if query.available else default

    return (
        pick(cols, pages.ITEM_COLS, pages.MAX_COLS),
        pick(rows, pages.ITEM_ROWS, pages.MAX_ROWS),
    )


def group_and_name(
    kind: str, first: Query[str], second: Query[str]
) -> tuple[catalog.Group | None, str | None]:
    """/bf6w 卡宾枪 abc、/bf6w abc 卡宾枪、/bf6w abc 都行：认得出的是类别，其余是 EA 名字。"""

    words = [q.result for q in (first, second) if q.available]
    group = next((g for w in words if (g := catalog.find_group(kind, w))), None)
    names = [w for w in words if not catalog.find_group(kind, w)]
    return group, names[0] if names else None
