import random
from datetime import date, datetime
from io import BytesIO

import pytest
import pytest_asyncio
from arclet.alconna import command_manager
from arclet.entari.command import _commands
from PIL import Image as Picture
from satori import Image, Text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool


def _at(*args) -> float:
    return datetime(*args).timestamp()


def _text(reply) -> str:
    if isinstance(reply, str):
        return reply
    return "".join(e.text for e in reply if isinstance(e, Text))


@pytest.fixture
def deck(module):
    from tenko_plugin_tarot import deck

    return deck


@pytest.fixture
def spreads(module):
    from tenko_plugin_tarot import spreads

    return spreads


@pytest_asyncio.fixture
async def clean(module, monkeypatch):
    """内存 SQLite 代替宿主给插件的库，每个用例从空的占卜记录开始。"""

    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(module.readings.Reading.__table__.create)
    monkeypatch.setattr(module.readings, "get_session", lambda: AsyncSession(engine))
    yield
    await engine.dispose()


# ---- 牌库 ----


def test_the_deck_has_all_78_cards_with_pictures(deck):
    cards = deck.cards()
    assert len(cards) == 78 and len(deck.major()) == 22
    for suit in deck.SUITS:
        assert sorted(c.number for c in cards.values() if c.arcana == suit) == list(range(1, 15))
    for card in cards.values():
        assert card.upright and card.reversed and card.name and card.name_en
        with Picture.open(card.path) as image:
            assert image.format == "JPEG" and image.width == 480
    assert (deck.DATA / "cards" / "back.jpg").is_file()


def test_names_are_cleaned_up(deck):
    cards = deck.cards()
    assert cards["wands-03"].name_en == "Three of Wands"  # 原数据写成了 II of Wands
    assert cards["swords-01"].name == "宝剑王牌" and cards["cups-13"].name == "圣杯王后"
    assert cards["major-17"].group == "大阿卡那 XVII"
    assert cards["pentacles-02"].group == "小阿卡那 · 星币"
    assert "内芯" not in cards["wands-07"].upright


@pytest.mark.parametrize(
    ("query", "card"),
    [
        ("星星", "major-17"),
        ("the star", "major-17"),
        ("STAR", "major-17"),
        ("皇后", "major-03"),
        ("宝剑三", "swords-03"),
        ("宝剑 3", "swords-03"),
        ("剑3", "swords-03"),
        ("宝剑ACE", "swords-01"),
        ("圣杯皇后", "cups-13"),
        ("钱币国王", "pentacles-14"),
        ("Queen of Cups", "cups-13"),
        ("命运", "major-10"),
    ],
)
def test_find_a_card_by_name_or_alias(deck, query, card):
    assert [c.id for c in deck.find(query)] == [card]


def test_find_falls_back_to_names_containing_the_words(deck):
    assert {c.id for c in deck.find("权杖")} == {f"wands-{n:02d}" for n in range(1, 15)}
    assert [c.id for c in deck.find("祭司")] == ["major-02"]
    assert deck.find("不存在的牌") == [] and deck.find("  ") == []


def test_reversed_pictures_are_upside_down(deck):
    upright, reversed = deck.picture("major-17"), deck.picture("major-17", True)
    assert upright.startswith(b"\xff\xd8\xff") and reversed.startswith(b"\xff\xd8\xff")
    with Picture.open(BytesIO(upright)) as a, Picture.open(BytesIO(reversed)) as b:
        top_left = a.convert("L").crop((0, 0, 60, 60)).resize((1, 1)).getpixel((0, 0))
        flipped = b.convert("L").crop((420, 900, 480, 960)).resize((1, 1)).getpixel((0, 0))
        assert abs(top_left - flipped) <= 8


# ---- 规则 ----


def test_daily_draws_one_major_card_and_spreads_use_the_whole_deck(spreads, deck):
    rng = random.Random(1)
    for _ in range(50):
        (card, _reversed), *rest = spreads.draw("daily", rng)
        assert not rest and deck.cards()[card].major
    celtic = spreads.draw("celtic", rng)
    assert len(celtic) == 10 and len({card for card, _ in celtic}) == 10
    seen = {card for _ in range(200) for card, _ in spreads.draw("three", rng)}
    assert any(not deck.cards()[card].major for card in seen)
    flips = [rev for _ in range(200) for _, rev in spreads.draw("three", rng)]
    assert 0.35 < sum(flips) / len(flips) < 0.65


def test_daily_and_celtic_cross_repeat_within_the_day(spreads):
    morning, night, tomorrow = _at(2026, 11, 2, 0, 1), _at(2026, 11, 2, 23, 59), _at(2026, 11, 3)
    assert spreads.check("daily", None, morning).kind == "new"
    assert spreads.check("daily", morning, night).kind == "again"
    assert spreads.check("daily", night, tomorrow).kind == "new"
    assert spreads.check("celtic", morning, night).kind == "again"
    assert spreads.check("celtic", night, tomorrow).kind == "new"
    assert spreads.check("celtic", morning, night, celtic_cross_daily=False).kind == "new"


def test_three_card_spread_waits_from_the_last_reading(spreads):
    last = _at(2026, 11, 2, 9, 59)
    # 按上次占卜起算，不按整点：10:00 不能马上再占
    assert spreads.check("three", last, _at(2026, 11, 2, 10, 0)) == spreads.Verdict("wait", 59)
    assert spreads.check("three", last, last + 3599).wait_minutes == 1
    assert spreads.check("three", last, last + 3600).kind == "new"
    assert spreads.check("three", last, last + 10, three_card_minutes=0).kind == "new"


def test_festival_notes(module):
    festivals = module.festivals
    assert festivals.note(date(2026, 11, 2), [("major-17", False)]) is None
    assert "情人节" in festivals.note(date(2027, 2, 14), [("major-17", False)])
    assert "真假" in festivals.note(date(2027, 4, 1), [("major-17", False)])
    assert "愚者" in festivals.note(date(2027, 4, 1), [("major-17", False), ("major-00", True)])


# ---- 占卜和记录 ----


async def test_daily_card_stays_the_same_all_day(module, clean):
    first = await module.readings.daily("1", now=_at(2026, 11, 2, 8))
    again = await module.readings.daily("1", now=_at(2026, 11, 2, 22))
    assert _text(first).startswith("你今天的塔罗牌是【")
    assert _text(again).startswith("你今天已经抽过了，今天的牌是【")
    assert _text(first).split("【")[1] == _text(again).split("【")[1]
    assert any(isinstance(e, Image) for e in first)
    readings = await module.readings.history("1")
    assert len(readings) == 1 and readings[0].kind == "daily"


async def test_three_card_spread_waits_and_shows_the_last_result(module, clean):
    reply = await module.readings.spread("three", "1", "甲", "这周的面试", now=_at(2026, 11, 2, 10))
    assert reply.startswith("三张牌阵「这周的面试」\n1. 过去：")  # 没有浏览器：文字版
    wait = await module.readings.spread("three", "1", "甲", "下周呢", now=_at(2026, 11, 2, 10, 20))
    assert wait.startswith("三张牌阵每 60 分钟只能占一次，再等 40 分钟吧。\n")
    assert wait.splitlines()[1].startswith("上次「这周的面试」：过去 ")
    later = await module.readings.spread("three", "1", "甲", "下周呢", now=_at(2026, 11, 2, 11))
    assert later.startswith("三张牌阵「下周呢」")
    assert [r.question for r in await module.readings.history("1")] == ["下周呢", "这周的面试"]


async def test_celtic_cross_is_once_a_day(module, clean):
    first = await module.readings.spread("celtic", "1", "甲", None, now=_at(2026, 11, 2, 10))
    again = await module.readings.spread("celtic", "1", "甲", "换个问题", now=_at(2026, 11, 2, 20))
    assert first.startswith("凯尔特十字\n1. 现状：") and first.count("\n") == 10
    assert again == f"你今天已经占过凯尔特十字了，这是今天的结果：\n{first}"
    # 别人不受影响
    other = await module.readings.spread("celtic", "2", "乙", None, now=_at(2026, 11, 2, 20))
    assert not other.startswith("你今天已经占过")


async def test_records_list_the_latest_readings(module, clean):
    assert await module.readings.records("1") == "你还没有占卜过，先来一张 /今日塔罗 吧"
    await module.readings.daily("1", now=_at(2026, 11, 1, 8))
    await module.readings.spread("three", "1", "甲", "面试", now=_at(2026, 11, 1, 9))
    for hour in range(10, 15):
        await module.readings.spread("three", "1", "甲", None, now=_at(2026, 11, 1, hour))
    lines = (await module.readings.records("1")).splitlines()
    assert lines[0] == "你最近的 5 次占卜：" and len(lines) == 6
    assert lines[1].startswith("11-01 14:00 三张牌阵：过去 ")
    assert lines[5].startswith("11-01 10:00 三张牌阵：")


def test_lookup_a_card(module):
    reply = module.readings.lookup("宝剑三")
    assert _text(reply).startswith("宝剑三 Three of Swords · 小阿卡那 · 宝剑\n")
    assert "\n正位：感情受到伤害" in _text(reply) and "\n逆位：" in _text(reply)
    assert any(isinstance(e, Image) for e in reply)
    assert module.readings.lookup("权杖").startswith("名字带“权杖”的牌有 14 张：权杖王牌、权杖二")
    assert module.readings.lookup("咖啡").startswith("没有找到“咖啡”这张牌")


def test_lore_has_facts(module):
    facts = module.lore.facts()
    assert len(facts) >= 15 and all(len(f) > 10 for f in facts)


# ---- 出图 ----


def test_spread_page_lists_every_position(module):
    render = module.render
    draws = [(f"major-{n:02d}", n % 2 == 1) for n in range(10)]
    page = render.context("celtic", draws, question="今年的工作", user="甲", at=_at(2026, 11, 2))
    html = render.html(page)
    for position in module.spreads.SPREADS["celtic"].positions:
        assert position in html
    assert html.count('class="face rev"') == 5 and "今年的工作" in html
    assert page["cards"][1]["img"].startswith("data:image/jpeg;base64,")


async def test_render_falls_back_without_a_browser(module):
    page = module.render.context("three", [("major-00", False)] * 3, images=False)
    assert await module.render.image(None, page) is None


# ---- 命令 ----


def _alconna(name):
    return next(c for c in command_manager.get_commands() if c.command == name)


def test_commands_reach_their_handlers(module):
    for name, handler in [
        ("今日塔罗", module.daily_command),
        ("塔罗牌", module.card_command),
        ("占卜", module.three_card_command),
        ("凯尔特十字", module.celtic_command),
        ("塔罗记录", module.records_command),
        ("塔罗知识", module.lore_command),
    ]:
        targets = [_commands.subscribers[i] for i in _commands.trie[f"/{name}"]]
        assert any(t.callable_target is handler for t in targets), name


def test_questions_keep_their_spaces(module):
    result = _alconna("占卜").parse("/占卜 这周 的面试")
    assert result.matched and result.query("first") == "这周"
    words = [e.text for e in result.query("rest") if isinstance(e, Text)]
    assert " ".join(["这周", *words]).split() == ["这周", "的面试"]
    assert _alconna("塔罗牌").parse("/塔罗牌").matched
