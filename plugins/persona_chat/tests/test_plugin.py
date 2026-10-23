import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from satori import At, Author, Image, Quote, Text
from satori.element import Message


def untimed(text):
    """去掉开头的“—— 月-日 时:分 ——”时间标记。"""

    return re.sub(r"^—— .+? ——\n", "", text)


@pytest.fixture
def fake_llm(module, monkeypatch):
    fake = SimpleNamespace(replies=[], requests=[])

    async def chat(client, *, model, messages, force, max_tokens):
        fake.requests.append((messages, force))
        return fake.replies.pop(0) if fake.replies else [], {"completion_tokens": 7}

    monkeypatch.setattr(module.llm, "chat", chat)
    module.conversations.clear()
    module._cooldowns.clear()
    module._streaks.clear()
    monkeypatch.setattr(module, "_typing_delay", lambda text: 0)
    for task in module._waiters.values():
        task.cancel()
    module._waiters.clear()
    module.usage_book.store.save({})
    return fake


def session(elements, *, user="1", message_id="m", group="100"):
    sent = AsyncMock(side_effect=lambda message: [SimpleNamespace(id=f"bot-{message_id}")])
    return SimpleNamespace(
        user=SimpleNamespace(id=user, nick=None, name=f"群友{user}"),
        account=SimpleNamespace(self_id="42"),
        event=SimpleNamespace(
            guild=SimpleNamespace(id=group) if group else None,
            channel=SimpleNamespace(id=group or user),
            member=None,
            message=SimpleNamespace(id=message_id, message=elements),
        ),
        send=sent,
    )


async def feed(module, s):
    await module.on_message.callable_target(s)


async def test_plugin_is_controlled(runtime, module):
    from tenko.host.features import feature_service

    assert feature_service.is_controlled("persona_chat")
    assert module.config.api_key == "test"


async def test_mention_replies_without_quote_and_records_usage(module, fake_llm):
    fake_llm.replies.append([{"text": "行，机票钱你出"}])
    s = session([At("42"), Text(" 去吃火锅")], message_id="m1")
    await feed(module, s)

    ((messages, force),) = fake_llm.requests
    assert force and untimed(messages[2]["content"]) == "#1 [群友1(1)] @你 去吃火锅"
    # 叫它的就是最新一条，直接说，不引用
    (sent,) = s.send.call_args.args[0]
    assert isinstance(sent, Message) and sent.children[0].text == "行，机票钱你出"
    assert module.conversations["group:100"].lines[-1].speaker is None
    assert module.usage_book.tokens(module.datetime.now().date(), "group:100") == 7


async def test_mention_of_another_tenko_account_counts(module, fake_llm, monkeypatch):
    # 同群两个 Tenko 账号时宿主可能把 @43 的消息交给 42 处理。
    monkeypatch.setattr(module, "online_accounts", lambda: (SimpleNamespace(self_id="43"),))
    fake_llm.replies.append([{"text": "来了"}])
    await feed(module, session([At("43"), Text(" 在吗")], message_id="m1"))

    ((messages, force),) = fake_llm.requests
    assert force and untimed(messages[2]["content"]) == "#1 [群友1(1)] @你 在吗"


async def test_replying_to_bot_triggers_and_cooldown_holds(module, fake_llm):
    fake_llm.replies.append([{"text": "咕噜"}])
    await feed(module, session([Text("大肥鱼 吃了吗")], message_id="m1"))
    # 回复 bot 刚发的那条（id 为 bot-m1），同一个人还在冷却中
    await feed(module, session([Quote("bot-m1"), Text("没吃")], message_id="m2"))
    assert len(fake_llm.requests) == 1
    await feed(module, session([Quote("bot-m1"), Text("没吃")], user="2", message_id="m3"))
    assert len(fake_llm.requests) == 2
    assert fake_llm.requests[1][0][-2]["content"].endswith("[群友2(2)] 回复#2 没吃")


async def test_batch_lets_model_stay_silent_and_commands_are_context_only(module, fake_llm):
    for i in range(3):
        await feed(module, session([Text(f"闲聊{i}")], user=str(i), message_id=f"m{i}"))
    await feed(module, session([At("42"), Text(" /猜词")], message_id="cmd"))  # @ 了也不触发
    await module._waiters["group:100"]

    ((messages, force),) = fake_llm.requests
    assert not force
    assert messages[2]["content"].endswith("#4 [群友1(1)] @你 /猜词")
    assert module.conversations["group:100"].unseen == 0


async def test_quotes_only_when_others_spoke_after(module, fake_llm):
    await feed(module, session([Text("周末去哪")], message_id="m1"))
    await feed(module, session([Text("随便")], user="2", message_id="m2"))
    fake_llm.replies.append([{"text": "去吃饭", "reply_to": 1}, {"text": "我也去", "reply_to": 3}])
    s = session([At("42"), Text(" 你说呢")], user="3", message_id="m3")
    await feed(module, s)

    first, second = (call.args[0] for call in s.send.call_args_list)
    assert isinstance(first[0], Quote) and first[0].id == "m1"
    assert isinstance(second[0], Message)  # #3 后面只有 bot 自己的话，不算被盖住


async def test_mention_ats_the_speaker_of_that_line(module, fake_llm):
    await feed(module, session([Text("我抽到了")], user="7", message_id="m1"))
    fake_llm.replies.append([{"text": "欧皇", "mention": 1}, {"text": "我也要", "mention": 99}])
    s = session([At("42"), Text(" 你看他")], message_id="m2")
    await feed(module, s)

    first, second = (call.args[0][0] for call in s.send.call_args_list)
    at, text = first.children
    assert isinstance(at, At) and at.id == "7" and text.text == " 欧皇"
    assert [type(c) for c in second.children] == [Text]  # 编号不存在就不 @
    assert module.conversations["group:100"].lines[-2].text == "@群友7 欧皇"


async def test_unknown_quote_and_at_are_spelled_out(module, fake_llm):
    await feed(module, session([Text("我在")], user="7", message_id="m1"))
    quote = Quote("old")(Author("9", name="豹喵"), Text("而且是bot自己判断回不回"))
    await feed(module, session([quote, At("7"), Text(" 其实有")], user="8", message_id="m2"))

    assert module.conversations["group:100"].lines[-1].text == (
        "回复[豹喵：而且是bot自己判断回不回] @群友7 其实有"
    )


async def test_rejected_pictures_are_dropped_and_retried(module, fake_llm, monkeypatch):
    async def download(http, url):
        return f"data:image/png;base64,{url}"

    monkeypatch.setattr(module, "_download", download)
    seen = []

    async def chat(client, *, model, messages, force, max_tokens):
        has_image = any(isinstance(m["content"], list) for m in messages)
        seen.append(has_image)
        if has_image:
            request = httpx.Request("POST", "https://x")
            raise httpx.HTTPStatusError(
                "bad", request=request, response=httpx.Response(400, request=request)
            )
        return [{"text": "看不清"}], {}

    monkeypatch.setattr(module.llm, "chat", chat)
    s = session([At("42"), Image(src="abc")], message_id="m1")
    await feed(module, s)

    assert seen == [True, False]
    assert s.send.called
    assert module.conversations["group:100"].lines[0].pictures == []


async def test_rapid_back_and_forth_loosens_then_stops(module, fake_llm, monkeypatch):
    # 另一个 bot 每次在我们说完立刻 @ 回来
    monkeypatch.setattr(module.config, "user_cooldown_seconds", 0)
    for i in range(8):
        fake_llm.replies.append([{"text": f"回{i}"}])
        await feed(module, session([At("42"), Text(f" 第{i}轮")], user="9", message_id=f"m{i}"))

    # 第 1 次正常，之后连续快速回叫：再强制两轮，接着三轮交给模型决定，第 6 轮起不理
    assert [force for _, force in fake_llm.requests] == [True, True, True, False, False, False]
    assert "来回好几轮" in fake_llm.requests[3][0][-1]["content"]


async def test_long_context_is_compressed_after_reply(module, fake_llm, monkeypatch):
    monkeypatch.setattr(module.config, "context_tokens", 100)

    async def chat(client, *, model, messages, force, max_tokens):
        return [{"text": "嗯"}], {"prompt_tokens": 150, "prompt_cache_miss_tokens": 150}

    async def summarize(client, *, model, messages, max_tokens):
        return "群友1一直在刷屏", {"prompt_cache_miss_tokens": 80, "completion_tokens": 20}

    monkeypatch.setattr(module.llm, "chat", chat)
    monkeypatch.setattr(module.llm, "summarize", summarize)
    for i in range(15):
        await feed(module, session([Text(f"刷屏{i}")], user=str(i + 10), message_id=f"m{i}"))
    await feed(module, session([At("42"), Text(" 你看")], message_id="call"))

    conversation = module.conversations["group:100"]
    assert conversation.summary == "群友1一直在刷屏"
    assert len(conversation.lines) == 10  # 预算 33 token 不够，按下限留 10 条
    assert module.usage_book.tokens(module.datetime.now().date(), "group:100") == 250


async def test_wake_word_only_counts_in_typed_text(module, fake_llm):
    # 群友里也有人叫“大肥鱼”：@ 它或引用它的消息不是在叫 bot
    await feed(module, session([At("77", name="大肥鱼"), Text(" 抱起来")], message_id="m1"))
    quote = Quote("x")(Author("77", name="大肥鱼"), Text("别晃"))
    await feed(module, session([quote, Text("哈哈")], user="2", message_id="m2"))
    assert fake_llm.requests == []

    await feed(module, session([At("77", name="大肥鱼"), Text(" 大肥鱼你说呢")], user="3"))
    assert len(fake_llm.requests) == 1


async def test_follow_up_every_two_messages_until_mostly_tired(module, fake_llm, monkeypatch):
    monkeypatch.setattr(module.config, "stamina", 6)
    fake_llm.replies.append([{"text": "我觉得海里更好玩"}])
    await feed(module, session([At("42"), Text(" 你喜欢哪")], message_id="m0"))

    # bot 刚说过话：每攒两条新消息就问一次模型，不用攒够 batch_size
    for i in range(12):
        fake_llm.replies.append([{"text": f"接{i}"}])
        await feed(module, session([Text(f"那你说说{i}")], user=str(i % 3), message_id=f"f{i}"))
        if task := module._waiters.get("group:100"):
            await task

    hints = [messages[-1]["content"] for messages, _ in fake_llm.requests[1:]]
    # 精力 6，说满 4 句（1 句被叫 + 3 句跟聊）用掉三分之二，不再主动开口
    assert len(hints) == 3 and all(h.endswith(module.FOLLOW_HINT) for h in hints)
    assert not any(force for _, force in fake_llm.requests[1:])


async def test_tired_bot_may_ignore_calls_then_stops(module, fake_llm, monkeypatch):
    monkeypatch.setattr(module.config, "stamina", 4)
    for i in range(8):
        fake_llm.replies.append([{"text": f"回{i}"}])
        await feed(module, session([At("42"), Text(" 在吗")], user=str(i), message_id=f"m{i}"))

    # 说了 4 句后累了：不再强制回复；说满 6 句后完全不理
    assert [force for _, force in fake_llm.requests] == [True] * 4 + [False] * 2
    assert "有点累了" in fake_llm.requests[4][0][-1]["content"]


async def test_lines_are_sent_as_separate_bubbles(module, fake_llm):
    await feed(module, session([Text("我抽到了")], user="7", message_id="m1"))
    fake_llm.replies.append([{"text": "真的假的\n\n晒一下\n我不信\n多的", "mention": 1}])
    s = session([At("42"), Text(" 你看他")], message_id="m2")
    await feed(module, s)

    bubbles = [call.args[0][0].children for call in s.send.call_args_list]
    assert [[getattr(c, "text", "@") for c in b] for b in bubbles] == [
        ["@", " 真的假的"],
        ["晒一下"],
        ["我不信 多的"],  # 最多三条，多出来的并进最后一条
    ]
    conversation = module.conversations["group:100"]
    assert len(conversation.spoken) == 1  # 一口气连发算一句话

    fake_llm.replies.append([{"text": "嗯"}])
    await feed(module, session([At("42"), Text(" 然后呢")], user="2", message_id="m3"))
    assistant = [m for m in fake_llm.requests[-1][0] if m["role"] == "assistant"]
    assert assistant == [{"role": "assistant", "content": "@群友7 真的假的\n晒一下\n我不信 多的"}]


async def test_private_reply_is_not_quoted(module, fake_llm):
    fake_llm.replies.append([{"text": "在呢", "reply_to": 99}])
    s = session([Text("你好")], group=None)
    await feed(module, s)
    sent = s.send.call_args.args[0]
    assert isinstance(sent[0], Message) and not isinstance(sent[0], Quote)
