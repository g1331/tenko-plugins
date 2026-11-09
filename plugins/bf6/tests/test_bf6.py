from datetime import UTC, datetime, timedelta

import pytest
from arclet.alconna import command_manager
from arclet.entari.command import _commands
from satori import Image, Text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

PLAYER = {
    "nucleus_id": 7,
    "display_name": "abc",
    "platform": "cem_ea_id",
    "avatar_url": "a.png",
    "has_current_season_stats": True,
}
CAREER = {
    "kills": 120,
    "human_kills": 100,
    "deaths": 40,
    "matches": 10,
    "wins": 6,
    "losses": 4,
    "time_seconds": 7200,
    "damage": 123456,
    "headshots": 20,
    "xp": 2_500_000,
    "distance_m": 18374,
    "kd": 2.5,
    "kpm": 0.833,
    "spm": 412.6,
    "win_rate": 0.6,
    "accuracy": 0.281,
    "headshot_rate": 0.2,
    "best_class": "engineer",
}
STATS = {
    "nucleus_id": 7,
    "has_data": True,
    "career": CAREER,
    "bests": {"round_kills": 40, "longest_kill_m": 492},
    "redsec": {"matches": 5, "wins": 1, "kills": 9, "time_seconds": 3600, "win_rate": 0.2},
    "weapons": [
        {"id": "crb_m4a1", "kills": 60, "kpm": 2.4, "headshot_rate": 0.1, "time_seconds": 1500},
        {"id": "ar_m433", "kills": 30, "kpm": 1.1, "headshot_rate": 0.3, "time_seconds": 1600},
        # 击杀不到 10：按比率排时不上榜
        {"id": "crb_qbz192", "kills": 3, "kpm": 9.0, "time_seconds": 20},
        {"id": "crb_ak205", "kills": None, "time_seconds": None},
        {"id": "xx_new", "kills": 1, "time_seconds": 30},
    ],
    "weapon_categories": [
        {"id": "crb", "kills": 63, "kpm": 2.1},
        {"id": "ar", "kills": 30, "kpm": 1.1},
        {"id": "dmr", "kills": None},
    ],
    "vehicles": [
        {"id": "air_panthera", "kills": 12, "kpm": 2.0, "time_seconds": 300},
        {"id": "sur_m1a2sepv3", "kills": 5, "time_seconds": 100},
        {"id": "sur_CB90", "kills": 2, "time_seconds": 100},
    ],
    "vehicle_categories": [],
    "gadgets": [
        {"id": "c4", "uses": 4, "kills": 3},
        {"id": "repair", "uses": 90, "repairs": 50},
    ],
    "kits": [
        {"id": "assault", "kills": 20, "time_seconds": 1800, "kd": 1.5},
        {"id": "engineer", "kills": 80, "time_seconds": 5400, "kd": 2.6},
    ],
    "modes": [
        {"id": "bt", "matches": 3, "time_seconds": 1000, "win_rate": 0.6667},
        {"id": "cq", "matches": 7, "time_seconds": 6000, "win_rate": 0.5714},
    ],
    "maps": [{"id": "mpdumbo", "matches": 4, "time_seconds": 3000, "win_rate": 0.5}],
    "fetched_at": "2026-11-09T14:00:00Z",
}


@pytest.fixture
def api(module, monkeypatch):
    routes = {}
    calls = []

    async def fake_get(path, **params):
        calls.append(path)
        if path not in routes:
            raise module.api.Bf6Error(f"测试里没有 {path}")
        handler = routes[path]
        return handler(**params) if callable(handler) else handler

    monkeypatch.setattr(module.api, "get", fake_get)
    routes["calls"] = calls
    return routes


@pytest.fixture
def rendered(module, monkeypatch):
    """记下渲染请求；返回值决定是出图还是回退文字。"""

    calls = {"result": None}

    async def fake_render(service, template, context):
        # 模板和上下文必须对得上：StrictUndefined 下缺字段会直接报错。
        module.pages.html(template, context)
        calls.update(template=template, context=context)
        return calls["result"]

    monkeypatch.setattr(module.pages, "render", fake_render)
    return calls


@pytest.fixture
def player(module, api):
    def resolve(**params):
        if params["name"].casefold() == "abc":
            return PLAYER
        raise module.api.NoSuchPlayer(f"没有找到 EA 显示名为「{params['name']}」的玩家")

    api["/players/resolve"] = resolve
    api["/players/7/stats"] = STATS
    return api


@pytest.fixture
async def db(module, monkeypatch):
    """内存 SQLite 代替宿主给插件的库。"""

    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(module.binding.Binding.__table__.create)
    monkeypatch.setattr(module.binding, "get_session", lambda: AsyncSession(engine))
    yield
    await engine.dispose()


async def test_plugin_loads_on_by_default(runtime):
    assert runtime.loaded_plugins["bf6"].metadata.feature_scope == "group"


async def test_stats_card(module, player, rendered):
    rendered["result"] = b"jpeg"
    reply = await module.stats.stats_reply("abc", object())
    assert isinstance(reply[0], Image)
    assert reply[1] == Text("https://bfm.umrbotech.com/bf6/player/7?name=abc&platform=ea")
    ctx = rendered["context"]
    assert rendered["template"] == "stats.html"
    assert (ctx["kd"], ctx["human_kills"], ctx["platform"]) == ("2.50", "100", "EA")
    assert (ctx["kit"], ctx["top_mode"]) == ("工程兵", "征服")
    assert ctx["kit_image"] == "https://bfm.umrbotech.com/bf6/classes/engineer-wide.webp"
    cells = {c["label"]: (c["value"], c["unit"], c["note"]) for c in ctx["cells"]}
    assert cells["胜率"] == ("60.0%", "", "6 胜 4 负")
    assert cells["战斗时长"] == ("2.0", "小时", "")
    assert cells["经验"] == ("2.50", "M", "")
    combat = {c["label"]: (c["value"], c["unit"]) for c in ctx["combat"]}
    assert combat["伤害"] == ("12.3", "万")
    assert combat["移动距离"] == ("18", "km")
    # EA 没给的项显示“—”，不是 0
    assert combat["处决"] == ("—", "")
    engineer, assault = ctx["kits"]
    assert (engineer["name"], engineer["best"], engineer["share"]) == ("工程兵", True, 0.75)
    assert not assault["best"]
    assert [m["name"] for m in ctx["mix"]] == ["卡宾枪", "突击步枪"]
    m4, _, _, heli = ctx["signature"]
    assert (m4["name"], m4["category"], m4["share"]) == ("M4A1", "卡宾枪", "50.0%")
    assert m4["image"] == "https://bfm.umrbotech.com/bf6/weapons/crb_m4a1.webp"
    assert (heli["name"], heli["category"]) == ("Panthera KHT", "空中作战")
    assert heli["rate"] == "KPM 2.00 · 5 分钟"
    assert [m["name"] for m in ctx["modes"]] == ["征服", "突破"]
    assert ctx["maps"][0]["name"] == "曼哈顿大桥"
    assert {c["label"]: c["value"] for c in ctx["redsec"]}["吃鸡"] == "1"


async def test_stats_falls_back_to_text(module, player, rendered):
    reply = await module.stats.stats_reply("abc", None)
    assert reply.startswith("abc 的战地六生涯战绩\nKD 2.50（真人击杀 100 / 死亡 40）")
    assert "战斗时长 2.0 小时" in reply and "移动距离 18km" in reply
    assert "王牌装备：M4A1 60、M433 30、QBZ-192 3、Panthera KHT 12" in reply
    assert reply.endswith("\nhttps://bfm.umrbotech.com/bf6/player/7?name=abc&platform=ea")


async def test_names_from_the_api_win_and_unknown_ids_show_as_is(module, player, rendered):
    weapons = [{**STATS["weapons"][0], "name": "新名字", "image": "/x.webp"}, STATS["weapons"][4]]
    player["/players/7/stats"] = {**STATS, "weapons": weapons}
    await module.stats.items_reply("weapons", "abc", None, "", None, (2, 5))
    first, unknown = rendered["context"]["items"]
    assert (first["name"], first["image"]) == ("新名字", "https://bfm.umrbotech.com/x.webp")
    assert (unknown["name"], unknown["image"], unknown["category"]) == ("xx_new", None, "其他")


async def test_player_without_bf6_stats(module, player, rendered):
    player["/players/7/stats"] = {**STATS, "has_data": False}
    assert await module.stats.stats_reply("abc", None) == "abc 还没有战地六多人战绩"
    assert "template" not in rendered


async def test_unknown_player_message_comes_from_bf_manager(module, player):
    reply = await module.reply.answer(module.stats.stats_reply("nobody", None))
    assert reply == "没有找到 EA 显示名为「nobody」的玩家"


async def items(module, kind, group=None, keyword=None, sort=None, layout=(2, 5)):
    found = module.catalog.find_group(kind, group) if group else None
    return await module.stats.items_reply(kind, "abc", None, "", found, layout, keyword, sort)


async def test_weapon_ranking_filters_and_sorts(module, player, rendered):
    reply = await items(module, "weapons")
    ctx = rendered["context"]
    # 没有击杀的不上榜
    assert [i["name"] for i in ctx["items"]] == ["M4A1", "M433", "QBZ-192", "xx_new"]
    assert ctx["items"][0]["stats"][:2] == ["60", "2.40"]
    assert ctx["highlight"] == 0 and ctx["title"] == "武器排行"
    assert "1. M4A1（卡宾枪）击杀 60 | KPM 2.40" in reply

    await items(module, "weapons", "卡宾", sort="kpm")
    ctx = rendered["context"]
    assert [i["name"] for i in ctx["items"]] == ["M4A1"]
    assert ctx["title"] == "卡宾枪排行" and ctx["highlight"] == 1
    assert ctx["note"] == ["按KPM排", f"击杀满 {module.pages.MIN_RATE_KILLS} 才排名"]

    # -n 搜名字时没上榜的也列出，置灰
    await items(module, "weapons", keyword="QBZ", sort="KPM")
    assert [(i["name"], i["muted"]) for i in rendered["context"]["items"]] == [("QBZ-192", True)]

    await items(module, "weapons", layout=(1, 1))
    assert len(rendered["context"]["items"]) == 1


async def test_gadgets_and_vehicles(module, player, rendered):
    await items(module, "weapons", "配备")
    ctx = rendered["context"]
    assert ctx["title"] == "配备排行"
    assert [(i["name"], i["category"]) for i in ctx["items"]] == [
        ("修理工具", "其他装备"),
        ("C-4 炸药", "爆炸物"),
    ]
    await items(module, "vehicles", "空中")
    assert [i["name"] for i in rendered["context"]["items"]] == ["Panthera KHT"]
    await items(module, "vehicles", "地面")
    assert [i["name"] for i in rendered["context"]["items"]] == ["M1A2 SEPv3"]
    await items(module, "vehicles", "海上")
    assert [i["category"] for i in rendered["context"]["items"]] == ["海上作战"]


async def test_unknown_sort_lists_the_choices(module, player):
    reply = await module.reply.answer(items(module, "vehicles", sort="爆头率"))
    assert reply == "不认识的排序：爆头率，可以用 击杀、KPM、伤害、时长、里程"


def _match(id, ended, **fields):
    end = datetime(2026, 11, 9, 13, 0, tzinfo=UTC) - ended
    return {
        "id": id,
        "matches": 1,
        "wins": 0,
        "losses": 0,
        "modes": [],
        "maps": [],
        "kills": 0,
        "human_kills": 0,
        "deaths": 0,
        "score": 0,
        "time_seconds": 0,
        "damage": 0,
        "assists": 0,
        "revives": 0,
        "headshots": 0,
        "xp": 0,
        "kit": None,
        "weapons": [],
        "vehicles": [],
        "kd": None,
        "kpm": None,
        "started_after": (end - timedelta(minutes=10)).isoformat(),
        "observed_at": end.isoformat(),
        **fields,
    }


MATCHES = {
    "tracking": True,
    "tracked_since": "2026-11-05T16:17:34Z",
    "last_checked_at": "2026-11-09T13:00:00Z",
    "matches": [
        _match(
            3,
            timedelta(0),
            wins=1,
            modes=[{"id": "cq", "matches": 1, "wins": 1, "losses": 0}],
            maps=[{"id": "mpdumbo", "matches": 1, "wins": 1, "losses": 0}],
            human_kills=28,
            deaths=9,
            kd=3.11,
            score=9240,
            time_seconds=1710,
            kit="engineer",
            weapons=[{"id": "crb_m4a1", "kills": 17, "time_seconds": 900}],
            vehicles=[{"id": "sur_m1a2sepv3", "kills": 9, "time_seconds": 400}],
        ),
        _match(
            2,
            timedelta(hours=1),
            matches=3,
            wins=2,
            losses=1,
            modes=[
                {"id": "brsquad", "matches": 2, "wins": 1, "losses": 1},
                {"id": "rush", "matches": 1, "wins": 1, "losses": 0},
            ],
        ),
        # 北京时间前一天
        _match(1, timedelta(hours=22), matches=0),
    ],
}


async def test_recent_rounds(module, player, rendered, monkeypatch):
    player["/players/7/matches"] = MATCHES
    monkeypatch.setattr(module.pages, "datetime", _FrozenDatetime)
    reply = await module.stats.recent_reply("abc", None, "", 8)
    # 先读一次生涯，bf-manager 才会记录这名玩家
    assert player["calls"][-2:] == ["/players/7/stats", "/players/7/matches"]
    ctx = rendered["context"]
    today, yesterday = ctx["days"]
    assert (today["label"], today["matches"], today["wins"]) == ("今天 · 周一", 4, 3)
    win, merged = today["rounds"]
    assert (win["title"], win["outcome"], win["label"]) == ("曼哈顿大桥", "win", "胜利")
    assert win["subtitle"] == "征服 · 20:50–21:00"
    assert (win["kit"], win["gear"]) == ("工程兵", "M4A1 17 杀 · M1A2 SEPv3 9 杀")
    assert (win["kd"], win["minutes"]) == ("3.11", "28")
    assert win["backdrop"] == "https://bfm.umrbotech.com/bf6/maps/mpdumbo.webp"
    assert (merged["title"], merged["label"]) == ("3 场", "2 胜 1 负")
    assert merged["subtitle"].startswith("大逃杀 · 四人 ×2、突袭 · ")
    assert yesterday["label"] == "昨天 · 周日"
    assert yesterday["rounds"][0]["label"] == "未计场次"
    assert ctx["since"] == "11 月 6 日 00:17"
    assert "【今天 · 周一】4 场 3 胜" in reply

    await module.stats.recent_reply("abc", None, "", 1)
    assert [len(d["rounds"]) for d in rendered["context"]["days"]] == [1]


async def test_recent_rounds_before_any_were_recorded(module, player, rendered):
    player["/players/7/matches"] = {**MATCHES, "matches": []}
    reply = await module.stats.recent_reply("abc", None, "", 8)
    assert reply.startswith("abc 的战地六对局估算\n从 11 月 6 日 00:17 开始记录")
    assert rendered["context"]["empty"].startswith("从 11 月 6 日 00:17 开始记录")


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 9, 13, 30, tzinfo=UTC).astimezone(tz)


async def test_binding(module, player, rendered, db):
    reply = await module.reply.answer(module.stats.stats_reply(None, None, "qq1"))
    assert reply == "还没绑定：使用 /bf6绑定 你的EA名字，或者在命令后面带上名字"
    # 没绑定时把群名片当 EA 名字
    reply = await module.stats.stats_reply(None, None, "qq1", "[ABC] abc")
    assert reply.startswith("abc 的战地六生涯战绩")
    reply = await module.reply.answer(module.stats.stats_reply(None, None, "qq1", "nobody"))
    assert reply.startswith("还没绑定，按群名片 nobody 查：没有找到")

    assert await module.binding.bind_reply(None, "qq1") == "用法：/bf6绑定 <EA 名字>"
    assert await module.binding.bind_reply("abc", "qq1") == "已绑定 abc（EA 账号）"
    assert await module.binding.bind_reply(None, "qq1") == "当前绑定：abc（EA 账号）"
    assert (await module.stats.stats_reply(None, None, "qq1")).startswith("abc 的战地六")

    # 绑定后改了名：按旧名字解析不到，仍按绑定的号查
    player["/players/resolve"] = lambda **_: (_ for _ in ()).throw(module.api.NoSuchPlayer("没有"))
    assert (await module.stats.stats_reply(None, None, "qq1")).startswith("abc 的战地六")
    renamed = {**PLAYER, "display_name": "abd"}
    player["/players/resolve"] = lambda **_: renamed
    assert (
        await module.binding.bind_reply("abd", "qq1") == "已绑定 abd（EA 账号），改过名：abc → abd"
    )
    other = {**PLAYER, "nucleus_id": 8, "display_name": "zzz", "platform": "ps3"}
    player["/players/resolve"] = lambda **_: other
    assert (
        await module.binding.bind_reply("zzz", "qq1")
        == "绑定变更：abd（EA 账号） → zzz（PlayStation 账号）"
    )


def _alconna(name):
    return next(c for c in command_manager.get_commands() if c.command == name)


@pytest.mark.parametrize(
    ("handler", "names"),
    [
        ("bf6s", ["bf6s", "bf6战绩", "bf6生涯"]),
        ("bf6w", ["bf6w", "bf6武器"]),
        ("bf6v", ["bf6v", "bf6载具"]),
        ("bf6r", ["bf6r", "bf6对局", "bf6最近"]),
        ("bf6bind", ["bf6绑定", "bf6b"]),
    ],
)
def test_command_names_reach_the_same_handler(module, handler, names):
    for name in names:
        targets = [_commands.subscribers[i] for i in _commands.trie[f"/{name}"]]
        assert [t.callable_target for t in targets] == [getattr(module, handler)]
    main = _alconna(names[0])
    assert not main.meta.hide and all(f"/{n}" in main.meta.description for n in names[1:])


def test_item_arguments(module):
    result = _alconna("bf6w").parse("/bf6w abc 卡宾 -s KPM -n m4 -c 3 -t")
    assert result.matched
    assert (result.query("first"), result.query("second")) == ("abc", "卡宾")
    assert result.query("sort.sort") == "KPM" and result.query("search.keyword") == "m4"
    assert result.query("cols.cols") == 3 and result.query("text") is not None
    assert _alconna("bf6r").parse("/bf6r abc -n 12").query("count.count") == 12


def test_group_words(module):
    find = module.catalog.find_group
    assert find("weapons", "DMR").key == "dmr" and find("weapons", "喷子").key == "sg"
    assert find("weapons", "配备").key == "gadgets" and find("vehicles", "飞机").key == "air"
    assert find("weapons", "abc") is None
