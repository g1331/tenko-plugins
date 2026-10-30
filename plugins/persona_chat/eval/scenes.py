"""从快照里挑回放场景：一份按触发方式分层的随机样本（general），外加按规则针对的情形挑的几组。

只挑不看图也判断得了的场景（用户要求过）：触发那条和前两条里有图、只发了表情包的都跳过。
针对性的几组用来检验“删掉某条规则会不会坏事”：随机样本里很少碰到被骂、被叫 bot、
被认真请教这些情形，只看随机样本，删掉专门管它们的规则也看不出差别。
"""

from __future__ import annotations

import random
import re
from collections import defaultdict
from datetime import datetime

from replay import Scene

_FOLLOW = 180  # 插件的 follow_seconds
_COLD_GAP = 300  # 她上次开口之后至少这么久才算“没在聊”
_MIN_CONTEXT = 15
_PICTURE = re.compile(r"\[(?:图片|表情包|表情)[^\]]*\]")
_QUOTE = re.compile(r"^回复#(\d+)")

# 规则针对的情形：(标签, 看触发那条的正则)。只是粗筛，挑出来的场景不一定都中，够多就行。
TARGETS = {
    "bot": re.compile(r"人机|机器人|活人感|智障|\bbot\b|\bai\b|ai味|是不是真人|真人吗", re.I),
    "tease": re.compile(r"主人|爹|祖宗|认主|闭嘴|滚|傻|笨|蠢|sb|弱智|骂|欠|老婆|叫我"),
    "earnest": re.compile(
        r"(?:你觉得|你怎么看|怎么办|为什么|为啥|推荐|建议|咋整|值不值|该不该).{0,40}[？?]?$"
    ),
    "fact": re.compile(
        r"(?:是什么|是谁|啥意思|什么意思|多少|几号|哪个|哪天|版本|更新|出了没|上线)"
    ),
    "self": re.compile(
        r"你(?:是谁|叫什么|几岁|多大|喜欢|讨厌|会不会|能不能|记得|还记得|在干嘛|干嘛呢)"
    ),
}
# 一般样本里各种触发方式的场景数。
GENERAL = {"call": 24, "named": 4, "answer": 12, "follow": 8, "cold": 12}
_PER_TARGET = 8


def labeled(items: list[dict]) -> tuple[dict, list[Scene]]:
    """以前几轮盲评的题还原成能回放的会话：都是群主挑过的典型情形（被叫 bot、被逗、玩梗、认真问），
    正好补上快照里少的那几类。每题只有最近 10 条，昵称后面配上假的 QQ 号。

    当时的题把 @ 她写成“@某人”、引用她写成“回复了一条”，最后一条照插件的写法换回“@你”。
    """

    conversations, found = {}, []
    base = datetime(2026, 10, 26, 20).timestamp()
    for i, item in enumerate(items):
        names: dict[str, int] = {}
        lines = []
        for j, row in enumerate(item["ctx"]):
            who, _, text = row.partition("：")
            speaker = None if who == "她" else f"{who}({names.setdefault(who, 10000 + len(names))})"
            lines.append(
                {
                    "number": j + 1,
                    "message_id": f"label-{i}-{j}",
                    "speaker": speaker,
                    "text": text,
                    "at": base + 20 * j,
                    "role": "",
                }
            )
        last = lines[-1]
        called = item["kind"] == "叫" or "@某人" in last["text"]
        if called:
            text = last["text"].replace("回复了一条", "").replace("@某人", "", 1).strip()
            last["text"] = f"@你 {text}"
            kind = "call"
        elif len(lines) > 1 and lines[-2]["speaker"] is None:
            kind = "answer"
        else:
            kind = "cold"
        key = f"label:{i}"
        conversations[key] = {"summary": "", "summary_from": 0, "lines": lines}
        tags = ["labeled", "target:labeled"]
        if item["kind"] == "bot":
            tags.append("target:bot")
        found.append(Scene(f"label-{item['round']}-{i}", key, last["number"], kind, tags))
    return conversations, found


def candidates(data: dict) -> list[Scene]:
    found = []
    for key, saved in data.items():
        if not key.startswith("group:"):
            continue
        lines = saved["lines"]
        by_number = {x["number"]: x for x in lines}
        last_hers = None
        for i, x in enumerate(lines):
            if x["speaker"] is None:
                last_hers = x
                continue
            if (
                i < _MIN_CONTEXT
                or x["speaker"] == "Tenko 功能"
                or x["text"].lstrip().startswith("/")
            ):
                continue
            window = [y["text"] for y in lines[max(i - 2, 0) : i + 1]]
            if any(_PICTURE.search(t) for t in window) or "[聊天记录" in x["text"]:
                continue
            kind = _kind(x, lines[i - 1], by_number, last_hers, lines, i)
            if kind is None:
                continue
            tags = [tag for tag, pattern in TARGETS.items() if pattern.search(_body(x["text"]))]
            found.append(Scene(f"{key[6:]}-{x['number']}", key, x["number"], kind, tags))
    return found


def _body(text: str) -> str:
    return re.sub(r"^回复#\d+（.*?）\s*", "", text).replace("@你", "").strip()


def _kind(x, before, by_number, last_hers, lines, i) -> str | None:
    text = x["text"]
    quoted = _QUOTE.match(text)
    if "@你" in text or quoted and by_number.get(int(quoted[1]), {}).get("speaker", "") is None:
        return "call"
    if "小埋" in _body(text):
        return "named"
    talking_to_others = "@" in text or quoted
    if before["speaker"] is None and x["at"] - before["at"] < _FOLLOW and not talking_to_others:
        return "answer"
    if last_hers and x["at"] - last_hers["at"] < _FOLLOW:
        return "follow"
    if last_hers is None or x["at"] - last_hers["at"] >= _COLD_GAP:
        # 插件攒够 8 条新消息才考虑插话：只挑攒够了的位置。
        since = [
            y for y in lines[max(i - 7, 0) : i + 1] if y["speaker"] not in (None, "Tenko 功能")
        ]
        return "cold" if len(since) == 8 else None
    return None


def pick(data: dict, seed: int = 7) -> list[Scene]:
    rng = random.Random(seed)
    pool = candidates(data)
    chosen: dict[str, Scene] = {}

    def take(scenes: list[Scene], count: int, label: str) -> None:
        # 各群轮流挑，别让最吵的群占满。
        by_group = defaultdict(list)
        for scene in scenes:
            if scene.id not in chosen:
                by_group[scene.key].append(scene)
        for group in by_group.values():
            rng.shuffle(group)
        groups = list(by_group.values())
        rng.shuffle(groups)
        taken = 0
        while taken < count and any(groups):
            for group in groups:
                if group and taken < count:
                    scene = group.pop()
                    scene.tags = [label, *scene.tags]
                    chosen[scene.id] = scene
                    taken += 1

    for kind, count in GENERAL.items():
        take([s for s in pool if s.kind == kind], count, "general")
    for tag in TARGETS:
        # 针对性的场景只挑被叫到、正在接话的：规则主要管这些时候怎么回。
        take(
            [s for s in pool if tag in s.tags and s.kind in ("call", "named", "answer")],
            _PER_TARGET,
            f"target:{tag}",
        )
    # 一般样本里碰巧中了某个情形的也算进那一组。
    for scene in chosen.values():
        for tag in TARGETS:
            if tag in scene.tags and f"target:{tag}" not in scene.tags:
                scene.tags.append(f"target:{tag}")
    return sorted(chosen.values(), key=lambda s: (s.key, s.cut))
