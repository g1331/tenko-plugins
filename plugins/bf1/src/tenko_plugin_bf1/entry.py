"""命令注册的公共部分：同义命令的注册方式、共用选项，以及从命令参数里取值。

具体有哪些命令在 ``__init__``；这里不放查询逻辑。
"""

from __future__ import annotations

from typing import Any

from arclet.alconna import Alconna, Args, CommandMeta, Option
from arclet.entari import MessageChain, Session, command
from arclet.entari.command import Query
from tenko_api.render import RenderService

from . import groups, pages, stats
from .reply import answer

# 旧 bot 的 -t：不出图，只回文字
TEXT = Option("-t|-text|--text", help_text="只回文字，不出图")

# 旧 bot 的 -r/-c：武器、载具卡片排几行几列
ROWS = Option("-r|-row|-行", Args["rows", int], dest="rows", help_text="排几行，最多 10")
COLS = Option("-c|-col|-列", Args["cols", int], dest="cols", help_text="排几列，最多 8")

# 旧 bot 的 -n 按名字搜、-s 排序（Alconna 自带 -h、-sct、-cp，不和这些撞）
SEARCH = Option(
    "-n|-name|-名字", Args["keyword", str], dest="search", help_text="按名字搜，没用过的也列出"
)
SORT = Option(
    "-s|-sort|-排序",
    Args["sort", str],
    dest="sort",
    help_text="排序：击杀、时长、KPM、命中率、爆头率（载具是击杀、时长、KPM、摧毁）",
)
ITEM_OPTIONS = (TEXT, COLS, ROWS, SEARCH, SORT)


# —— 注册 ——


def _meta(description: str, usage: str, example: str, aliases: list[str]) -> CommandMeta:
    """主命令的说明里列出旧名字；旧名字本身不进帮助列表。"""

    also = "；也可以用 " + " ".join(f"/{a}" for a in aliases) if aliases else ""
    return CommandMeta(description + also, usage=usage, example=example)


def commands(
    names: list[str],
    args: Args,
    description: str,
    usage: str,
    example: str,
    *options: Option,
):
    """一组同义命令：/bf1s 这类主命令，加上旧 bot 用过的 /stat、/战绩 等。

    每个名字单独注册一条，共用同一个处理函数。不用 Alconna 的 shortcut：entari 先按命令名
    前缀派发，别的命令名是别名的前缀时（/bf1 之于 /bf1b）别名到不了处理函数，
    见 ArcletProject/Entari#34。
    """

    main, aliases = names[0], names[1:]

    def register(handler):
        meta = _meta(description, usage, example, aliases)
        command.on(Alconna(main, args, *options, meta=meta))(handler)
        for alias in aliases:
            meta = CommandMeta(description, usage=usage.replace(main, alias, 1), hide=True)
            command.on(Alconna(alias, args, *options, meta=meta))(handler)
        return handler

    return register


def player_commands(
    names: list[str], what: str, flags: str = "", example: str = "", *options: Option
):
    """查某个玩家的命令：都带可选的 EA 名字和 -t。"""

    return commands(
        names,
        Args["name?", str],
        f"查询战地一玩家的{what}，绑定过可以不带名字，也可以用 #PID 查；加 -t 只回文字",
        f"{names[0]} [EA 名字]{flags} [-t]",
        f"/{names[0]} SHlSAN13{example}",
        TEXT,
        *options,
    )


def items_commands(kind: str, names: list[str]):
    what = "武器" if kind == "weapons" else "载具"
    sample = "步枪、突击兵" if kind == "weapons" else "坦克、飞机"
    return commands(
        names,
        Args["first?", str]["second?", str],
        f"查询战地一玩家的{what}击杀排行，可以按类别或兵种筛选（{sample}等，也可以直接发 /"
        f"{groups.GROUPS[kind][0].names[0]}）；绑定过可以不带名字，-n 按名字搜，"
        f"-s 换排序（{'、'.join(s.label for s in pages.SORTS[kind])}），-c 几列 -r 几行自己排，"
        "加 -t 只回文字",
        f"{names[0]} [类别] [EA 名字] [-n 名字] [-s 排序] [-c 列] [-r 行] [-t]",
        f"/{names[0]} {sample.split('、')[0]} SHlSAN13 -s KPM",
        *ITEM_OPTIONS,
    )


# —— 取参数 ——


def name_of(name: Query[str]) -> str | None:
    return name.result if name.available else None


def keyword_of(keyword: Query[list]) -> str:
    """AllParam 收到的几段拼回一句话。"""

    return " ".join(str(part) for part in keyword.result).strip()


def card_of(session: Session) -> str | None:
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
) -> tuple[groups.Group | None, str | None]:
    """/武器 步枪 abc、/武器 abc 步枪、/武器 abc 都行：EA 名字不会是中文，认得出的就是分组。"""

    words = [q.result for q in (first, second) if q.available]
    group = next((g for w in words if (g := groups.find(kind, w))), None)
    names = [w for w in words if not groups.find(kind, w)]
    return group, names[0] if names else None


async def items_answer(
    kind: str,
    group: groups.Group | None,
    name: str | None,
    session: Session,
    text: Query[Any],
    cols: Query[int],
    rows: Query[int],
    search: Query[str],
    sort: Query[str],
    render_service: RenderService,
) -> MessageChain | str:
    """武器、载具和分组命令共用：把选项整理好交给排行查询。"""

    # 旧 bot 的用法里常给名字加引号
    keyword = name_of(search).strip("\"'“”") if search.available else None
    return await answer(
        stats.items_reply(
            kind,
            name,
            service_for(text, render_service),
            session.user.id,
            group,
            layout_of(cols, rows),
            keyword or None,
            name_of(sort),
            card_of(session),
        )
    )
