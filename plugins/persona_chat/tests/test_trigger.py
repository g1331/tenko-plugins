"""什么时候开口、发出去前怎么改写：纯逻辑，经 module fixture 取子模块。"""

import pytest


@pytest.fixture
def trigger(module):
    return module.trigger


@pytest.fixture
def shaping(module):
    return module.shaping


def chat(module, *speakers):
    """按顺序加几条消息，``None`` 是她自己说的。"""

    conversation = module.chat.Conversation()
    for i, speaker in enumerate(speakers):
        conversation.add(f"m{i}", speaker, f"第{i}句")
    return conversation


def test_call_mood_does_not_depend_on_how_much_she_talked(trigger):
    assert trigger.call_mood(direct=True, streak=0) == ("call", True)
    assert trigger.call_mood(direct=False, streak=0) == ("named", False)
    # 只防两个 bot 无限对聊：刚说完几秒又来叫，连着几轮后不强制、再多就不理
    assert trigger.call_mood(direct=True, streak=trigger.STREAK_LOOSE) == ("again", False)
    assert trigger.call_mood(direct=True, streak=trigger.STREAK_MUTE) is None


def test_talking_follows_who_is_chatting_with_her(trigger):
    partners = {}
    talk = trigger.talking
    # 没跟她说过话的人，不 @ 她就不算
    assert not talk(partners, "甲", 0, to_her=False, to_other=False)
    # @ 她算聊开了；那条自己走被叫的路
    assert not talk(partners, "甲", 1, to_her=True, to_other=False) and "甲" in partners
    # 之后不 @ 也算在跟她聊，每说一句续一次
    assert talk(partners, "甲", 100, to_her=False, to_other=False)
    assert talk(partners, "甲", 100 + trigger.TALK_SECONDS - 1, to_her=False, to_other=False)
    # 转去 @、引用别人就聊完了
    assert not talk(partners, "甲", 500, to_her=False, to_other=True) and "甲" not in partners
    # 隔太久没说话也算聊完了
    talk(partners, "乙", 600, to_her=True, to_other=False)
    assert not talk(partners, "乙", 600 + trigger.TALK_SECONDS, to_her=False, to_other=False)


def test_answers_her_only_right_after_her_and_not_to_others(module, trigger):
    conversation = chat(module, "甲(1)", None, "乙(2)")
    kwargs = {"speaker": "乙(2)", "text": "真的假的", "typed": "真的假的", "addressed": False}
    assert trigger.answers_her(conversation.lines, **kwargs)
    assert not trigger.answers_her(conversation.lines, **{**kwargs, "addressed": True})
    assert not trigger.answers_her(conversation.lines, **{**kwargs, "typed": ""})  # 只发了张图
    assert not trigger.answers_her(chat(module, None, "甲(1)", "乙(2)").lines, **kwargs)


def test_she_stops_chiming_in_once_she_talks_more_than_anyone(module, trigger):
    busy = chat(module, *["甲(1)", None] * 6)
    assert trigger.talks_too_much(busy.lines)
    assert not trigger.talks_too_much(chat(module, *["甲(1)"] * 15, None, "乙(2)").lines)

    conversation = chat(module, *["甲(1)"] * 8)
    conversation.unseen = 8
    args = {"now": 1000.0, "answered": False, "follow_seconds": 180, "batch_size": 8}
    assert trigger.chime_in(conversation, load=0, interval=300, **args) == (False, True)
    # 精力用掉三分之二后不主动开口
    assert trigger.chime_in(conversation, load=0.7, interval=300, **args) == (False, False)


def test_she_is_not_the_loudest_one_without_being_called(module, trigger):
    # 一对一聊：她总排在对方后面，连着发的几条算一次，不会比对方多
    assert not trigger.loudest(chat(module, "甲(1)", "甲(1)", None, None, "甲(1)", None).lines)
    # 两个人轮流逗她、她句句都接：比谁都多
    banter = chat(module, "甲(1)", None, "乙(2)", None, "甲(1)", None, "乙(2)", None)
    assert trigger.loudest(banter.lines)
    # 功能结果不算群友在说话
    assert trigger.loudest(chat(module, "甲(1)", None, "Tenko 功能", None).lines)

    # 这时紧跟在她后面的话也不接，跟聊、插话也不开口，等再被叫到
    assert banter.follows == 0
    assert not trigger.keep_talking(banter, answered=True)
    banter.unseen = 8
    args = {"now": 1000.0, "answered": True, "follow_seconds": 180, "batch_size": 8}
    assert trigger.chime_in(banter, load=0, interval=0, **args) == (False, False)
    pair = chat(module, "甲(1)", None, "甲(1)")
    assert trigger.keep_talking(pair, answered=True)


def test_shaping_keeps_first_line_and_stickers(shaping):
    calls = [{"text": "第一句\n第二句", "reply_to": 3}, {"text": "再来一句"}]
    assert shaping.first_line(calls) == [{"text": "第一句", "reply_to": 3}]
    assert shaping.unsearched([{"text": "我搜了下，有新地图"}]) == [{"text": "有新地图"}]
    assert shaping.stickers_only([{"text": "笑死[表情:猫猫震惊]"}, {"text": "就这"}]) == [
        {"text": "[表情:猫猫震惊]"}
    ]
    assert shaping.banned_word([{"text": "蓝 银 真 神"}], ["蓝银真神"]) == "蓝银真神"
