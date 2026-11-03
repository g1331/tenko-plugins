"""她什么时候开口：收到一条群友消息后，回不回、用什么口气回、要不要等群里安静了再插一句。

这里只做判断，不碰网络和存盘，状态（冷却、连击、她刚说过话的时刻）由调用方传进来、自己更新。
"""

from __future__ import annotations

from collections.abc import Iterable
from itertools import islice, pairwise

from .chat import Conversation, Line

# 对方在 bot 刚说完几秒内又来叫，连续几轮后不再强制回复，再多就不理，防止和别的 bot 无限对聊。
QUICK_SECONDS = 10
STREAK_LOOSE = 3
STREAK_MUTE = 6
# 跟她聊开了的人，后面的话很少再 @：2026-11 日志里她回完之后，同一个人 3 分钟内的下一句
# 139 句什么都不带，只有 84 句仍然 @、引用或叫她名字。所以聊开了的人接着说就当在跟她聊，
# 直到他转去 @、引用别人，或者这么久没再开口。
TALK_SECONDS = 300
# 跟聊时攒够这么多条新消息才问一次模型，不是每句都接。
FOLLOW_MIN_NEW = 2
# 没人叫她时最多连着跟聊这么多次：她每接一句跟聊的窗口就重新计时，群友你一句我一句地聊，
# 她就句句跟着点评，成了“评论员”。被叫到（@、引用、点名）后重新算。
FOLLOW_STREAK = 3
# 日志里最活跃的群友大约说两成的话；最近 20 条里她开口（连着几条算一次）超过这个比例就不插嘴。
SHARE_WINDOW = 20
MAX_SHARE = 0.2


def call_mood(*, direct: bool, streak: int) -> tuple[str, bool] | None:
    """被叫到时怎么回：返回 (口气, 是否必须回)，``None`` 是这次不理。

    ``direct`` 是 @ 了她、引用了她或在私聊里；只提到名字的不算。``streak`` 是对方在她刚说完
    几秒内又来叫的连续轮数，只用来防两个 bot 无限对聊。

    以前还按十分钟里说了多少句算“精力”，累了就让她敷衍一句：有人认真跟她连聊二十轮，
    聊得正好时被硬生生压短。聊不聊下去看对方还接不接，不看她自己说了几句。
    """

    if streak >= STREAK_MUTE:
        return None
    mood = "again" if streak >= STREAK_LOOSE else "call"
    # 只是提到名字：很多人自己部署了同名的 AI，“我的大肥鱼”不一定在说她，让她自己判断。
    if mood == "call" and not direct:
        mood = "named"
    return mood, mood == "call"


def talking(
    partners: dict[str, float], speaker: str, now: float, *, to_her: bool, to_other: bool
) -> bool:
    """记下谁正在跟她聊，返回这条是不是聊开了的人没 @ 她、接着跟她说的话。

    ``partners`` 是 {发言人: 上次跟她说话的时刻}，原地更新。只有明确叫她（@、引用、点名）
    才算聊开了，但那条本身走被叫的路，这里返回假；转去 @、引用别人就算聊完了。
    她接着聊时没接他的话，调用方也会把他移出去。
    """

    for who, at in list(partners.items()):
        if now - at >= TALK_SECONDS:
            del partners[who]
    if to_her:
        partners[speaker] = now
        return False
    if to_other:
        partners.pop(speaker, None)
        return False
    if speaker in partners:
        partners[speaker] = now
        return True
    return False


def answers_her(lines: Iterable[Line], *, speaker: str, text: str, typed: str, addressed: bool):
    """刚进来的这条（``lines`` 的最后一条）是不是紧跟在她后面、多半在回她。

    ``typed`` 是群友亲手打的字，``addressed`` 是这条 @ 或引用了谁。
    """

    lines = list(lines)
    return (
        len(lines) > 1
        and lines[-2].speaker is None
        # 只发一张图、一个表情多半是群友自己的反应，不当成在回她。
        and bool(typed.strip())
        # 引用、@ 了别人的是在跟别人说话，哪怕正好排在她后面。
        and not addressed
        # 一字不差地重复别人刚说的话，多半是跟着问那个人，比如别人问图上“哪个是你”。
        and not any(
            other.speaker not in (None, speaker) and other.text.strip() == text.strip()
            for other in islice(reversed(lines), 1, 12)
        )
    )


def talks_too_much(lines: Iterable[Line]) -> bool:
    """最近的话里她开口的次数已经比最活跃的群友还多：别人没在回她就别插嘴。"""

    lines = list(lines)
    recent = lines[max(len(lines) - SHARE_WINDOW - 1, 0) :]
    turns = sum(b.speaker is None and a.speaker is not None for a, b in pairwise(recent))
    return turns > SHARE_WINDOW * MAX_SHARE


def keep_talking(conversation: Conversation, *, answered: bool) -> bool:
    """聊开了的人没 @ 她接着说时，要不要问模型：和跟聊用同一套刹车。

    没人叫她、自己接着聊满 ``FOLLOW_STREAK`` 次就停，等再被叫到；这句不是紧跟在她后面
    （``answered`` 为假）、她最近又说得比最活跃的群友还多，也不接。以前这条路不设限，
    11-03 有人隔两分钟刷一次别的 bot 的查服命令，每条都当成在跟她聊，她十二分钟里开口九次。
    """

    return conversation.follows < FOLLOW_STREAK and (
        answered or not talks_too_much(conversation.lines)
    )


def chime_in(
    conversation: Conversation,
    *,
    now: float,
    load: float,
    answered: bool,
    follow_seconds: float,
    batch_size: int,
    interval: float,
) -> tuple[bool, bool]:
    """没人叫她时要不要考虑开口：返回 (跟聊, 插话)，两个都假就不问模型。

    跟聊是她刚说过话、大家还在接着聊；插话是没在聊天时群里攒够了 ``batch_size`` 条新消息。
    """

    # 精力用掉三分之二后不再主动开口，只在被叫到时说话。
    fresh = load * 3 < 2
    quiet = talks_too_much(conversation.lines)
    follow = (
        fresh
        and now - conversation.last_reply_at < follow_seconds
        and conversation.follows < FOLLOW_STREAK
        # 紧跟在她后面打的字多半是在回她，一条就问；不然一对一聊着聊着就断了。
        and (answered or (conversation.unseen >= FOLLOW_MIN_NEW and not quiet))
    )
    cold = (
        fresh
        and not quiet
        and 0 < batch_size <= conversation.unseen
        and now - conversation.last_unprompted_at >= interval
        # 刚说过话（被叫到、跟聊）也算：跟聊到了上限，转头又主动插话就白封顶了。
        and now - conversation.last_reply_at >= interval
    )
    return follow, cold
