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


def test_call_mood_answers_direct_calls_even_when_tired(trigger):
    assert trigger.call_mood(direct=True, streak=0, load=0) == ("call", True)
    assert trigger.call_mood(direct=False, streak=0, load=0) == ("named", False)
    # 累了：@ 她照样回（简短），只提到名字的到 1.5 倍就不理
    assert trigger.call_mood(direct=True, streak=0, load=1.8) == ("tired", True)
    assert trigger.call_mood(direct=False, streak=0, load=1.2) == ("tired", False)
    assert trigger.call_mood(direct=False, streak=0, load=trigger.TIRED_OUT) is None
    # 刚说完又来叫，连着几轮多半是别的 bot：先不强制，再多就不理
    assert trigger.call_mood(direct=True, streak=trigger.STREAK_LOOSE, load=0) == ("again", False)
    assert trigger.call_mood(direct=True, streak=trigger.STREAK_MUTE, load=0) is None


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


def test_shaping_keeps_first_line_and_stickers(shaping):
    calls = [{"text": "第一句\n第二句", "reply_to": 3}, {"text": "再来一句"}]
    assert shaping.first_line(calls) == [{"text": "第一句", "reply_to": 3}]
    assert shaping.unsearched([{"text": "我搜了下，有新地图"}]) == [{"text": "有新地图"}]
    assert shaping.stickers_only([{"text": "笑死[表情:猫猫震惊]"}, {"text": "就这"}]) == [
        {"text": "[表情:猫猫震惊]"}
    ]
    assert shaping.banned_word([{"text": "蓝 银 真 神"}], ["蓝银真神"]) == "蓝银真神"
