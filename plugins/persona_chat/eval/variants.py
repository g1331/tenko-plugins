"""回放用的几种变体：在加载好的插件上临时改掉一部分规则、提示或改写逻辑。

每个变体是 ``(插件模块, persona 模块) -> [(对象, 属性名, 新值), …]``，回放前换上、回放完换回。
"""

from __future__ import annotations

import re
from collections.abc import Callable

Patch = list[tuple[object, str, object]]


def _drop(text: str, *needles: str) -> str:
    """删掉规则里含 ``needles`` 任一词的条目（以“- ”开头的一行）。"""

    lines = text.split("\n")
    kept = [x for x in lines if not (x.startswith("- ") and any(n in x for n in needles))]
    missing = [n for n in needles if not any(n in x for x in lines if x.startswith("- "))]
    if missing:
        raise ValueError(f"规则里找不到这些条目：{missing}")
    return "\n".join(kept)


def drop_member(*needles: str) -> Callable[[object, object], Patch]:
    def apply(module, persona) -> Patch:
        return [(persona, "MEMBER", _drop(persona.MEMBER, *needles))]

    return apply


# 规则分组，按条目开头的几个字认。
GROUPS = {
    # 当平辈、不捧哏
    "peer": ("你是平辈的群友",),
    # 被逗、被骂、被挑刺、被起外号、开黄腔
    "tease": ("聊口味、聊看法时你可以有立场", "引用你的话再吐槽", "有人开黄腔"),
    # 认真问、问她自己、问她看出来没有、别人难过
    "earnest": ("有人认真问你", "被问到你自己", "被问“你看出来了吗”", "遇到别人难过"),
    # 网红和梗、不熟的话题、具体事实
    "knowledge": ("群友随口提到的外号", "不熟、不感兴趣的话题", "具体的事实"),
    # 说法：别复述、别概括点评、别写作文、用平常说法、别抖机灵、别没话找话、梗是调料
    "style": (
        "回话直接说",
        "也别先把对方的话概括一遍",
        "像打字聊天",
        "用最平常的说法",
        "不用每句都抖机灵",
        "没人找你、也没有梗可接",
        "人设里的梗和口头禅是调料",
    ),
    # 其余：接住对方、关心群友的事、群友聊 bot 本身
    "misc": ("群友跟你聊，你接住对方", "群友说起自己的事", "群友在聊 bot 本身"),
}


def _no_bot_talk(module, persona) -> Patch:
    """不再在被说是 bot 时只留第一句、优先只发表情包。"""

    return [(module, "BOT_TALK", re.compile(r"(?!)"))]


VARIANTS: dict[str, Callable[[object, object], Patch]] = {
    "base": lambda module, persona: [],
    **{f"-{name}": drop_member(*needles) for name, needles in GROUPS.items()},
    "-bot_talk": _no_bot_talk,
}


class applied:
    """``with applied(host, "名字"):`` 期间插件换上这个变体。"""

    def __init__(self, host, name: str) -> None:
        self.patch = VARIANTS[name](host.module, host.persona)
        self.saved: Patch = []

    def __enter__(self):
        for target, attr, value in self.patch:
            self.saved.append((target, attr, getattr(target, attr)))
            setattr(target, attr, value)
        return self

    def __exit__(self, *exc) -> None:
        for target, attr, value in reversed(self.saved):
            setattr(target, attr, value)
