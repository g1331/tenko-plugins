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
# 精力用到这么多倍时，只提到名字的不理；@ 她、引用她的再累也回。
TIRED_OUT = 1.5
# 跟聊时攒够这么多条新消息才问一次模型，不是每句都接。
FOLLOW_MIN_NEW = 2
# 没人叫她时最多连着跟聊这么多次：她每接一句跟聊的窗口就重新计时，群友你一句我一句地聊，
# 她就句句跟着点评，成了“评论员”。被叫到（@、引用、点名）后重新算。
FOLLOW_STREAK = 3
# 日志里最活跃的群友大约说两成的话；最近 20 条里她开口（连着几条算一次）超过这个比例就不插嘴。
SHARE_WINDOW = 20
MAX_SHARE = 0.2


def call_mood(*, direct: bool, streak: int, load: float) -> tuple[str, bool] | None:
    """被叫到时怎么回：返回 (口气, 是否必须回)，``None`` 是这次不理。

    ``direct`` 是 @ 了她、引用了她或在私聊里；只提到名字的不算。``streak`` 是对方在她刚说完
    几秒内又来叫的连续轮数，``load`` 是用掉的精力比例（1 是累了）。
    """

    # @ 她、引用她时一声不吭，群里就是“似了吧”“bot是不是下播了”：再累也简短回一句。
    # 以前到两倍连 @ 也不理：一群人轮流叫她查战绩，她刚答应“马上查”就没了声。
    if streak >= STREAK_MUTE or not direct and load >= TIRED_OUT:
        return None
    mood = "again" if streak >= STREAK_LOOSE else "tired" if load >= 1 else "call"
    # 只是提到名字：很多人自己部署了同名的 AI，“我的大肥鱼”不一定在说她，让她自己判断。
    if mood == "call" and not direct:
        mood = "named"
    return mood, mood == "call" or mood == "tired" and direct


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
