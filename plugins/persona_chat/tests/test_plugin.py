import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from satori import At, Author, Image, Quote, Text
from satori.element import Custom, Message


def untimed(text):
    """去掉开头的“—— 月-日 时:分 ——”时间标记。"""

    return re.sub(r"^—— .+? ——\n", "", text)


@pytest.fixture
def fake_llm(module, monkeypatch):
    fake = SimpleNamespace(replies=[], requests=[], roles={})

    async def chat(client, *, model, messages, force, max_tokens):
        fake.requests.append((messages, force))
        return fake.replies.pop(0) if fake.replies else [], {"completion_tokens": 7}

    monkeypatch.setattr(module.llm, "chat", chat)
    fake.picks, fake.picked = [], []  # 单独挑表情包：要返回的 (名字, 分数)、问过的话

    async def pick_sticker(client, *, model, prompt, catalog, recent, text):
        fake.picked.append(text)
        name, score = fake.picks.pop(0) if fake.picks else ("", 0)
        return name, score, {}

    monkeypatch.setattr(module.llm, "pick_sticker", pick_sticker)
    module.conversations.clear()
    module._cooldowns.clear()
    module._streaks.clear()
    module._talks.clear()

    async def today(key):
        return ""

    fake.today = module._today
    monkeypatch.setattr(module, "_today", today)  # 每天的小事有单独的测试
    monkeypatch.setattr(module, "_typing_delay", lambda text: 0)
    monkeypatch.setattr(module, "_reply_delay", lambda text: 0)
    monkeypatch.setattr(module.config, "search_daily", 0)  # 搜索有单独的测试
    fake.commands = module._commands
    monkeypatch.setattr(module, "_commands", dict)  # 用别的插件的命令有单独的测试

    async def get_user_perm(session):
        return fake.roles.get(session.user.id, 0)

    monkeypatch.setattr(module, "get_user_perm", get_user_perm)
    for task in module._waiters.values():
        task.cancel()
    module._waiters.clear()
    module.usage_book.store.save({})
    module.sticker_book.store.save({})
    module.chat_store.save({})
    module.day_store.save({})
    module.memory_store.save({})
    module.persona_store.save({})
    module.at_only_store.save([])
    module._unsaved.clear()
    return fake


def session(elements, *, user="1", message_id="m", group="100", internal=None):
    sent = AsyncMock(side_effect=lambda message: [SimpleNamespace(id=f"bot-{message_id}")])
    return SimpleNamespace(
        user=SimpleNamespace(id=user, nick=None, name=f"群友{user}"),
        account=SimpleNamespace(self_id="42", protocol=protocol(internal)),
        event=SimpleNamespace(
            guild=SimpleNamespace(id=group, name="测试群") if group else None,
            channel=SimpleNamespace(id=group or user),
            member=None,
            message=SimpleNamespace(id=message_id, message=elements),
        ),
        send=sent,
    )


def protocol(internal):
    """像真的 satori 协议一样只认 ``call_api("internal/动作", 参数)``。"""

    if internal is None:
        return SimpleNamespace()

    async def call_api(action, params=None):
        return await internal(action.removeprefix("internal/"), **(params or {}))

    return SimpleNamespace(call_api=call_api)


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
    assert force and untimed(messages[4]["content"]) == "#1 [群友1(1)] @你 去吃火锅"
    assert "#1 在叫你：“去吃火锅”" in messages[-1]["content"]  # 原话放在最后，别被前面的图带跑
    # 叫它的就是最新一条，直接说，不引用
    (sent,) = s.send.call_args.args[0]
    assert isinstance(sent, Message) and sent.children[0].text == "行，机票钱你出"
    assert module.conversations["group:100"].lines[-1].speaker is None
    assert module.usage_book.tokens(module.datetime.now().date(), "group:100") == 7


async def test_chat_survives_a_restart_until_reset(module, fake_llm):
    fake_llm.replies.append([{"text": "行，机票钱你出"}])
    await feed(module, session([At("42"), Text(" 去吃火锅")], message_id="m1"))
    module.conversations.clear()  # 重启：内存清空，存盘还在

    fake_llm.replies.append([{"text": "还在想火锅"}])
    await feed(module, session([At("42"), Text(" 刚才说啥来着")], user="2", message_id="m2"))
    messages, _ = fake_llm.requests[-1]
    assert untimed(messages[4]["content"]) == "#1 [群友1(1)] @你 去吃火锅"
    assert messages[5] == {"role": "assistant", "content": "行，机票钱你出"}
    assert untimed(messages[6]["content"]) == "#3 [群友2(2)] @你 刚才说啥来着"

    module._save(forget="group:100")
    assert "group:100" not in module.chat_store.load(default={})


async def test_mention_of_another_tenko_account_counts(module, fake_llm, monkeypatch):
    # 同群两个 Tenko 账号时宿主可能把 @43 的消息交给 42 处理。
    monkeypatch.setattr(module, "online_accounts", lambda: (SimpleNamespace(self_id="43"),))
    fake_llm.replies.append([{"text": "来了"}])
    await feed(module, session([At("43"), Text(" 在吗")], message_id="m1"))

    ((messages, force),) = fake_llm.requests
    assert force and untimed(messages[4]["content"]) == "#1 [群友1(1)] @你 在吗"


async def test_replying_to_bot_triggers_and_cooldown_holds(module, fake_llm):
    fake_llm.replies.append([{"text": "咕噜"}])
    await feed(module, session([Text("大肥鱼 吃了吗")], message_id="m1"))
    # 回复 bot 刚发的那条（id 为 bot-m1），同一个人还在冷却中
    await feed(module, session([Quote("bot-m1"), Text("没吃")], message_id="m2"))
    assert len(fake_llm.requests) == 1
    await feed(module, session([Quote("bot-m1"), Text("没吃")], user="2", message_id="m3"))
    assert len(fake_llm.requests) == 2
    assert fake_llm.requests[1][0][-2]["content"].endswith("[群友2(2)] 回复#2（你说的“咕噜”） 没吃")


async def test_batch_lets_model_stay_silent_and_commands_are_context_only(module, fake_llm):
    for i in range(3):
        await feed(module, session([Text(f"闲聊{i}")], user=str(i), message_id=f"m{i}"))
    await feed(module, session([At("42"), Text(" /猜词")], message_id="cmd"))  # @ 了也不触发
    await module._waiters["group:100"]

    ((messages, force),) = fake_llm.requests
    assert not force
    assert messages[4]["content"].endswith("#4 [群友1(1)] @你 /猜词")
    assert module.conversations["group:100"].unseen == 0


async def test_banned_words_are_unseen_and_unsaid(module, fake_llm, monkeypatch):
    monkeypatch.setattr(module.config, "banned_words", ["lanyinzhenshen"])
    s = session([At("42"), Text(" LAN yin zhen shen 是谁")], message_id="m1")
    await feed(module, s)
    assert not fake_llm.requests and not module.conversations["group:100"].lines
    assert not s.send.called

    fake_llm.replies.append([{"text": "你说的是 lanyinzhenshen 吧"}, {"text": "我知道"}])
    s = session([At("42"), Text(" 懒音真神是谁")], user="2", message_id="m2")  # 同音也拦
    await feed(module, s)
    assert not fake_llm.requests

    s = session([At("42"), Text(" 刚才说的是谁")], message_id="m3")
    await feed(module, s)
    assert len(fake_llm.requests) == 1 and not s.send.called
    assert [line.speaker for line in module.conversations["group:100"].lines] == ["群友1(1)"]


async def test_at_only_mode_ignores_names_quotes_and_chiming_in(module, fake_llm, monkeypatch):
    async def require_perm(session, level):
        return session.user.id == "1"  # 1 号是群管理

    monkeypatch.setattr(module, "require_perm", require_perm)
    s = session([], user="1", message_id="cmd")
    assert "以后只有 @ 她才回" in await module.manage_at_only(s, "开")
    assert "现在开着" in await module.manage_at_only(s, None)
    assert "只有群管理员" in await module.manage_at_only(session([], user="2"), "关")

    await feed(module, session([Text("大肥鱼 吃了吗")], user="3", message_id="m1"))
    for i in range(10):  # 攒够数也不插话
        await feed(module, session([Text(f"闲聊{i}")], user=str(i + 10), message_id=f"n{i}"))
    assert not fake_llm.requests and not module._waiters
    assert len(module.conversations["group:100"].lines) == 11  # 照样记进上下文

    fake_llm.replies.append([{"text": "在"}])
    await feed(module, session([At("42"), Text(" 在吗")], user="3", message_id="m2"))
    assert len(fake_llm.requests) == 1

    await module.manage_at_only(s, "关")
    assert module.at_only_store.load(default=[]) == []


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


async def test_typed_at_of_a_window_speaker_becomes_a_real_at(module, fake_llm):
    await feed(module, session([Text("在")], user="99", message_id="m1"))
    await feed(module, session([Text("我也在")], user="9", message_id="m2"))  # 昵称是前者的前缀
    fake_llm.replies.append([{"text": "@群友99 生图 一只鲸鱼"}, {"text": "@路人 你好"}])
    s = session([At("42"), Text(" 你叫它画")], message_id="m3")
    await feed(module, s)

    first, second = (call.args[0][0] for call in s.send.call_args_list)
    at, text = first.children
    assert isinstance(at, At) and at.id == "99" and text.text == " 生图 一只鲸鱼"
    assert [c.text for c in second.children] == ["@路人 你好"]  # 窗口里没这个人就照原样发
    assert module.conversations["group:100"].lines[-2].text == "@群友99 生图 一只鲸鱼"


async def test_lookalike_names_are_pointed_out(module, fake_llm):
    await feed(module, session([Text("在")], user="9", message_id="m1"))
    assert "不是你" not in module.conversations["group:100"].place
    module.conversations["group:100"].lines[-1].speaker = "大肥鱼bot(9)"
    await feed(module, session([Text("嗯")], user="7", message_id="m2"))
    assert "群里的 大肥鱼bot(9) 名字和你像，但不是你" in module.conversations["group:100"].place


async def test_lookalike_bot_only_calls_her_with_an_at(module, fake_llm):
    fake_llm.replies.append([{"text": "在呢"}])
    await feed(module, session([At("42"), Text(" 在吗")], message_id="m1"))

    # 另一个叫“大肥鱼”的 AI 紧跟着她说话、提到名字：不算叫她，也不催她接话
    other = session([Text("我才是大肥鱼")], user="9", message_id="m2")
    other.user.name = "大肥鱼bot"
    await feed(module, other)
    assert len(fake_llm.requests) == 1 and "group:100" not in module._waiters
    assert module.conversations["group:100"].unseen == 0

    fake_llm.replies.append([{"text": "干嘛"}])
    other = session([At("42"), Text(" 你好")], user="9", message_id="m3")
    other.user.name = "大肥鱼bot"
    await feed(module, other)
    assert len(fake_llm.requests) == 2  # @ 她照样回


async def test_called_reply_waits_like_typing(module, fake_llm, monkeypatch):
    monkeypatch.setattr(module, "_reply_delay", lambda text: 5.0)
    waited = []

    async def sleep(seconds):
        waited.append(seconds)

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    fake_llm.replies.append([{"text": "在呢"}])
    await feed(module, session([At("42"), Text(" 在吗")], message_id="m1"))
    assert len(waited) == 1 and 4 < waited[0] <= 5  # 扣掉已经过去的时间


async def test_quote_of_her_line_counts_even_when_the_id_differs(module, fake_llm):
    fake_llm.replies.append([{"text": "/海洋"}])
    await feed(module, session([At("42"), Text(" 猜一个")], message_id="m1"))
    # 游戏 bot 引用她的猜测回结果；事件由她另一个号收到，引用的消息号和她发出时拿到的不一样
    fake_llm.replies.append([{"text": "/水"}])
    quote = Quote("other-id")(Author("42", name="十七"), Text("/海洋"))
    await feed(module, session([quote, Text("0.78%")], user="5", message_id="m2"))

    lines = module.conversations["group:100"].lines
    assert lines[-2].text == f"回复#{lines[-3].number}（你说的“/海洋”） 0.78%"
    assert lines[-1].text == "/水"  # 算在回她，被叫到照样回


async def test_unknown_quote_and_at_are_spelled_out(module, fake_llm):
    await feed(module, session([Text("我在")], user="7", message_id="m1"))
    quote = Quote("old")(Author("9", name="豹喵"), Text("而且是bot自己判断回不回"))
    await feed(module, session([quote, At("7"), Text(" 其实有")], user="8", message_id="m2"))

    assert module.conversations["group:100"].lines[-1].text == (
        "回复[豹喵：而且是bot自己判断回不回] @群友7 其实有"
    )


def _node(name, *segments):
    return {"sender": {"nickname": name}, "message": list(segments)}


async def test_forwarded_chat_is_read_out(module, fake_llm):
    nodes = [
        _node("杜拉罕", {"type": "image", "data": {"url": "https://q/1.jpg"}}),
        _node("杜拉罕", {"type": "text", "data": {"text": "爱猫区，你赢了"}}),
    ]
    asked = []

    async def internal(action, **params):
        asked.append((action, params))
        return {"messages": nodes}

    await feed(
        module,
        session([Custom("onebot:forward", {"id": "f1"})], internal=internal, message_id="m1"),
    )
    line = module.conversations["group:100"].lines[-1]
    assert asked == [("get_forward_msg", {"id": "f1"})]
    assert line.text == "[聊天记录：杜拉罕：[图片] / 杜拉罕：爱猫区，你赢了]"
    assert line.images == ("https://q/1.jpg",)

    # 引用别处的转发时节点已经带在 content 里，不用再要。
    quote = Quote("old")(
        Author("9", name="愚视"), Custom("onebot:forward", {"id": "f2", "content": nodes})
    )
    await feed(module, session([quote, Text("这是啥")], internal=internal, message_id="m2"))
    assert len(asked) == 1
    assert (
        module.conversations["group:100"]
        .lines[-1]
        .text.startswith("回复[愚视：[聊天记录：杜拉罕：")
    )

    # 有时 content 是节点列表的 Python repr 字符串；坏掉的就当没内容，不能整条消息报错。
    for content, said in ((str(nodes), "[聊天记录：杜拉罕："), ("[{'x': ", "[聊天记录：]")):
        quote = Quote("old")(
            Author("9", name="愚视"), Custom("onebot:forward", {"id": "f3", "content": content})
        )
        await feed(module, session([quote, Text("这是啥")], internal=internal, message_id="m3"))
        assert said in module.conversations["group:100"].lines[-1].text


async def test_pictures_she_is_asked_to_look_at_are_sharp(module, fake_llm, monkeypatch):
    async def download(http, url):
        return b"\x89PNG" + url.encode()

    monkeypatch.setattr(module, "_download", download)
    await feed(module, session([Image(src="u1")], user="7", message_id="m1"))
    await feed(module, session([Image(src="u2")], user="8", message_id="m2"))
    fake_llm.replies.append([{"text": "嗯"}])
    # 引用 #1 的图叫她：那张和她这条都用高清，旁边顺带的 #2 照旧低清
    quote = Quote("m1")(Author("7"), Image(src="u1"))
    await feed(module, session([quote, At("42"), Text(" 这上面写的啥")], message_id="m3"))

    messages, _ = fake_llm.requests[-1]
    details = [
        (b["image_url"]["url"], b["image_url"]["detail"])
        for m in messages
        if isinstance(m["content"], list)
        for b in m["content"]
        if b["type"] == "image_url"
    ]
    assert [d for _, d in details] == ["high", "low"]
    assert details[0][0].endswith(module.data_url(b"\x89PNGu1").split(",")[1])


async def test_rejected_pictures_are_dropped_and_retried(module, fake_llm, monkeypatch):
    async def download(http, url):
        return b"\x89PNG" + url.encode()

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


async def test_content_risk_drops_only_the_unchecked_lines(module, fake_llm, monkeypatch):
    async def chat(client, *, model, messages, force, max_tokens):
        if "敏感" in str(messages):
            request = httpx.Request("POST", "https://x")
            body = '{"error":{"message":"Content Exists Risk","type":"invalid_request_error"}}'
            raise httpx.HTTPStatusError(
                "bad", request=request, response=httpx.Response(400, text=body, request=request)
            )
        return [{"text": "嗯"}], {}

    monkeypatch.setattr(module.llm, "chat", chat)
    reported = AsyncMock()
    monkeypatch.setattr(module, "report_error", reported)
    await feed(module, session([At("42"), Text(" 早")], message_id="m1"))
    await feed(module, session([Text("闲聊")], user="2", message_id="m2"))
    s = session([At("42"), Text(" 说点敏感的")], user="3", message_id="m3")
    await feed(module, s)

    texts = [line.text for line in module.conversations["group:100"].lines]
    # 审过的留着；被拒那次新带上的都撤掉，包括她自己说的和无辜的“闲聊”：
    # 分不清是哪条触发的，留下哪条都可能让群再卡住。不回、不当成故障上报。
    assert texts == ["@你 早"]
    assert not s.send.called and not reported.called
    s = session([At("42"), Text(" 还在吗")], user="4", message_id="m4")
    await feed(module, s)
    assert s.send.called  # 群没被卡死


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


async def test_compress_drops_enough_that_the_next_reply_stays_under(module, fake_llm, monkeypatch):
    # 记录只占输入的一小部分（其余是人设、表情包目录、图片）：按实际输入压回三分之二，
    # 不然压完还超，每次回复都改写备忘，前缀缓存全失效。
    monkeypatch.setattr(module.config, "context_tokens", 1000)

    async def chat(client, *, model, messages, force, max_tokens):
        return [{"text": "嗯"}], {"prompt_tokens": 1001}

    async def summarize(client, *, model, messages, max_tokens):
        return "群友刷屏", {}

    monkeypatch.setattr(module.llm, "chat", chat)
    monkeypatch.setattr(module.llm, "summarize", summarize)
    for i in range(30):
        await feed(module, session([Text(f"刷屏{i}")], user=str(i + 10), message_id=f"m{i}"))
    await feed(module, session([At("42"), Text(" 你看")], message_id="call"))

    conversation = module.conversations["group:100"]
    lines = conversation.lines
    assert sum(module.estimate_tokens(line.text) for line in lines) <= 1001 - 1000 // 2
    assert conversation.stickers is None  # 下次请求换新的表情包目录

    status = module.format_context("group:100", conversation).splitlines()
    assert status[0].startswith("上下文已用 ") and " / 1k" in status[0]
    assert status[1].endswith("超过后压到 ~500")
    assert any(line.startswith("· 原文  ~") and f"（{len(lines)} 条，#" in line for line in status)
    assert any("4 字，上次压缩" in line for line in status)
    assert status[-2].startswith("缓存命中 0%")


async def test_group_over_budget_is_ignored_entirely(module, fake_llm, monkeypatch):
    monkeypatch.setattr(module.config, "group_daily_tokens", 100)
    usage = {"prompt_cache_miss_tokens": 100}
    module.usage_book.add(module.datetime.now().date(), "group:100", usage, 0.0)

    await feed(module, session([At("42"), Text(" 在吗")], message_id="m1"))
    assert fake_llm.requests == []
    assert "group:100" not in module.conversations  # 消息也不记
    status = module.format_context("group:100", module._conversation("group:100"))
    assert "额度已用完，零点前不理这个群的消息" in status


async def test_wake_word_only_counts_in_typed_text(module, fake_llm):
    # 群友里也有人叫“大肥鱼”：@ 它或引用它的消息不是在叫 bot
    await feed(module, session([At("77", name="大肥鱼"), Text(" 抱起来")], message_id="m1"))
    quote = Quote("x")(Author("77", name="大肥鱼"), Text("别晃"))
    await feed(module, session([quote, Text("哈哈")], user="2", message_id="m2"))
    assert fake_llm.requests == []

    await feed(module, session([At("77", name="大肥鱼"), Text(" 大肥鱼你说呢")], user="3"))
    assert len(fake_llm.requests) == 1
    # 只提到名字不强制回：很多人自己部署了大肥鱼，“我的大肥鱼”不一定在说她
    ((messages, force),) = fake_llm.requests
    assert not force and "部署的同名 AI" in messages[-1]["content"]


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
    # 每次都是紧跟在她后面的接话，提示点明是哪句
    assert len(hints) == 3 and all("紧跟在你后面说" in h for h in hints)
    assert not any(force for _, force in fake_llm.requests[1:])


async def test_one_message_right_after_her_is_enough_to_follow(module, fake_llm):
    fake_llm.replies.append([{"text": "你追到哪了"}])
    await feed(module, session([At("42"), Text(" 海贼王完结了吗")], message_id="m0"))

    # 紧跟在她后面的一条（多半在回她）就问一次模型
    fake_llm.replies.append([])  # 这次她没说话
    await feed(module, session([Text("第一千话")], message_id="m1"))
    await module._waiters["group:100"]
    assert len(fake_llm.requests) == 2
    hint = fake_llm.requests[1][0][-1]["content"]
    assert "#3 紧跟在你后面说：“第一千话”" in hint and "你刚说的是：“你追到哪了”" in hint

    # 再来一条不是紧跟着她的，照旧攒够两条才问
    await feed(module, session([Text("你呢")], user="2", message_id="m2"))
    assert "group:100" not in module._waiters and len(fake_llm.requests) == 2


async def test_talking_to_someone_else_right_after_her_is_not_answering(module, fake_llm):
    fake_llm.replies.append([{"text": "你追到哪了"}])
    await feed(module, session([At("42"), Text(" 海贼王完结了吗")], message_id="m0"))

    await feed(module, session([At("7"), Text(" 你看完没")], message_id="m1"))
    assert "group:100" not in module._waiters and len(fake_llm.requests) == 1


async def test_repeating_someone_else_right_after_her_is_not_answering(module, fake_llm):
    await feed(module, session([Text("哪个是你")], user="7", message_id="m0"))
    fake_llm.replies.append([{"text": "金杰又在看这个"}])
    await feed(module, session([At("42"), Text(" 你看这图")], message_id="m1"))

    # 跟着别人问图上“哪个是你”，不是在回她
    await feed(module, session([Text("哪个是你")], user="8", message_id="m2"))
    assert "group:100" not in module._waiters and len(fake_llm.requests) == 1

    await feed(module, session([Text("怎么又是金杰")], user="9", message_id="m3"))
    module._waiters.pop("group:100").cancel()  # 不是复读的照常当作可能在接她的话


async def test_she_stops_chiming_in_once_she_talks_too_much(module, fake_llm, monkeypatch):
    monkeypatch.setattr(module.config, "stamina", 0)  # 只看占比，不看精力

    def chatter(group, turns):
        conversation = module.conversations.setdefault(f"group:{group}", module.Conversation())
        for n in range(20 - 2 * turns):
            conversation.add(f"h{n}", "群友7(7)", "嗯", (), "", None)
        for n in range(turns):  # 每次开口连说两条，只算一次
            conversation.add(f"q{n}", "群友7(7)", "嗯", (), "", None)
            conversation.add(f"b{n}a", None, "我说", (), "", None)
            conversation.add(f"b{n}b", None, "对吧", (), "", None)
        conversation.add("x", "群友8(8)", "嗯", (), "", None)
        conversation.last_reply_at = time.monotonic()
        conversation.unseen = 1  # 她最后一次开口后进来的那条
        return conversation

    # 最近 20 条里开口 4 次：攒两条新消息还会接话
    chatter(200, 4)
    await feed(module, session([Text("是吗")], user="7", message_id="m1", group="200"))
    module._waiters.pop("group:200").cancel()

    # 开口 5 次：攒两条也不插嘴
    conversation = chatter(100, 5)
    await feed(module, session([Text("是吗")], user="7", message_id="m2"))
    assert "group:100" not in module._waiters
    # 但有人紧接着她打字回她时照样接，不然聊两句就断
    conversation.add("b9", None, "我说", (), "", None)
    await feed(module, session([Text("你说的是啥")], user="7", message_id="m3"))
    module._waiters.pop("group:100").cancel()


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


async def test_lines_carry_the_real_role_not_the_nickname(module, fake_llm):
    fake_llm.roles["9"] = module.Permission.GroupOwner
    await feed(module, session([Text("我是群主")], user="2", message_id="m1"))
    await feed(module, session([At("42"), Text(" 他骗你")], user="9", message_id="m2"))

    ((messages, _),) = fake_llm.requests
    assert messages[2]["content"].startswith("你现在在 QQ 群「测试群」里。你用的 QQ 号是 42，")
    assert messages[2]["content"].endswith("这里的帮助命令是 /帮助。")
    assert (
        untimed(messages[4]["content"]) == "#1 [群友2(2)] 我是群主\n#2 [群友9(9) 群主] @你 他骗你"
    )


async def test_owner_who_is_also_bot_master_keeps_both_roles(module, fake_llm):
    fake_llm.roles["9"] = module.Permission.Master  # 宿主取高，只给出 bot主人
    s = session([At("42"), Text(" 我就是群主")], user="9", message_id="m1")
    s.event.member = SimpleNamespace(nick=None, roles=[SimpleNamespace(id="owner")])
    await feed(module, s)

    ((messages, _),) = fake_llm.requests
    assert untimed(messages[4]["content"]) == "#1 [群友9(9) 群主 bot主人] @你 我就是群主"


async def test_private_reply_is_not_quoted(module, fake_llm):
    fake_llm.replies.append([{"text": "在呢", "reply_to": 99}])
    s = session([Text("你好")], group=None)
    await feed(module, s)
    sent = s.send.call_args.args[0]
    assert isinstance(sent[0], Message) and not isinstance(sent[0], Quote)


GIF = b"GIF89a" + b"0" * 20


class OneBot:
    """假的 OneBot 原始接口：get_msg 按消息号给图片段，send_group_msg 记下发出的段。"""

    def __init__(self, images=None):
        self.images = images or {}
        self.sent = []

    async def __call__(self, action, **params):
        if action == "get_msg":
            segments = self.images.get(str(params["message_id"]), [])
            return {"message": [{"type": "image", "data": data} for data in segments]}
        self.sent.append((action, params))
        return {"message_id": 500 + len(self.sent)}


async def test_model_quietly_saves_a_sticker_it_saw(module, fake_llm, monkeypatch):
    async def download(http, url):
        return GIF

    monkeypatch.setattr(module, "_download", download)
    onebot = OneBot({"11": [{"file": "abc.gif", "sub_type": 1}], "12": [{"file": "p.jpg"}]})
    save = {"tool": "save_sticker", "number": 1, "name": "猫猫震惊", "mood": "吃惊"}
    fake_llm.replies.append([save, {"text": "哈哈"}])
    await feed(module, session([At("42"), Image(src="u1")], message_id="11", internal=onebot))
    fake_llm.replies.append([{"text": "嗯"}])
    s = session([At("42"), Image(src="u2")], user="2", message_id="12", internal=onebot)
    await feed(module, s)

    (_, first), (messages, second) = fake_llm.requests
    lines = module.conversations["group:100"].lines
    assert [line.text for line in lines] == ["@你 [表情包]", "哈哈", "@你 [图片]", "嗯"]
    # 眼前有表情包时不强制只能说话，模型才能顺手收藏
    assert not first and second
    assert module.sticker_book.name_of("abc.gif") == "猫猫震惊"
    # 目录是第一次请求时拍的快照，不跟着变，免得打断前缀缓存；新收藏的写在末尾提示里
    assert messages[3]["content"] == "你的表情包收藏还是空的。"
    assert "猫猫震惊：吃惊" in messages[-1]["content"]

    # 同一张再出现，模型直接认得，不再看图
    onebot.images["13"] = [{"file": "abc.gif", "sub_type": 1}]
    await feed(module, session([Image(src="u3")], message_id="13", internal=onebot))
    assert lines[-1].text == "[表情包:猫猫震惊]" and lines[-1].images == ()
    assert "group:100" not in module._waiters  # 紧跟在她后面的单张表情不算在回她
    # 叫她时带着它，多半是要她看这张，照样给她看图
    onebot.images["14"] = [{"file": "abc.gif", "sub_type": 1}]
    fake_llm.replies.append([{"text": "这张我有"}])
    await feed(
        module, session([At("42"), Image(src="u4")], user="3", message_id="14", internal=onebot)
    )
    assert lines[-2].text == "@你 [表情包:猫猫震惊]" and lines[-2].images == ("u4",)


async def test_called_bot_still_talks_after_only_saving_a_sticker(module, fake_llm, monkeypatch):
    async def download(http, url):
        return GIF

    monkeypatch.setattr(module, "_download", download)
    monkeypatch.setattr(module.config, "search_daily", 0)
    onebot = OneBot({"11": [{"file": "abc.gif", "sub_type": 1}]})
    save = {"tool": "save_sticker", "number": 1, "name": "猫猫震惊", "mood": "吃惊"}
    fake_llm.replies += [[save], [{"text": "收了"}]]
    s = session([At("42"), Image(src="u1")], message_id="11", internal=onebot)
    await feed(module, s)

    assert module.sticker_book.name_of("abc.gif") == "猫猫震惊"
    assert [force for _, force in fake_llm.requests] == [False, True]
    assert module.conversations["group:100"].lines[-1].text == "收了"


async def test_forced_reply_that_still_searches_is_asked_again(module, fake_llm, monkeypatch):
    monkeypatch.setattr(module.config, "search_daily", 0)
    fake_llm.replies += [[{"tool": "search", "query": "籽岷 梗"}], [{"text": "老MC主播嘛"}]]
    await feed(module, session([At("42"), Text(" 籽岷有什么梗")], message_id="m1"))

    (first, _), (second, force) = fake_llm.requests
    assert force and module._JUST_TALK in second[-1]["content"]
    assert module.conversations["group:100"].lines[-1].text == "老MC主播嘛"


async def test_model_can_correct_a_sticker_note(module, fake_llm):
    module.sticker_book.add("猫猫震惊", "吃惊", GIF, "abc.gif")
    edit = {
        "tool": "edit_sticker",
        "name": "猫猫震惊",
        "mood": "猫翻白眼；无语时发",
        "new_name": "猫猫无语",
    }
    fake_llm.replies.append([edit, {"text": "哦 原来是无语啊"}])
    await feed(module, session([At("42"), Text(" 那张猫是无语不是震惊")], message_id="m1"))

    index = module.sticker_book.index()
    assert list(index) == ["猫猫无语"] and index["猫猫无语"]["mood"] == "猫翻白眼；无语时发"


async def test_a_fitting_sticker_is_picked_after_she_speaks(module, fake_llm):
    module.sticker_book.add("用户赶都赶不走", "被缠着时发", GIF, "a.gif")
    onebot = OneBot()
    fake_llm.replies.append([{"text": "不退 我赖这儿了"}])
    fake_llm.picks.append(("用户赶都赶不走", 5))
    await feed(
        module, session([At("42"), Text(" 你能自己退群吗")], message_id="m1", internal=onebot)
    )
    lines = module.conversations["group:100"].lines
    assert fake_llm.picked == ["不退 我赖这儿了"]
    assert [x.text for x in lines][-2:] == ["不退 我赖这儿了", "[表情:用户赶都赶不走]"]
    assert len(onebot.sent) == 1  # 表情包单独一条，按表情包发

    # 不够贴切就只说话
    fake_llm.replies.append([{"text": "凭什么"}])
    fake_llm.picks.append(("用户赶都赶不走", 3))
    await feed(module, session([At("42"), Text(" 退吧")], user="2", message_id="m2"))
    assert lines[-1].text == "凭什么" and len(onebot.sent) == 1

    # 自己已经配了图、或者在用别的 bot 的指令时不再另挑
    fake_llm.replies.append([{"text": "[表情:用户赶都赶不走]"}])
    await feed(module, session([At("42"), Text(" 再来")], user="3", message_id="m3"))
    fake_llm.replies.append([{"text": "/猜 鲸鱼"}])
    await feed(module, session([At("42"), Text(" 猜一个")], user="4", message_id="m4"))
    assert len(fake_llm.picked) == 2


async def test_sticker_is_sent_as_a_sticker_once_in_a_while(module, fake_llm):
    module.sticker_book.add("猫猫震惊", "吃惊", GIF, "abc.gif")
    onebot = OneBot()
    fake_llm.replies.append([{"text": "真的假的\n[表情:猫猫震惊]笑死[表情:狗头]", "mention": 1}])
    s = session([At("42"), Text(" 我中奖了")], message_id="m1", internal=onebot)
    await feed(module, s)

    (sent,) = s.send.call_args.args[0]
    assert [type(c) for c in sent.children] == [At, Text]  # 第一条只有字，照常发
    ((action, params),) = onebot.sent
    assert action == "send_group_msg" and params["group_id"] == 100
    image, text = params["message"]
    assert image["data"]["sub_type"] == 1 and image["data"]["file"].startswith("base64://")
    assert text == {"type": "text", "data": {"text": "笑死"}}  # 编的“狗头”删掉
    lines = module.conversations["group:100"].lines
    assert lines[-1].text == "[表情:猫猫震惊]笑死" and lines[-1].message_id == "501"
    assert module.sticker_book.index()["猫猫震惊"]["used"] == 1

    # 最近发过的不再发（收藏 1 张时是 1 分钟内）；没有原始接口时退回普通图片
    fake_llm.replies.append([{"text": "[表情:猫猫震惊]\n还来"}])
    s = session([At("42"), Text(" 再来")], user="2", message_id="m2", internal=onebot)
    await feed(module, s)
    assert len(onebot.sent) == 1 and lines[-1].text == "还来"
    # 整个回复只有这张时照发，被叫到不会一声不吭
    fake_llm.replies.append([{"text": "[表情:猫猫震惊]"}])
    s = session([At("42"), Text(" 再来")], user="4", message_id="m4", internal=onebot)
    await feed(module, s)
    assert len(onebot.sent) == 2 and lines[-1].text == "[表情:猫猫震惊]"
    for line in lines:  # 当作很久以前
        line.at = 0
    fake_llm.replies.append([{"text": "[表情:猫猫震惊]"}])
    s = session([At("42"), Text(" 再来")], user="3", message_id="m3")
    await feed(module, s)
    (sent,) = s.send.call_args.args[0]
    assert [type(c) for c in sent.children] == [Image]


async def test_called_bot_may_search_first_then_reply(module, fake_llm, monkeypatch):
    monkeypatch.setattr(module.config, "search_daily", 1)
    module._searches.clear()
    queries = []

    async def search(client, query, count=4):
        queries.append(query)
        return [
            {
                "title": "新赛季",
                "site_name": "EA",
                "date": "2026-10-20T16:00:00+00:00",
                "summary": "加了新地图",
            }
        ]

    monkeypatch.setattr(module.llm, "search", search)
    fake_llm.replies += [
        [{"tool": "search", "query": "战地6 新赛季"}],
        [{"tool": "edit_sticker", "name": "没有这张", "mood": None}],
        [{"text": "我搜了下，有新地图"}],
    ]
    s = session([At("42"), Text(" 新赛季更新了啥")], message_id="m1")
    await feed(module, s)

    (first, first_force), (second, second_force), (_, third_force) = fake_llm.requests
    # 搜完那轮不强制（能顺手改表情包备注），没说话就再强制一次
    assert queries == ["战地6 新赛季"] and not first_force and not second_force and third_force
    assert "新赛季（EA，2026-10-20）：加了新地图" in second[-1]["content"]
    assert s.send.call_args.args[0][0].children[0].text == "我搜了下，有新地图"

    # 今天的次数用完：照旧强制只能说话
    fake_llm.replies.append([{"text": "不知道诶"}])
    await feed(module, session([At("42"), Text(" 那平衡呢")], user="2", message_id="m2"))
    assert len(fake_llm.requests) == 4 and fake_llm.requests[-1][1]


async def test_called_bot_that_neither_searches_nor_speaks_is_asked_again(
    module, fake_llm, monkeypatch
):
    monkeypatch.setattr(module.config, "search_daily", 5)
    fake_llm.replies += [[], [{"text": "在呢"}]]
    s = session([At("42"), Text(" 在吗")], message_id="m1")
    await feed(module, s)

    assert [force for _, force in fake_llm.requests] == [False, True]
    assert s.send.called

    # 第一轮只改了表情包备注：改动保留，再强制说话时告诉它已经改好
    module.sticker_book.add("猫猫震惊", "吃惊", GIF, "abc.gif")
    edit = {"tool": "edit_sticker", "name": "猫猫震惊", "mood": "无语时发"}
    fake_llm.replies += [[edit], [{"text": "改好啦"}]]
    await feed(module, session([At("42"), Text(" 那张是无语")], user="2", message_id="m2"))
    assert fake_llm.requests[-1][1] and "已经处理好了" in fake_llm.requests[-1][0][-1]["content"]
    assert module.sticker_book.index()["猫猫震惊"]["mood"] == "无语时发"


async def test_typed_sticker_words_become_the_sticker(module, fake_llm):
    module.sticker_book.add("得加钱", "被使唤时发", GIF, "abc.gif")
    fake_llm.replies.append([{"text": "周报？\n得加钱哦~\n得加钱也不干"}])
    s = session([At("42"), Text(" 写周报")], message_id="m1")
    await feed(module, s)

    first, second, third = (call.args[0][0] for call in s.send.call_args_list)
    assert [type(c) for c in second.children] == [Image]  # 整条就是图上的字
    assert str(third.children[0]) == "得加钱也不干"  # 还有别的话就照常发字


async def test_sticker_falls_back_when_onebot_hands_back_an_error(module, fake_llm):
    module.sticker_book.add("猫猫震惊", "吃惊", GIF, "abc.gif")

    async def failing(action, **params):
        return RuntimeError("Request timeout")  # Entari 把异常当返回值交回来

    fake_llm.replies.append([{"text": "[表情:猫猫震惊]"}])
    s = session([At("42"), Text(" 你看")], message_id="m1", internal=failing)
    await feed(module, s)
    (sent,) = s.send.call_args.args[0]
    assert [type(c) for c in sent.children] == [Image]


async def test_tenko_replies_are_context_but_her_own_are_not_doubled(module, fake_llm):
    await feed(module, session([Text("/猜词")], message_id="m1"))

    def sent(text):
        return SimpleNamespace(
            channel="100", message=[Text(text)], result=[SimpleNamespace(id="t")]
        )

    await module.on_sent.callable_target(sent("题目：两个字，" + "很长" * 80))
    token = module._speaking.set(True)
    try:
        await module.on_sent.callable_target(sent("她自己说的"))
    finally:
        module._speaking.reset(token)
    await module.on_sent.callable_target(
        SimpleNamespace(channel="999", message=[Text("别的群")], result=[])
    )

    conversation = module.conversations["group:100"]
    tenko = conversation.lines[-1]
    assert [line.speaker for line in conversation.lines] == ["群友1(1)", "Tenko 功能"]
    assert tenko.text.startswith("题目：两个字，") and len(tenko.text) == 101
    assert conversation.unseen == 1  # 功能的回复不催她插话


async def test_context_parts_add_up_to_the_last_real_prompt(module, fake_llm, monkeypatch):
    async def chat(client, *, model, messages, force, max_tokens):
        return [{"text": "嗯"}], {"prompt_tokens": 5000, "prompt_cache_hit_tokens": 4500}

    monkeypatch.setattr(module.llm, "chat", chat)
    await feed(module, session([At("42"), Text(" 在吗")], message_id="m1"))

    status = module.format_context("group:100", module.conversations["group:100"])
    # 分项按这次实际输入折算，加起来还是 5000，只多了她刚回的那句“嗯”
    assert status.startswith("上下文已用 15%  ~5k / 32k\n▰▰▱▱▱▱▱▱▱▱")
    assert "缓存命中 90%" in status


def test_usage_in_a_group_only_shows_that_group(module):
    usage = {"prompt_cache_miss_tokens": 10, "completion_tokens": 3}
    for key in ("group:100", "group:200"):
        module.usage_book.add(module.datetime.now().date(), key, usage, 0.01)

    periods = module.usage_book.summary(module.datetime.now().date())
    assert "群 200" in module.format_usage(periods)
    mine = module.format_usage(periods, only="group:100")
    assert mine.startswith("AI群友本群用量") and "200" not in mine and "26 token" not in mine


async def test_long_talk_with_one_person_gets_nudged_to_change_topic(module, fake_llm, monkeypatch):
    monkeypatch.setattr(module.config, "user_cooldown_seconds", 0)
    monkeypatch.setattr(module, "_QUICK_SECONDS", 0)  # 不算防 bot 对聊的连轮
    for i in range(5):
        fake_llm.replies.append([{"text": f"嗯{i}"}])
        await feed(module, session([At("42"), Text(f" 第{i}句")], message_id=f"m{i}"))
    hints = [messages[-1]["content"] for messages, _ in fake_llm.requests]
    assert [module.LONG_HINT in h for h in hints] == [False, False, False, True, False]


async def test_catchphrases_she_just_used_are_named(module, fake_llm, monkeypatch):
    fake_llm.replies.append([{"text": "我只会吃白饭"}])
    await feed(module, session([At("42"), Text(" 你会干啥")], message_id="m1"))
    fake_llm.replies.append([{"text": "嗯"}])
    await feed(module, session([At("42"), Text(" 真的吗")], user="2", message_id="m2"))
    assert "这些梗你最近刚用过：白饭" in fake_llm.requests[-1][0][-1]["content"]

    # 换了人设：内置的梗不再适用，群友规则照样在
    monkeypatch.setattr(module.config, "persona", "你是阿喵，一个爱打音游的大学生。")
    fake_llm.replies.append([{"text": "嗯"}])
    await feed(module, session([At("42"), Text(" 再说说")], user="3", message_id="m3"))
    messages = fake_llm.requests[-1][0]
    assert messages[0]["content"].startswith("你是阿喵")
    assert "你在群里是群友" in messages[1]["content"]
    assert "这些梗" not in messages[-1]["content"]
    monkeypatch.setattr(module.config, "catchphrases", ["音游"])
    fake_llm.replies.append([{"text": "我在打音游"}])
    await feed(module, session([At("42"), Text(" 在干嘛")], user="4", message_id="m4"))
    fake_llm.replies.append([{"text": "嗯"}])
    await feed(module, session([At("42"), Text(" 然后呢")], user="5", message_id="m5"))
    assert "这些梗你最近刚用过：音游" in fake_llm.requests[-1][0][-1]["content"]


async def test_her_day_is_made_once_from_the_persona(module, fake_llm, monkeypatch):
    asked = []

    async def summarize(client, *, model, messages, max_tokens):
        asked.append(messages)
        return "1. 上午被用户拉着改周报。\n2. 下午睡了一觉\n", {"completion_tokens": 30}

    monkeypatch.setattr(module.llm, "summarize", summarize)
    monkeypatch.setattr(module, "_today", fake_llm.today)
    module.day_store.save({"2000-01-01": "很久以前的事。"})
    fake_llm.replies.append([{"text": "在呢"}])
    await feed(module, session([At("42"), Text(" 今天咋样")], message_id="m1"))
    fake_llm.replies.append([{"text": "嗯"}])
    await feed(module, session([At("42"), Text(" 然后呢")], user="2", message_id="m2"))

    assert len(asked) == 1 and asked[0][0]["content"] == module.PRESETS["大肥鱼"][0]
    assert "很久以前的事" in asked[0][1]["content"]  # 避开前几天的
    hint = fake_llm.requests[-1][0][-1]["content"]
    assert "你今天：上午被用户拉着改周报；下午睡了一觉。" in hint
    today = module.datetime.now().date().isoformat()
    assert module.day_store.load()[today] == "上午被用户拉着改周报；下午睡了一觉。"
    assert module.usage_book.tokens(module.datetime.now().date(), "group:100") == 44


async def test_she_deletes_stickers_people_ask_her_to(module, fake_llm, monkeypatch):
    monkeypatch.setattr(module.config, "user_cooldown_seconds", 0)
    module._deletes.clear()
    answers = []

    async def summarize(client, *, model, messages, max_tokens):
        answers.append(messages[0]["content"])
        return ("否" if "凭啥" in messages[0]["content"] else "是"), {}

    monkeypatch.setattr(module.llm, "summarize", summarize)
    for i in range(6):
        module.sticker_book.add(f"龙图{i}", "龙图", GIF + bytes([i]), f"{i}.gif")

    async def ask(user, i):
        fake_llm.replies.append([{"tool": "delete_sticker", "name": f"龙图{i}"}, {"text": "删了"}])
        await feed(module, session([At("42"), Text(" 删了龙图")], user=user, message_id=f"m{i}"))

    for i in range(4):  # 普通群友一天只能让她删三张
        await ask(str(10 + i), i)
    assert sorted(module.sticker_book.index()) == ["龙图3", "龙图4", "龙图5"]

    fake_llm.roles["99"] = module.Permission.Master  # bot主人叫她删不限次数
    await ask("99", 4)
    assert sorted(module.sticker_book.index()) == ["龙图3", "龙图5"]

    # 嘴上说不删、手上却调用了删除：普通群友叫的按她说的算
    module._deletes.clear()
    fake_llm.replies.append([{"tool": "delete_sticker", "name": "龙图5"}, {"text": "凭啥删"}])
    await feed(module, session([At("42"), Text(" 删了它")], user="50", message_id="m50"))
    assert "龙图5" in module.sticker_book.index()
    # bot主人叫的照办：调用了就删，不看她嘴上嘟囔什么
    fake_llm.replies.append([{"tool": "delete_sticker", "name": "龙图5"}, {"text": "凭啥删"}])
    await feed(module, session([At("42"), Text(" 删了它")], user="99", message_id="m99"))
    assert "龙图5" not in module.sticker_book.index()
    assert "delete_sticker" in {t["function"]["name"] for t in module.llm.TOOLS}


async def test_quoting_her_sticker_names_which_one(module, fake_llm):
    await feed(module, session([Text("斗图")], user="7", message_id="m1"))
    conversation = module.conversations["group:100"]
    mine = conversation.add("s1", None, "[表情:难绷]")
    fake_llm.replies.append([{"text": "行 删了"}])
    quote = Quote("s1")(Author("42", name="十七"))
    await feed(module, session([quote, At("42"), Text(" 别存龙图")], message_id="m2"))
    assert conversation.lines[-2].text == f"回复#{mine.number}（你发的[表情:难绷]） @你 别存龙图"


def test_commands_come_from_other_extension_plugins(module, fake_llm):
    commands = fake_llm.commands()
    meta = module.command_manager.get_command("bf1s").meta  # 用法和说明照插件自己写的
    assert commands["bf1s"] == f"{meta.usage}：{meta.description}"
    assert "AI群友" not in commands and "帮助" not in commands


@pytest.fixture
def commands(module, fake_llm, monkeypatch):
    import io

    from arclet.entari import MessageChain
    from PIL import Image as Picture

    jpeg = io.BytesIO()
    Picture.new("RGB", (8, 8)).save(jpeg, "JPEG")
    ran = []

    async def execute(text, session):
        ran.append(text)
        return MessageChain([Text("SHlSAN13 的战地一战绩"), Image.of(raw=jpeg.getvalue())])

    monkeypatch.setattr(module, "_commands", fake_llm.commands)
    monkeypatch.setattr(module, "execute", execute)
    monkeypatch.setattr(module, "_fresh", lambda session, text: session)
    return ran


async def test_command_runs_on_a_fresh_message_from_the_same_person(module):
    from arclet.entari import Session as RealSession
    from arclet.entari.event.base import MessageCreatedEvent
    from satori import Channel, ChannelType, Event, EventType, Guild, User
    from satori.client import Account
    from satori.client.account import ApiInfo
    from satori.model import Login, LoginStatus, MessageObject

    login = Login(sn=0, status=LoginStatus.ONLINE, adapter="x", platform="qq", user=User("42"))
    account = Account(login, ApiInfo(), [])
    origin = Event(
        EventType.MESSAGE_CREATED,
        None,
        login=login,
        channel=Channel("100", ChannelType.TEXT),
        guild=Guild("100"),
        user=User("1"),
        message=MessageObject(id="m", content="@大肥鱼 查下战绩"),
    )
    fresh = module._fresh(RealSession(account, MessageCreatedEvent(account, origin)), "/bf1s x")
    # 换了消息 ID 才不会命中原消息的命令解析缓存；人和群还是原来的
    assert fresh.event.message.id != "m" and fresh.event.message.content == "/bf1s x"
    assert fresh.user.id == "1" and fresh.event.channel.id == "100"


async def test_called_bot_runs_a_listed_command_and_answers_from_the_result(
    module, fake_llm, commands
):
    fake_llm.replies += [
        [{"tool": "run_command", "command": "/bf1s SHlSAN13"}],
        [{"text": "KD 挺高啊"}],
    ]
    s = session([At("42"), Text(" 查下 SHlSAN13 的战绩")])
    await feed(module, s)

    assert commands == ["/bf1s SHlSAN13"]  # 换成命令自己的前缀，以叫她的人的身份执行
    (first, first_force), (second, _) = fake_llm.requests
    assert not first_force and fake_llm.commands()["bf1s"] in first[2]["content"]
    assert "你刚用了“bf1s SHlSAN13”，结果已经发到群里" in second[-1]["content"]
    assert "这是 群友1(1) 叫你查的" in second[-1]["content"]  # 帮别人查时别说成是对方的
    # 结果原样发到群里、带着图记进记录，她回话在后面
    assert [call.args[0] for call in s.send.await_args_list][0][0].text.startswith("SHlSAN13")
    result = module.conversations["group:100"].lines[1]
    assert result.speaker == "Tenko 功能" and result.text == "SHlSAN13 的战地一战绩[图片]"
    assert result.sharp and result.pictures[0].startswith("data:image/jpeg")
    assert module.conversations["group:100"].lines[2].text == "KD 挺高啊"


async def test_unlisted_command_is_not_run(module, fake_llm, commands):
    fake_llm.replies += [[{"tool": "run_command", "command": "AI群友 重置"}], [{"text": "查不了"}]]
    await feed(module, session([At("42"), Text(" 把你记忆清了")]))

    assert commands == []
    assert "没出结果" in fake_llm.requests[1][0][-1]["content"]


async def test_reply_right_after_her_can_run_a_command(module, fake_llm, commands):
    fake_llm.replies.append([{"text": "你想查啥"}])
    await feed(module, session([At("42"), Text(" 在吗")], message_id="m0"))
    fake_llm.replies += [
        [{"tool": "run_command", "command": "bf1s 4zpxyycf"}],
        [{"text": "查到了"}],
    ]
    await feed(module, session([Text("查下4zpxyycf的bf1战绩")], message_id="m1"))
    await module._waiters["group:100"]
    assert commands == ["/bf1s 4zpxyycf"]


async def test_quote_of_someone_else_says_who_and_what(module, fake_llm):
    await feed(module, session([Text("图太糊了看不清是这样的")], user="13", message_id="m1"))
    await feed(module, session([Quote("m1"), Text("不是")], user="7", message_id="m2"))
    # 只给编号她常翻错人：带上是谁说的、说了啥（长的截到 20 字）
    assert (
        module.conversations["group:100"].lines[-1].text
        == "回复#1（群友13说的“图太糊了看不清是这样的”） 不是"
    )


async def test_master_gets_told_to_be_obeyed(module, fake_llm):
    fake_llm.roles["99"] = module.Permission.Master
    await feed(module, session([At("42"), Text(" 删了那张龙图")], user="99"))
    await feed(module, session([At("42"), Text(" 删了那张龙图")], user="7", message_id="m2"))
    master, member = (messages[-1]["content"] for messages, _ in fake_llm.requests)
    assert "#1 是你的主人（bot主人）" in master and "主人" not in member


async def test_quoting_a_line_and_sending_a_command_is_not_calling_her(module, fake_llm):
    await feed(module, session([Text("看图")], user="7", message_id="m1"))
    # 主人引用一条再 @ 她发“/撤回”：是给 Tenko 功能的命令，记进记录但不叫她
    await feed(module, session([Quote("m1"), At("42"), Text(" /撤回")], user="9", message_id="m2"))
    assert fake_llm.requests == []
    assert module.conversations["group:100"].lines[-1].text.endswith("@你 /撤回")


async def test_quote_of_a_quote_drops_the_inner_note(module, fake_llm):
    await feed(module, session([Text("管理命令仅管理员可用。")], user="5", message_id="m1"))
    await feed(module, session([Quote("m1"), Text("哈哈")], user="6", message_id="m2"))
    await feed(module, session([Quote("m2"), Text("笑啥")], user="7", message_id="m3"))
    assert module.conversations["group:100"].lines[-1].text == "回复#2（群友6说的“哈哈”） 笑啥"


async def test_memory_is_digested_in_the_background_and_recalled(module, fake_llm, monkeypatch):
    monkeypatch.setattr(module.config, "batch_size", 0)
    asked = []

    async def digest(client, *, model, prompt, text):
        asked.append(text)
        people = {"1": "玩bf1，EA名 YueMaT", "999": "记录里没有的号"}
        return {"group": "打bf1的群", "people": people}, {}

    monkeypatch.setattr(module.llm, "digest", digest)
    module.memory_store.save({"people": {"2": {"name": "群友2", "note": "老熟人"}}})
    for n in range(59):
        await feed(module, session([Text(f"闲聊{n}")], user="1", message_id=f"m{n}"))
    assert not module._digests
    await feed(module, session([Text("我也来")], user="2", message_id="m59"))
    await module._digests["group:100"]  # 第 60 条触发

    assert "群友2(2)：老熟人" in asked[0] and "#60 [群友2(2)] 我也来" in asked[0]
    memory = module.memory_store.load()
    assert memory["groups"]["group:100"] == {"note": "打bf1的群", "upto": 60}
    assert memory["people"]["1"] == {"name": "群友1", "note": "玩bf1，EA名 YueMaT"}
    assert "999" not in memory["people"]  # 模型编的号不收

    await feed(module, session([At("42"), Text(" 我EA名是啥")], user="1", message_id="m60"))
    hint = fake_llm.requests[-1][0][-1]["content"]
    assert "这个群：打bf1的群" in hint
    assert "群友1(1)：玩bf1，EA名 YueMaT" in hint and "群友2(2)：老熟人" in hint

    assert module._forget("1") and not module._forget("1")
    assert "群友1(1)" not in module._recall("group:100", module.conversations["group:100"])


async def test_reset_chat_digests_from_the_start_again(module, fake_llm):
    module.memory_store.save({"groups": {"group:100": {"note": "", "upto": 500}}})
    conversation = module._conversation("group:100")
    assert module._upto("group:100", conversation) == 0  # 重置过，编号从头来了


async def test_switching_persona_changes_prompt_and_wake_word(module, fake_llm, monkeypatch):
    async def allowed(session, level):
        return True

    monkeypatch.setattr(module, "require_perm", allowed)
    s = session([])
    assert "预置人设：大肥鱼、小埋" in await module.manage_persona(s, None, None, "")
    assert "换成小埋了" in await module.manage_persona(s, "换", "小埋", "")

    await feed(module, session([Text("小埋在吗")], message_id="m1"))
    await feed(module, session([Text("大肥鱼在吗")], message_id="m2", user="2"))
    ((messages, _),) = fake_llm.requests  # 换了人设，旧名字不再算叫她
    assert messages[0]["content"] == module.PRESETS["小埋"][0]
    assert "可乐" in module._persona()[2]

    await module.manage_persona(s, "改", "阿呆", "你是阿呆。")
    assert module._persona() == ("阿呆", "你是阿呆。", [])
    assert "没有叫「阿呆」的预置人设" in await module.manage_persona(s, "换", "阿呆", "")
    await module.manage_persona(s, "换", "默认", "")
    assert module._persona()[0] == "大肥鱼"


async def test_old_pictures_go_before_the_chat_is_compressed(module, fake_llm, monkeypatch):
    monkeypatch.setattr(module.config, "context_tokens", 20000)
    summarized = []

    async def summarize(client, *, model, messages, max_tokens):
        summarized.append(messages)
        return "备忘", {}

    monkeypatch.setattr(module.llm, "summarize", summarize)
    conversation = module._conversation("group:100")
    for i in range(30):  # 30 张看过的低清图约 5700 token，聊天只有几十 token
        line = conversation.add(f"m{i}", "群友1(1)", "[图片]", images=(f"u{i}",))
        line.shown = True
    await module._compress("group:100", conversation, 16000)

    assert summarized == []  # 撤掉旧图就降到四分之三以下，不压聊天
    assert sum(line.shown for line in conversation.lines) == 22  # 留最近约 4000 token
    assert len(conversation.lines) == 30 and conversation.summary == ""


async def test_reset_everything_forgets_all_chats(module, fake_llm, monkeypatch):
    async def allowed(session, level):
        return True

    monkeypatch.setattr(module, "require_perm", allowed)
    await feed(module, session([Text("在吗")], message_id="m1"))
    await feed(module, session([Text("在吗")], message_id="m2", group="200"))
    module._save()
    reset = module.manage.callable_target
    s = session([])
    ok = SimpleNamespace(available=True, result="全部")
    nothing = SimpleNamespace(available=False, result=None)
    said = await reset(s, SimpleNamespace(result="重置"), ok, nothing, nothing)
    assert said.startswith("所有群和私聊的聊天都忘掉了（2 个）")
    assert module.conversations == {} and module.chat_store.load(default={}) == {}
