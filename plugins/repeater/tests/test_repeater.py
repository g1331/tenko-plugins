import asyncio
import random
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from arclet.letoderea.utils import _EventSystem
from satori import (
    At,
    Channel,
    ChannelType,
    EventType,
    Guild,
    Image,
    Login,
    MessageObject,
    Text,
    User,
)
from satori.client import get_app
from satori.element import Custom
from satori.model import Event, Member
from tenko.events import MessageEventHandler


class Scripted(random.Random):
    """按顺序给出 random() 的结果；shuffle 固定为倒序，便于断言。"""

    def __init__(self, *values: float) -> None:
        super().__init__()
        self.values = list(values)

    def random(self) -> float:
        return self.values.pop(0)

    def shuffle(self, x) -> None:
        x.reverse()


@pytest.fixture
def chain(module):
    return module.chain


def make(chain, rng, *, digest=None, now=None):
    clock = now or [0.0]
    return chain.Repeater(
        chain.Settings(cooldown_seconds=600, interrupt_lines=("打断！",)),
        digest or AsyncMock(return_value=None),
        rng=rng,
        clock=lambda: clock[0],
    ), clock


def said(chain, user, text="草"):
    return chain.Said(user, text, [text], text=text)


async def test_plugin_loads_and_is_on_by_default_in_new_groups(runtime, module):
    from tenko.host.features import feature_service

    metadata = runtime.loaded_plugins["repeater"].metadata
    assert metadata.feature_scope == "group"
    assert "repeater" not in feature_service.plugin_defaults  # 没有声明“新群默认关闭”
    assert module.config.cooldown_seconds == 60


async def test_repeats_once_per_run_ignores_same_user_and_cools_down(chain):
    # 第 2 人：不打断(0.9)、跟读(0.1 < 0.15)
    repeater, clock = make(chain, Scripted(0.9, 0.1, 0.9, 0.1))
    assert await repeater.feed(("onebot", "g"), said(chain, "a")) is None
    assert await repeater.feed(("onebot", "g"), said(chain, "a")) is None  # 同一个人不算
    assert await repeater.feed(("onebot", "g"), said(chain, "b")) == ["草"]
    assert await repeater.feed(("onebot", "g"), said(chain, "c")) is None  # 这一串已经出过声

    await repeater.feed(("onebot", "g"), said(chain, "a", "好"))
    assert await repeater.feed(("onebot", "g"), said(chain, "b", "好")) is None  # 冷却中
    clock[0] = 601
    await repeater.feed(("onebot", "g"), said(chain, "a", "行"))
    assert await repeater.feed(("onebot", "g"), said(chain, "b", "行")) == ["行"]


async def test_chance_grows_with_each_extra_repeater(chain):
    # 第 2 人 0.2 > 15% 不跟；第 3 人 0.2 < 1 - 0.85² ≈ 27.75% 跟
    repeater, _ = make(chain, Scripted(0.9, 0.2, 0.9, 0.2))
    for user in "ab":
        assert await repeater.feed(("onebot", "g"), said(chain, user)) is None
    assert await repeater.feed(("onebot", "g"), said(chain, "c")) == ["草"]


async def test_interrupt_shuffles_text_or_falls_back_to_a_line(chain):
    # 子类只覆盖 random() 时，choice 也会取一次 random()
    repeater, clock = make(chain, Scripted(0.01, 0.01, 0.0))
    await repeater.feed(("onebot", "g"), said(chain, "a", "复读机"))
    assert await repeater.feed(("onebot", "g"), said(chain, "b", "复读机")) == ["机读复"]
    clock[0] = 601
    await repeater.feed(("onebot", "h"), said(chain, "a", "草"))
    assert await repeater.feed(("onebot", "h"), said(chain, "b", "草")) == ["打断！"]


async def test_images_compare_by_content_not_url(chain):
    digest = AsyncMock(side_effect=lambda src: "same")
    repeater, _ = make(chain, Scripted(0.9, 0.1), digest=digest)
    first = chain.Said("a", None, ["img1"], image="https://cdn/1")
    second = chain.Said("b", None, ["img2"], image="https://cdn/2")
    await repeater.feed(("onebot", "g"), said(chain, "a"))
    await repeater.feed(("onebot", "g"), first)
    digest.assert_not_called()  # 前一条是文字，不用下载
    assert await repeater.feed(("onebot", "g"), second) == ["img1"]


def test_parse_skips_commands_mentions_long_text_and_mixed_images(module):
    parse = module.parse
    assert parse([Text("/帮助")], "a", 100) is None
    assert parse([At("1"), Text("草")], "a", 100) is None
    assert parse([Text("长" * 101)], "a", 100) is None
    assert parse([Text("看"), Image(src="https://x")], "a", 100) is None
    mface = Custom("onebot:mface", {"emojiId": "e1", "emojiPackageId": "9", "url": "https://m"})
    parsed = parse([mface], "a", 100)
    assert parsed.key == "<mface:9:e1>" and parsed.elements[0].src == "https://m"


@pytest.mark.parametrize("interruption", ["text", "own_bot", "blacklisted", "quote", "reordered"])
async def test_listener_only_repeats_consecutive_group_messages(module, monkeypatch, interruption):
    from tenko.host.features import feature_service

    app = get_app()
    # 生产宿主把事件分发绑定到自己的循环，测试在这里绑定到 pytest 的循环。
    monkeypatch.setattr(_EventSystem, "loop", asyncio.get_running_loop())
    monkeypatch.setattr(feature_service, "_ready", True)
    monkeypatch.setattr(module.repeater, "rng", Scripted(0.9, 0.1))
    monkeypatch.setattr(module.repeater, "runs", {})
    monkeypatch.setattr(module.repeater, "cooldown_until", {})

    if interruption == "own_bot":
        monkeypatch.setattr(module, "online_accounts", lambda: (SimpleNamespace(self_id="20002"),))

    login = Login(platform="onebot", user=User("10001"))
    quote_requested = asyncio.Event()
    quote_ready = asyncio.Event()

    async def message_get(channel_id, message_id):
        quote_requested.set()
        await quote_ready.wait()
        return MessageObject(id=message_id, content="引用原文", user=User("20001"))

    send = AsyncMock(return_value=[])
    account = SimpleNamespace(
        self_id="10001",
        platform="onebot",
        protocol=SimpleNamespace(message_get=message_get, send_message=send),
    )
    monkeypatch.setitem(app.accounts, login.identifier, account)
    permissions = SimpleNamespace(
        blacklisted=lambda kind, platform, target: (
            interruption == "blacklisted" and kind == "user" and target == "20002"
        )
    )
    guard = MessageEventHandler(permissions=permissions).guard
    monkeypatch.setattr(
        app,
        "event_callbacks",
        [
            guard(callback) if callback == app.handle_event else callback
            for callback in app.event_callbacks
        ],
    )

    def event(user, content, sequence=None):
        channel_id = f"group-{interruption}"
        return Event(
            type=EventType.MESSAGE_CREATED,
            timestamp=datetime.now(),
            login=login,
            channel=Channel(channel_id, ChannelType.TEXT),
            guild=Guild(channel_id, "群"),
            member=Member(user=User(user)),
            user=User(user),
            message=MessageObject(id=f"m{user}", content=content),
            _data={"real_seq": str(sequence)} if sequence is not None else None,
        )

    reordered = interruption == "reordered"
    await app.post(event("20001", "哈哈哈", 101 if reordered else None), None)
    if reordered:
        await app.post(event("20003", "哈哈哈", 103), None)
        await app.post(event("20002", "xxx", 102), None)
    elif interruption == "quote":
        pending = asyncio.create_task(app.post(event("20002", '<quote id="m20001"/>xxx'), None))
        try:
            await asyncio.wait_for(quote_requested.wait(), timeout=2)
            await app.post(event("20003", "哈哈哈"), None)
        finally:
            quote_ready.set()
            await pending
    else:
        await app.post(event("20002", "xxx"), None)
        await app.post(event("20003", "哈哈哈"), None)
    send.assert_not_awaited()

    await app.post(event("20004", "哈哈哈", 104 if reordered else None), None)
    send.assert_awaited_once()
    assert [str(element) for element in send.await_args.args[1]] == ["<message>哈哈哈</message>"]
