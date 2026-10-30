"""她想说的话发出去之前的改写：都是提示词压不住、只能在代码里兜底的地方。

每个函数拿模型给的 send_message 参数列表（``[{"text": …, "reply_to": …}, …]``），返回改好的。
"""

from __future__ import annotations

import re

from .chat import banned
from .stickers import TOKEN

# 搜完开口先交代“我搜了下”：日志里 133 条，提示怎么写都压不干净，群友不会每句都这么说。
SEARCHED = re.compile(r"^(?:我)?(?:刚)?(?:搜|查)了(?:一)?下[，,。：:\s]*")


def first_line(calls: list[dict]) -> list[dict]:
    """只留第一句。用完功能时结果图大家都看得到，连发三条念数据（K/D、胜率、时长）一看就是 bot；
    被说是 bot 时连着辩解（“欸 哪没活人感嘛 / 我这不是在陪你们聊吗”）越辩越像。"""

    if not (texts := [c["text"] for c in calls]):
        return calls
    first = next((x for t in texts for x in t.splitlines() if x.strip()), "")
    return [{**calls[0], "text": first}]


def unsearched(calls: list[dict]) -> list[dict]:
    """去掉开头的“我搜了下”：搜是悄悄的。"""

    return [{**c, "text": SEARCHED.sub("", c["text"])} for c in calls]


def banned_word(calls: list[dict], words: list[str]) -> str | None:
    """要说的话里第一个违禁词；有就整次不说，只删那几个字，剩下的话照样能看出在聊什么。"""

    return next((w for c in calls if (w := banned(c["text"], words))), None)


def stickers_only(calls: list[dict]) -> list[dict]:
    """字都去掉，只留表情包；一张都没有的那次发言整个不要。"""

    kept = [
        {**c, "text": "\n".join(f"[表情:{name}]" for name in TOKEN.findall(c["text"]))}
        for c in calls
    ]
    return [c for c in kept if c["text"]]
