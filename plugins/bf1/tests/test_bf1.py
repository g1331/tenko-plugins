from types import SimpleNamespace

import pytest
import pytest_asyncio
from arclet.alconna import command_manager
from arclet.entari.command import _commands
from satori import Author, Image, Quote, Text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

PERSONA = {"persona_id": 2, "display_name": "abc", "avatar_url": None}
STATS = {
    "summary": {
        "rank": 150,
        "skill": 324.28,
        "time_played_seconds": 3183813,
        "kills": 87541,
        "deaths": 35986,
        "kd": 2.4326,
        "kpm": 1.65,
        "sps": 33.0,
        "wins": 1737,
        "losses": 1530,
        "infantry_kills": 85224,
        "vehicle_kills": 2317,
        "assists": 5474,
        "revives": 6202,
        "heals": 43475,
        "repairs": 537,
        "dogtags": 1500,
        "max_killstreak": 39,
        "longest_headshot_meters": 441.87,
        "best_class": "assault",
    },
    "soldiers": [
        {"class": "medic", "kills": 50, "time_seconds": 7200},
        {"class": "assault", "kills": 100, "time_seconds": 3600},
    ],
}
WEAPONS = [
    {
        "name": "Wex",
        "category": "戰場裝備",
        "kills": 50,
        "headshots": None,
        "accuracy": 21.7,
        "time_seconds": 3000,
    },
    {
        "name": "費德洛夫自動步槍",
        "category": "半自動步槍",
        "kills": 6100,
        "headshots": 7,
        "accuracy": 20.4,
        "time_seconds": 2400,
        "skin_name": "克倫斯基",
        "skin_rarity": "Superior",
        "image": "w.png",
        "skin_image": "s.png",
    },
    {"name": "沒用過", "category": "手槍", "kills": None, "time_seconds": 0},
]
VEHICLES = [
    {"name": "戰馬", "category": "馬匹", "kills": 89, "destroyed": 13, "time_seconds": 9000}
]


@pytest.fixture
def api(module, monkeypatch):
    routes = {}

    async def fake_get(path, **params):
        # 和真接口一样：查不到的（比如没准备的单局详情）报 Bf1Error
        if path not in routes:
            raise module.Bf1Error(f"测试里没有 {path}")
        handler = routes[path]
        return handler(**params) if callable(handler) else handler

    monkeypatch.setattr(module, "_get", fake_get)
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
def player(api):
    api["/players/search"] = {
        "personas": [
            {**PERSONA, "persona_id": 1, "time_played_hours": 0},
            {**PERSONA, "time_played_hours": 884},
        ]
    }
    api["/stats/2"] = STATS
    api["/stats/2/weapons"] = {"weapons": WEAPONS}
    api["/stats/2/vehicles"] = {"vehicles": VEHICLES}
    api["/stats/2/ban"] = {"bfban": "hit", "bfeac": "unknown"}
    api["/stats/2/platoon"] = {
        "tag": "EA",
        "name": "Electronic Arts",
        "size": 77,
        "emblem_url": "e.png",
        "verified": True,
    }
    api["/stats/2/online"] = {"is_online": True, "server_name": "S1"}
    return api


async def test_plugin_loads_on_by_default(runtime):
    assert runtime.loaded_plugins["bf1"].metadata.feature_scope == "group"


async def test_stats_renders_the_real_account_as_an_image(module, player, rendered):
    rendered["result"] = b"jpeg"
    reply = await module.stats_reply("abc", object())
    assert isinstance(reply[0], Image)
    assert reply[1] == Text("https://bfm.umrbotech.com/bf1/player/2")
    ctx = rendered["context"]
    assert rendered["template"] == "stats.html"
    assert ctx["kd"] == "2.43"
    assert ctx["kpis"] == [("KPM", "1.65"), ("SPM", "1,980"), ("胜率", "53.2%"), ("技巧值", "324")]
    assert (ctx["online"], ctx["online_server"]) == (True, "S1")
    assert ctx["platoon"]["emblem_url"] == "e.png"
    assert ctx["rank_color"] == module.pages.GOLD
    medic, assault = ctx["classes"]
    assert (medic["name"], medic["time"], medic["share"], medic["ratio"]) == (
        "医疗兵",
        "2h 0m",
        "66.7%",
        100,
    )
    assert assault["best"] and assault["ratio"] == 50
    assert ctx["banned"] and ctx["banned_by"] == "BFBAN"
    assert [b["state"] for b in ctx["bans"]] == ["hit", "unknown"]
    assert ctx["splits"][0]["pct"] == 97
    assert ctx["team"][0] == ("复活", "6,202", "heart", "7.0")
    best_weapon = ctx["gear"][0][1][0]
    assert (best_weapon["name"], best_weapon["image"]) == ("费德洛夫自动步枪", "s.png")
    assert (best_weapon["skin"], best_weapon["rarity"]) == ("克伦斯基", "gold")
    assert not ctx["gear"][0][1][1]["skin"]  # 没装皮肤的不写
    assert html_has(
        module, ctx, "e.png", "正在游戏 · S1", "[EA]", 'class="skin rarity-gold">克伦斯基'
    )


def html_has(module, ctx, *parts):
    page = module.pages.html("stats.html", ctx)
    return all(part in page for part in parts)


async def test_stats_falls_back_to_text_and_survives_missing_extras(module, player, rendered):
    def down(**_):
        raise module.Bf1Error("x")

    player["/stats/2/ban"] = down
    player["/stats/2/online"] = down
    reply = await module.stats_reply("abc", None)
    assert reply.startswith("abc 的战地一战绩")
    assert reply.endswith("\nhttps://bfm.umrbotech.com/bf1/player/2")
    assert "最佳兵种 突击兵" in reply
    ctx = rendered["context"]
    assert [b["state"] for b in ctx["bans"]] == ["unknown", "unknown"]
    assert ctx["online"] is None and not ctx["banned"]


async def test_weapon_rows_sorted_with_rarity(module, player, rendered):
    reply = await module.items_reply("weapons", "abc", None)
    ctx = rendered["context"]
    rows = ctx["items"]
    assert ctx["tag"] == "EA"
    assert [r["name"] for r in rows] == ["费德洛夫自动步枪", "Wex"]
    assert (rows[0]["stars"], rows[0]["star_color"]) == (61, module.pages.BLUE)
    assert (rows[0]["skin"], rows[0]["rarity"]) == ("克伦斯基", "gold")
    assert rows[0]["stats"][3] == "0.1%"
    assert "1. 费德洛夫自动步枪 · 半自动步枪" in reply
    assert reply.endswith("/bf1/player/2#weapons")


async def test_vehicle_rows(module, player, rendered):
    await module.items_reply("vehicles", "abc", None)
    row = rendered["context"]["items"][0]
    assert row["stats"] == ["89", "0.59", "13", "2h 30m"]


async def test_errors_become_replies(module, api):
    api["/players/search"] = {"personas": []}
    assert await module._answer(module.stats_reply("nobody", None)) == "没找到玩家 nobody"


async def test_fuzzy_search_picks_the_exact_name(module, api):
    api["/players/search"] = {
        "personas": [
            {"persona_id": 1, "display_name": "YuEmAt34", "time_played_hours": None},
            {"persona_id": 2, "display_name": "YueMaT", "time_played_hours": None},
        ]
    }
    assert (await module._persona("yuemat"))["persona_id"] == 2
    reply = await module._answer(module._persona("yue"))
    assert reply == "没找到玩家 yue，相近的有：YuEmAt34、YueMaT"


async def test_servers_sorted_by_players(module, api, rendered):
    api["/servers"] = lambda **params: {
        "total": 12,
        "items": [
            {
                "name": f"[S{i}] test",
                "player_count": i,
                "max_player_count": 64,
                "queue_count": 3 if i == 11 else 0,
                "mode_display_name": "征服",
                "map_display_name": "西奈沙漠",
                "region_display_name": "亚洲",
                "game_id": i,
            }
            for i in range(12)
        ],
    }
    reply = await module.servers_reply("test", None)
    lines = reply.splitlines()
    assert lines[0] == "「test」找到 12 个服务器，人最多的 10 个："
    assert lines[1] == "1. [S11] test"
    assert "11/64[3] | 征服·西奈沙漠 | 亚洲 | gameId 11" in lines[2]
    assert rendered["context"]["servers"][0]["ratio"] == 17
    assert rendered["context"]["summary"] == "共找到 12 个有人的服务器，按人数显示前 10 个"
    api["/servers"] = {"total": 0, "items": []}
    assert "没找到" in await module.servers_reply("none", None)


@pytest_asyncio.fixture
async def db(module, monkeypatch):
    """内存 SQLite 代替宿主给插件的库。"""

    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(module.Binding.__table__.create)
        await conn.run_sync(module.JoinReview.__table__.create)
        await conn.run_sync(module.JoinRequest.__table__.create)
    monkeypatch.setattr(module, "get_session", lambda: AsyncSession(engine))
    yield
    await engine.dispose()


async def test_binding_lets_queries_skip_the_name(module, player, rendered, db):
    reply = await module._answer(module.stats_reply(None, None, "qq1"))
    assert reply == "还没绑定：使用 /bf1绑定 你的EA名字，或者在命令后面带上名字"
    assert await module.bind_reply(None, "qq1") == "用法：/bf1绑定 <EA 名字>"
    assert (await module.bind_reply("abc", "qq1")).startswith("已绑定 abc（PID 2）")
    assert await module.bind_reply(None, "qq1") == "当前绑定：abc（PID 2）"
    assert await module.bind_reply("abc", "qq1") == "已绑定 abc（PID 2）"
    # 同一个号改了名再绑，提示改名
    renamed = {**PERSONA, "display_name": "abd", "time_played_hours": 884}
    player["/players/search"] = {"personas": [renamed]}
    assert await module.bind_reply("abd", "qq1") == "已绑定 abd（PID 2），改过名：abc → abd"
    player["/players/2"] = PERSONA
    reply = await module.stats_reply(None, None, "qq1")
    assert reply.startswith("abc 的战地一战绩")


async def test_old_json_bindings_move_into_the_database(module, player, db):
    module._legacy.save({"qq1": {"pid": 9, "name": "old"}, "qq2": {"pid": 8, "name": "kept"}})
    assert await module.bind_reply("abc", "qq1") == "绑定变更：old（PID 9）→ abc（PID 2）"
    # 新绑定覆盖 JSON 里的旧值，JSON 里别人的绑定原样导入
    assert await module.bind_reply(None, "qq1") == "当前绑定：abc（PID 2）"
    assert await module.bind_reply(None, "qq2") == "当前绑定：kept（PID 8）"
    # 换了个号再绑，说一声原来的
    assert await module.bind_reply("abc", "qq2") == "绑定变更：kept（PID 8）→ abc（PID 2）"
    assert not module._legacy.path.exists()
    module._legacy.path.with_name("bindings.json.imported").unlink()


SERVER = {
    "name": "[S] test",
    "player_count": 2,
    "max_player_count": 64,
    "mode_display_name": "征服",
    "map_display_name": "西奈沙漠",
    "region_display_name": "亚洲",
    "game_id": 7,
}


async def test_server_detail_picks_the_busiest_match(module, api, rendered):
    api["/servers"] = {"total": 2, "items": [{**SERVER, "game_id": 6, "player_count": 1}, SERVER]}
    api["/servers/7"] = {
        "summary": SERVER,
        "description": "欢迎",
        "map_rotation": [{"map_display_name": "西奈沙漠", "mode_display_name": "征服"}],
        "extras": {"owner": {"display_name": "boss"}, "bookmark_count": 1200, "admins": [1]},
    }
    api["/servers/7/players"] = {
        "teams": [
            {
                "team_id": 0,
                "faction": "美国",
                "avg_rank": 75.5,
                "rank_150_count": 1,
                "players": [
                    {"display_name": "low", "rank": 3, "stats": None, "loading": True},
                    {"display_name": "top", "rank": 150, "is_admin": True, "stats": {"kd": 2.5}},
                ],
            }
        ],
        "queued": [{"display_name": "wait", "rank": 91}],
        "spectators": [{"display_name": "eye", "rank": 40, "loading": True}],
        "summary": {"loading_count": 3},
        "operation": {
            "maps": [
                {"name": "法欧堡", "sectors": 3},
                {"name": "苏伊士", "sectors": 4},
                {"name": "西奈沙漠"},
            ],
            "map_index": 1,
            "sectors_taken": 1,
            "attacker": "大英帝国",
            "defender": "不认识",
        },
    }
    reply = await module.server_reply("test", None)
    ctx = rendered["context"]
    assert ctx["owner"] == "boss" and ctx["roles"][0] == ("管理", 1)
    team = ctx["teams"][0]
    assert [p["name"] for p in team["players"]] == ["top", "low"]
    assert (team["players"][0]["admin"], team["players"][0]["kd_hot"], team["kd"]) == (
        True,
        True,
        "2.50",
    )
    assert [p["index"] for p in team["players"]] == [1, 2] and ctx["width"] == 1280
    assert team["flag"].startswith("data:image/png")
    op = ctx["operation"]
    assert [m["sectors"] for m in op["track"]] == [[True] * 3, [True, False, False, False], []]
    assert (op["where"], op["next"], op["defender"]) == ("区域 2/4 · 第 2/3 张图", "西奈沙漠", None)
    assert reply.endswith("其他 1 个：/bf1ss test\nhttps://bfm.umrbotech.com/bf1/server/7#players")
    assert ctx["waiting"] == [
        ("排队", "#34d399", [("wait", 91, False)]),
        ("观战", "#f87171", [("eye", 40, True)]),
    ]
    assert "排队（1）：wait" in reply and "观战（1）：eye（载入中）" in reply
    # 载入中：名字调暗、后面标出来；图例人数用 bf-manager 的计数（含没进名单的人）
    assert [p["loading"] for p in team["players"]] == [False, True] and ctx["loading"] == 3
    assert "美国（2）：low（载入中）、top" in reply and "载入中 3 人" in reply
    page = module.pages.html("server.html", ctx)
    assert page.count('class="ld"') == 2 and "载入中：3" in page

    def down(**_):
        raise module.Bf1Error("x")

    api["/servers/7/players"] = down
    # 服名里的短数字按名字搜，不当 gameId
    assert "其他 1 个：/bf1ss 3090" in await module.server_reply("3090", None)
    assert rendered["context"]["players_missing"]


async def test_overview(module, api, rendered):
    api["/overview"] = {
        "available": True,
        "players": {"total": 10708, "asia": 6541, "eu": 3701, "other": 466},
        "servers": {"total": 274},
        "queues": {"total": 133},
        "spectators": {"total": 29},
        "top_map_modes": [{"label": "征服 · 西奈沙漠", "servers": 29, "players": 1295}],
        "mode_distribution": [{"label": "征服", "players": 3}, {"label": "行动", "players": 1}],
        "history": [{"players": 10}, {"players": 20}],
    }
    reply = await module.overview_reply(None)
    ctx = rendered["context"]
    assert ctx["players"] == "10,708" and ctx["modes"][0] == ("征服", "3", 75)
    assert ctx["trend"]["line"] == "0.0,150.0 900.0,0.0"
    assert reply.startswith("战地一当前在线 10708 人，274 个服务器")
    assert reply.endswith("\nhttps://bfm.umrbotech.com/stats")
    api["/overview"] = {"available": False}
    assert await module._answer(module.overview_reply(None)) == "全服概况暂时没有数据，稍后再试"


async def test_join_request_shows_the_card_and_flags_problems(module, rounds, rendered, db):
    player = rounds
    rendered["result"] = b"jpeg"
    message, persona = await module.review_request("10", "新人", "问题：EA名字\n答案：abc", None)
    assert message[0] == Text("收到 新人(10) 的入群申请\n答案：abc\n检查：BFBAN 实锤\n")
    # 战绩卡片带玩家页链接，后面跟最近对局卡片
    assert isinstance(message[1], Image) and message[2].text.endswith("/bf1/player/2")
    assert isinstance(message[3], Image) and len(message) == 5
    assert message[-1].text.endswith("作为理由拒绝")
    assert persona["persona_id"] == 2
    await module.bind_reply("abc", "10")
    player["/stats/2/ban"] = {"bfban": "unknown", "bfeac": "unknown"}
    message, _ = await module.review_request("10", "新人", "abc", None)
    assert "检查：有效 ID" in message[0].text


async def test_join_request_without_a_usable_name(module, player, rendered, db):
    message, persona = await module.review_request("10", "新人", "问题：EA名字\n答案：", None)
    assert "答案：-\n检查：没填 EA 名字" in message[0].text and persona is None
    player["/players/search"] = {"personas": []}
    message, persona = await module.review_request("10", "新人", "nobody", None)
    assert "检查：没找到玩家 nobody" in message[0].text and persona is None
    assert len(message) == 2


async def test_join_request_flags_a_mismatched_binding_and_unplayed_account(
    module, rounds, rendered, db
):
    player = rounds
    player["/stats/2/rounds"] = {"items": [], "next_before": None}
    await module._bind("10", {"persona_id": 9, "display_name": "other"})
    message, _ = await module.review_request("10", "新人", "abc", None)
    assert "这个 QQ 绑定的是 other，不是同一个号" in message[0].text
    player["/stats/2"] = {**STATS, "summary": {**STATS["summary"], "time_played_seconds": 0}}
    message, _ = await module.review_request("10", "新人", "abc", None)
    assert message[0].text.endswith("检查：有这个 EA 账号，但没玩过战地一\n")


async def test_join_request_lists_recent_rounds(module, rounds, rendered, db):
    message, _ = await module.review_request("10", "新人", "abc", None)
    # 两张卡片都回退成文字，中间空一行，对局部分不带链接
    assert message[1].text.endswith("/bf1/player/2")
    assert message[2].text.startswith("\n\nabc 最近 3 局\n")
    assert "#rounds" not in message[2].text
    rounds["/stats/2/rounds"] = {"items": [ROUNDS[1]], "next_before": None}
    message, _ = await module.review_request("10", "新人", "abc", None)
    assert message[0].text.endswith("最近对局：bf-manager 90 天内没记录到\n")
    assert len(message) == 3


async def test_join_request_survives_a_rounds_failure(module, rounds, rendered, db):
    def broken(limit):
        raise module.Bf1Error("bf-manager 出错")

    rounds["/stats/2/rounds"] = broken
    message, persona = await module.review_request("10", "新人", "abc", None)
    assert "检查：BFBAN 实锤" in message[0].text and persona["persona_id"] == 2


def test_reply_to_the_review_message_finds_the_applicant(module):
    said = Text("收到 新人(10) 的入群申请\n答案：abc\n")
    quote = Quote("1")(Author("bot"), said)
    assert module._quoted_applicant([quote, Text("y")], {"bot"}) == "10"
    assert module._quoted_applicant([quote, Text("y")], {"other"}) is None
    other = Quote("1")(Author("bot"), Text("随便说的"))
    assert module._quoted_applicant([other], {"bot"}) is None
    assert module._quoted_applicant([Text("y")], {"bot"}) is None


def _admin(module, monkeypatch, approve, self_id="bot"):
    account = SimpleNamespace(
        self_id=self_id, protocol=SimpleNamespace(guild_member_approve=approve)
    )
    monkeypatch.setattr(module, "online_accounts", lambda: (account,))


async def test_admin_decides_and_approval_binds_the_name(module, db, monkeypatch):
    calls = []

    async def approve(flag, ok, comment):
        calls.append((flag, ok, comment))

    _admin(module, monkeypatch, approve)
    assert await module._claim("g", "10", "bot", "f1")
    await module._remember_persona("g", "10", PERSONA)
    assert await module.decide("g", "10", "Y") == "已同意 10 的入群申请"
    assert await module.bind_reply(None, "10") == "当前绑定：abc（PID 2）"
    assert await module._claim("g", "11", "bot", "f2")
    await module._remember_persona("g", "11", PERSONA)
    assert await module.decide("g", "11", "ID 不对") == "已拒绝 11 的入群申请，理由：ID 不对"
    assert calls == [("f1", True, ""), ("f2", False, "ID 不对")]
    assert await module.bind_reply(None, "11") == "用法：/bf1绑定 <EA 名字>"
    assert await module.decide("g", "11", "y") == "11 的入群申请已经处理过了"


async def test_a_request_waits_for_its_account_without_a_deadline(module, db, monkeypatch):
    calls = []

    async def approve(flag, ok, comment):
        calls.append(flag)

    _admin(module, monkeypatch, approve, self_id="other")
    assert await module._claim("g", "10", "bot", "f")
    reply = await module.decide("g", "10", "y")
    assert reply == "收到申请的号 bot 不在线，上线后再回复" and not calls
    # 一天后这个号上线了，申请照样能处理
    monkeypatch.setattr(module.time, "time", lambda: 1e12)
    _admin(module, monkeypatch, approve)
    assert await module.decide("g", "10", "y") == "已同意 10 的入群申请" and calls == ["f"]


async def test_joining_clears_the_pending_request(module, db):
    assert await module._claim("g", "10", "bot", "f")
    await module._drop_request("g", "10")
    assert await module.decide("g", "10", "y") == "10 的入群申请已经处理过了"


async def test_approval_failure_is_reported(module, db, monkeypatch):
    async def approve(flag, ok, comment):
        raise RuntimeError("不是管理员")

    _admin(module, monkeypatch, approve)
    assert await module._claim("g", "10", "bot", "f")
    assert await module.decide("g", "10", "y") == "处理 10 的入群申请失败：不是管理员"


async def test_the_first_admin_account_claims_the_request(module, db, monkeypatch):
    assert await module._claim("g", "10", "bot", "f")
    assert not await module._claim("g", "10", "bot2", "f")
    # 一分钟前的申请没审完、又申请了一次：照常处理新的
    now = module.time.time()
    monkeypatch.setattr(module.time, "time", lambda: now + 60)
    assert await module._claim("g", "10", "bot2", "f9")


def _bot(self_id, role, calls, fail=False):
    async def call_api(action, params):
        calls.append((self_id, action, params))
        if fail:
            raise RuntimeError("掉线了")
        return {"role": role} if action == "internal/get_group_member_info" else None

    return SimpleNamespace(self_id=self_id, protocol=SimpleNamespace(call_api=call_api))


async def test_joining_renames_a_bound_member_with_an_admin_account(module, player, db):
    calls = []
    member, admin = _bot("1", "member", calls), _bot("2", "admin", calls)
    assert await module.rename_member("100", "10", [member, admin]) is None
    assert calls == []
    await module._bind("10", PERSONA)
    # 绑定后改了名：用现在的名字
    player["/players/2"] = {**PERSONA, "display_name": "abd"}
    assert await module.rename_member("100", "10", [member, admin]) == "abd"
    assert calls[-1] == (
        "2",
        "internal/set_group_card",
        {"group_id": 100, "user_id": 10, "card": "abd"},
    )
    assert not any(c[0] == "1" and c[1].endswith("set_group_card") for c in calls)


async def test_renaming_skips_broken_accounts_and_gives_up_without_an_admin(module, api, db):
    await module._bind("10", PERSONA)

    def down():
        raise module.Bf1Error("查询服务暂时连不上，稍后再试")

    api["/players/2"] = down
    calls = []
    # 查不到现在的名字就用绑定时记下的
    named = await module.rename_member(
        "100", "10", [_bot("1", "admin", calls, fail=True), _bot("2", "owner", calls)]
    )
    assert named == "abc" and calls[-1][0] == "2"
    assert await module.rename_member("100", "10", [_bot("1", "member", [])]) is None


def _alconna(name):
    return next(c for c in command_manager.get_commands() if c.command == name)


@pytest.mark.parametrize(
    ("handler", "names"),
    [
        ("bf1s", ["bf1s", "stat", "生涯", "战绩"]),
        ("bf1w", ["bf1w", "wp", "weapon", "武器"]),
        ("bf1v", ["bf1v", "vc", "vehicle", "载具"]),
        ("bf1r", ["bf1r", "recent", "match", "最近", "对局"]),
        ("bf1bind", ["bf1绑定", "bf1b", "绑定", "bind"]),
        ("bf1ss", ["bf1ss", "ss", "搜服务器"]),
        ("bf1pl", ["bf1pl", "ds", "详细服务器"]),
        ("bf1", ["bf1", "bfstat"]),
        ("bf1wiki", ["bf1百科", "百科", "wiki"]),
        ("bf1info", ["bf1info", "info", "信息", "tyc", "天眼查"]),
        ("bf1tip", ["bf1tip", "tip", "小贴士"]),
        ("bf1c", ["bf1c", "收藏", "勋章", "狗牌", "collection"]),
    ],
)
def test_old_command_names_reach_the_same_handler(module, handler, names):
    """旧 bot 的 -stat、-武器 等改成 /stat、/武器，和主命令走同一个处理函数。"""

    for name in names:
        # entari 按命令名（带 Alconna 前缀）建派发表，每个名字要有自己的一条
        targets = [_commands.subscribers[i] for i in _commands.trie[f"/{name}"]]
        assert [t.callable_target for t in targets] == [getattr(module, handler)]
    main = _alconna(names[0])
    assert not main.meta.hide and all(f"/{n}" in main.meta.description for n in names[1:])
    for name in names[1:]:
        assert _alconna(name).meta.hide


def test_old_names_take_the_same_arguments(module):
    assert _alconna("最近").parse("/最近 abc").query("name") == "abc"
    assert _alconna("stat").parse("/stat").matched
    assert _alconna("ss").parse("/ss sakula").matched
    assert _alconna("百科").parse("/百科").matched
    assert _alconna("tip").parse("/tip 伦敦的呼唤").matched


def test_text_flag(module):
    """旧 bot 的 -t：玩家类命令加上就只回文字，放名字前后都行。"""

    for line in ["/info -t abc", "/天眼查 abc -t", "/stat -text", "/武器 步枪 abc -t", "/步枪 -t"]:
        result = _alconna(line[1:].split()[0]).parse(line)
        assert result.matched and result.query("text") is not None, line
    assert _alconna("info").parse("/info abc").query("text") is None
    assert _alconna("info").parse("/info -t abc").query("name") == "abc"
    service = object()
    assert module._service(_query(), service) is service
    assert module._service(_query(...), service) is None
    assert "-t" in _alconna("bf1info").meta.description


def _round(id, kills, deaths, *, status="finished", won=True, seconds=600, weapons=None):
    return {
        "round": {
            "id": id,
            "game_id": 9,
            "server_game_id": 9,
            "server_name": "S1",
            "map_name": "MP_Amiens",
            "map_display_name": "亚眠",
            "map_image_url": "m.jpg",
            "game_mode": "Conquest",
            "mode_display_name": "征服",
            "started_at": 1793200000,
            "ended_at": 1793201200,
            "observed_start": True,
            "settled": True,
            "winner": 1,
            "team1_faction": "大英帝国",
            "team2_faction": "德意志帝国",
            "player_count": 64,
        },
        "player": {
            "persona_id": 2,
            "name": "abc",
            "team": 1,
            "status": status,
            "won": won,
            "kills": kills,
            "deaths": deaths,
            "score": 10000,
            "assists": 0,
            "revives": 0,
            "heals": 0,
            "repairs": 0,
            "dogtags": 0,
            "vehicle_kills": 0,
            "seconds": seconds,
            "kit": "scout",
            "kd": kills / deaths if deaths else float(kills),
            "kpm": kills / seconds * 60 if seconds else None,
            "spm": 1000.0,
            "weapons": weapons,
        },
    }


ROUNDS = [
    _round(
        5,
        30,
        10,
        weapons=[
            {"kind": "vehicle", "name": "战马", "kills": 9, "shared_kills": True},
            {"kind": "weapon", "name": "罗斯 MKIII（步兵）", "image": "r.png", "kills": 20},
            {"kind": "weapon", "name": "军铲", "image": "s.png", "kills": 10},
            {"kind": "weapon", "name": "信号枪", "kills": 0, "seconds": 100},
        ],
    ),
    # 排队、观战、进服就走：没击杀也没死过
    _round(4, 0, 0, status="left", won=None, seconds=30),
    _round(3, 10, 20, status="left", won=None, seconds=300),
    _round(2, 5, 5, won=False, seconds=0),
]


@pytest.fixture
def rounds(player):
    player["/players/2"] = PERSONA
    player["/stats/2/rounds"] = lambda limit: {"items": ROUNDS[:limit], "next_before": None}
    return player


async def test_recent_rounds_card(module, rounds, rendered):
    rendered["result"] = b"jpeg"
    reply = await module.recent_reply("abc", object())
    assert str(reply).endswith("/bf1/player/2#rounds")
    ctx = rendered["context"]
    assert rendered["template"] == "recent.html"
    assert ctx["tag"] == "EA"
    assert [r["outcome"] for r in ctx["rounds"]] == ["胜利", "中途退出", "失败"]
    first = ctx["rounds"][0]
    assert first["when"].startswith("2026-")
    assert first["minutes"] == "在场 10 分钟"
    # 战马的击杀和骑马时的武器重复，不列；没击杀的不列
    assert [g["name"] for g in first["gear"]] == ["罗斯 MKIII（步兵）", "军铲"]
    assert dict(ctx["kpis"])["胜 / 负"] == "1 / 1"
    assert dict(ctx["kpis"])["击杀 / 死亡"] == "45 / 35"


async def test_recent_rounds_fall_back_to_text(module, rounds, rendered, db):
    await module._bind("u1", PERSONA)
    reply = await module.recent_reply(None, None, "u1")
    assert reply.startswith("abc 最近 3 局\n")
    assert "罗斯 MKIII（步兵） 20 杀、军铲 10 杀" in reply
    assert reply.endswith("/bf1/player/2#rounds")


async def test_no_recent_rounds(module, rounds, rendered):
    rounds["/stats/2/rounds"] = {"items": [ROUNDS[1]], "next_before": None}
    reply = await module._answer(module.recent_reply("abc", None))
    assert reply.startswith("还没有记录到 abc 的对局")
    assert "template" not in rendered


def test_wiki_search(module):
    codex = module.codex
    assert len(codex.WIKI) == 310 and len(codex.TIPS) == 390
    # 旧 bot 的序号照用
    assert [e.name for e in codex.search_wiki("3")] == ["HELLRIEGEL 1915"]
    assert codex.search_wiki("311") == [] and codex.search_wiki("0") == []
    assert len(codex.search_wiki("武器")) == 123
    # 名字完全对上就只要这一条；不分大小写、不管空格
    assert [e.title for e in codex.search_wiki("mp 18")] == ["MP18 冲锋枪"]
    a7v = codex.search_wiki("a7v")[0]
    assert (a7v.title, a7v.taglines[0]) == ("A7V 坦克", "首款德式坦克")
    assert len(codex.search_wiki("坦克")) > 1
    # 名字里没有就搜正文
    assert [e.name for e in codex.search_wiki("托尔金")] == ["名人"]


def test_tip_search_covers_map_names(module):
    tips = module.codex.search_tips("亚眠")
    assert any("亚眠" in t.tags and "亚眠" not in t.text for t in tips)
    assert all("亚眠" in t.tags or "亚眠" in t.text for t in tips)
    assert module.format_tip(module.codex.Tip("记得防守旗帜！", ("征服", "抢攻"))) == (
        "【征服·抢攻】记得防守旗帜！"
    )


async def test_wiki_reply(module, rendered):
    rendered["result"] = b"jpeg"
    reply = await module.wiki_reply("托尔金", object())
    assert isinstance(reply[0], Image) and len(reply) == 1
    assert rendered["template"] == "wiki.html" and rendered["context"]["total"] == 310
    rendered["result"] = None
    text = await module.wiki_reply("mp18", None)
    assert text.startswith("#") and "解锁：用 MP18 冲锋枪进行" in text
    listed = await module.wiki_reply("坦克", None)
    assert listed.startswith("“坦克”找到") and "\n#" in listed
    catalog = await module.wiki_reply("全世界", None)
    assert catalog.startswith("“全世界”找到 17 条") and "、" in catalog
    missing = await module._answer(module.wiki_reply("不存在的东西", None))
    assert missing.startswith("百科里没找到“不存在的东西”")
    assert (await module.wiki_reply("", None)).startswith("#")


def test_tip_reply(module):
    assert module.tip_reply("")
    many = module.tip_reply("凡尔登")
    assert many.startswith("带“凡尔登”的小贴士有") and many.count("\n· ") == module.TIP_LIST
    with pytest.raises(module.Bf1Error):
        module.tip_reply("不存在的东西")


@pytest.fixture
def bots(module, monkeypatch):
    """群 100 里两个 bot 号：1 收到事件，2 是被戳的那个。"""

    sent = []

    def bot(self_id):
        async def send_message(channel, message):
            sent.append((self_id, channel, list(message)))

        return SimpleNamespace(
            self_id=self_id,
            platform="qq-client",
            protocol=SimpleNamespace(send_message=send_message),
        )

    accounts = [bot("1"), bot("2")]
    monkeypatch.setattr(module, "accounts_for_group", lambda group, platform: accounts)
    monkeypatch.setattr(module, "feature_enabled", lambda feature, group: True)
    monkeypatch.setattr(module, "_poked", {})
    return SimpleNamespace(accounts=accounts, sent=sent)


async def test_poking_a_bot_gets_a_tip_or_wiki_entry(module, bots, rendered, monkeypatch):
    receiver = bots.accounts[0]
    poke = {"group_id": 100, "user_id": 10, "target_id": 2}
    tip = module.codex.TIPS[0]
    monkeypatch.setattr(module.codex, "pick", lambda: tip)
    assert await module.poked(receiver, poke, None)
    # 被戳的号来回，@ 戳的人
    assert bots.sent == [("2", "100", [module.At("10"), Text(f"\n{module.format_tip(tip)}")])]
    # 冷却期内再戳不回
    assert not await module.poked(receiver, poke, None)
    monkeypatch.setattr(module.codex, "pick", lambda: module.codex.WIKI[2])
    rendered["result"] = b"jpeg"
    assert await module.poked(receiver, {**poke, "user_id": 11}, None)
    assert isinstance(bots.sent[-1][2][-1], Image)


async def test_pokes_that_are_not_for_us(module, bots, rendered):
    receiver = bots.accounts[0]
    # 戳群友、bot 互戳、私聊戳
    assert not await module.poked(receiver, {"group_id": 100, "user_id": 10, "target_id": 9}, None)
    assert not await module.poked(receiver, {"group_id": 100, "user_id": 1, "target_id": 2}, None)
    assert not await module.poked(receiver, {"user_id": 10, "target_id": 2}, None)
    assert bots.sent == []


def test_weapon_and_vehicle_groups(module):
    groups = module.groups
    assault = groups.find("weapons", "土鸡")
    assert assault.label == "突击兵"
    assert {"category": "衝鋒槍", "name": "MP18"} in assault
    assert {"category": "配備", "name": "炸藥"} in assault  # 繁体也认
    assert {"category": "配备", "name": "弹药箱"} not in assault
    assert {"category": "制式步枪", "name": "SMLE MKIII"} in groups.find("weapons", "狙击枪")
    ground = groups.find("vehicles", "地面")
    assert {"category": "机械巨兽", "name": "Char 2C"} in ground
    assert {"category": "机械巨兽", "name": "无畏舰"} not in ground
    assert groups.find("weapons", "坦克") is None and groups.find("vehicles", "坦克")
    # 每个旧名字只属于一组
    for kind_groups in groups.GROUPS.values():
        words = [w for g in kind_groups for w in g.names]
        assert len(words) == len(set(words))


def test_group_names_are_their_own_commands(module):
    for kind, word, label in [
        ("weapons", "步枪", "步枪"),
        ("weapons", "斟茶", "侦察兵"),
        ("vehicles", "飞艇", "飞船"),
    ]:
        targets = [_commands.subscribers[i] for i in _commands.trie[f"/{word}"]]
        handler = module.GROUP_HANDLERS[(kind, label)]
        assert [t.callable_target for t in targets] == [handler]
        assert _alconna(word).meta.hide
    assert "/突击兵" in _alconna("bf1w").meta.description
    assert _alconna("武器").parse("/武器 步枪 abc").matched


def _query(value=None):
    return SimpleNamespace(available=value is not None, result=value)


def test_category_and_name_in_either_order(module):
    split = module._group_and_name
    assert split("weapons", _query("步枪"), _query("abc"))[0].label == "步枪"
    group, name = split("weapons", _query("abc"), _query("突击兵"))
    assert (group.label, name) == ("突击兵", "abc")
    assert split("weapons", _query("abc"), _query()) == (None, "abc")
    assert split("vehicles", _query(), _query()) == (None, None)


async def test_items_by_group(module, player, rendered):
    rendered["result"] = b"jpeg"
    medic = module.groups.find("weapons", "医疗")
    await module.items_reply("weapons", "abc", object(), group=medic)
    ctx = rendered["context"]
    assert ctx["title"] == "武器 · 医疗兵"
    assert [i["name"] for i in ctx["items"]] == ["费德洛夫自动步枪"]
    rendered["result"] = None
    text = await module.items_reply(
        "vehicles", "abc", None, group=module.groups.find("vehicles", "飞机")
    )
    assert text.startswith("abc 还没有飞机击杀记录")


def test_item_layout_flags(module):
    """旧 bot 的 -c 几列 -r 几行，中文的 -列 -行 也认，位置随意。"""

    result = _alconna("武器").parse("/武器 步枪 abc -c 3 -r 4")
    assert (result.query("first"), result.query("second")) == ("步枪", "abc")
    assert (result.query("cols.cols"), result.query("rows.rows")) == (3, 4)
    result = _alconna("步枪").parse("/步枪 -行 2 -列 1 abc")
    assert (result.query("name"), result.query("cols.cols"), result.query("rows.rows")) == (
        "abc",
        1,
        2,
    )
    assert _alconna("载具").parse("/载具 -c 4").matched
    layout = module._layout
    assert layout(_query(), _query()) == (2, 5)
    assert layout(_query(3), _query(4)) == (3, 4)
    assert layout(_query(99), _query(0)) == (8, 1)


async def test_items_in_columns_and_rows(module, player, rendered):
    rendered["result"] = b"jpeg"
    many = [
        {"name": f"W{i}", "category": "步枪", "kills": 100 - i, "time_seconds": 600}
        for i in range(30)
    ]
    player["/stats/2/weapons"] = {"weapons": many}
    await module.items_reply("weapons", "abc", object(), layout=(3, 4))
    ctx = rendered["context"]
    assert len(ctx["items"]) == 12 and ctx["cols"] == 3 and ctx["width"] == 1485
    await module.items_reply("weapons", "abc", object())
    assert rendered["context"]["width"] == 1000 and len(rendered["context"]["items"]) == 10
    # 不够一行就收窄
    player["/stats/2/weapons"] = {"weapons": many[:2]}
    await module.items_reply("weapons", "abc", object(), layout=(4, 2))
    assert rendered["context"]["cols"] == 2 and rendered["context"]["width"] == 1000
    rendered["result"] = None
    player["/stats/2/weapons"] = {"weapons": many}
    text = await module.items_reply("weapons", "abc", None, layout=(1, 3))
    assert text.startswith("abc 的武器击杀前 3\n") and "\n3. W2 · " in text


async def test_players_by_pid(module, player, rendered):
    player["/players/2"] = PERSONA
    text = await module.stats_reply("#2", None)
    assert text.startswith("abc")
    wrong = await module._answer(module.stats_reply("#x2", None))
    assert wrong == "PID 只能是数字，例如 #1004198901469"


INFO_SERVERS = {
    "owner": [],
    "admin": [
        {
            "server_id": i,
            "game_id": i,
            "name": f"S{i}",
            "refreshed_at": 0,
            "player_count": 60 if i == 0 else None,
            "max_player_count": 64,
            "map_display_name": "亚眠" if i == 0 else None,
        }
        for i in range(10)
    ],
    "vip": [],
    "ban": [{"server_id": 99, "game_id": 99, "name": "B", "refreshed_at": 0}],
    "tracked_servers": 369,
}


def _elsewhere(item, game_id, name):
    return {**item, "round": {**item["round"], "server_game_id": game_id, "server_name": name}}


async def test_player_info(module, player, rendered):
    player["/stats/2/servers"] = INFO_SERVERS
    # 新的在前：S1 两局（中间夹一局进服就走的不算），然后去了 R2
    rounds = [ROUNDS[0], ROUNDS[1], _elsewhere(ROUNDS[2], 7, "R2"), ROUNDS[3]]
    player["/stats/2/rounds"] = lambda limit: {"items": rounds[:limit], "next_before": None}
    rendered["result"] = b"jpeg"
    reply = await module.info_reply("abc", object())
    # 名字和 PID 用文字跟在图后面，好复制
    assert isinstance(reply[0], Image)
    caption = str(reply[1])
    assert caption.startswith("玩家名：abc\nPID：2\nhttp") and caption.endswith("/bf1/player/2")
    ctx = rendered["context"]
    assert rendered["template"] == "info.html" and ctx["platoon"]["tag"] == "EA"
    admin = dict((r["key"], r) for r in ctx["roles"])["admin"]
    assert (admin["total"], len(admin["rows"]), admin["more"]) == (10, 8, 2)
    assert admin["rows"][0] == {"name": "S0", "map": "亚眠", "players": "60/64"}
    recent = [(r["name"], r["rounds"], r["where"]) for r in ctx["recent"]]
    assert recent == [("S1", 2, "亚眠 · 征服"), ("R2", 1, "亚眠 · 征服")]
    assert ctx["recent"][0]["when"].startswith("2026-")
    rendered["result"] = None
    text = await module.info_reply("abc", None)
    assert text.startswith("abc（PID 2）\n战队：[EA] Electronic Arts\n正在游戏：S1 | ")
    assert "BFBAN 实锤 | BFEAC 无信息" in text
    assert "私服身份（bf-manager 收录的 369 个私服）：服主 0 · 管理 10 · VIP 0 · 封禁 1" in text
    assert "……还有 2 个，加 -a 全部列出" in text and "\n· R2（2026-" in text and "，1 局）" in text
    assert "玩家名：" not in text and text.endswith("/bf1/player/2")


def test_info_role_flags(module):
    """旧 bot 天眼查的 -o/-a/-v/-b，中文 -服主/-管理/-封禁 也认。"""

    info = _alconna("天眼查")
    result = info.parse("/天眼查 abc -b -管理")
    assert result.query("name") == "abc"
    assert [k for k in ("owner", "admin", "vip", "ban") if result.query(k) is not None] == [
        "admin",
        "ban",
    ]
    assert info.parse("/天眼查 -v -t").query("vip") is not None
    assert info.parse("/天眼查 abc").query("ban") is None


async def test_player_info_full_list(module, player, rendered):
    player["/stats/2/servers"] = INFO_SERVERS

    def unused(limit):
        raise AssertionError("全部列出时不查最近游玩")

    player["/stats/2/rounds"] = unused
    rendered["result"] = b"jpeg"
    await module.info_reply("abc", object(), full=("admin", "vip"))
    ctx = rendered["context"]
    roles = {r["key"]: r for r in ctx["roles"]}
    assert len(roles["admin"]["rows"]) == 10 and roles["admin"]["more"] == 0
    assert roles["vip"]["full"] and not roles["vip"]["rows"]
    # 没要的只留个数
    assert roles["ban"]["total"] == 1 and not roles["ban"]["rows"] and not roles["ban"]["full"]
    assert ctx["full"] and not ctx["recent"] and (ctx["columns"], ctx["width"]) == (1, 1000)
    many = {**INFO_SERVERS, "ban": INFO_SERVERS["admin"] * 7}
    player["/stats/2/servers"] = many
    await module.info_reply("abc", object(), full=("ban",))
    assert (rendered["context"]["columns"], rendered["context"]["width"]) == (3, 1900)
    rendered["result"] = None
    player["/stats/2/servers"] = INFO_SERVERS
    text = await module.info_reply("abc", None, full=("admin", "vip"))
    assert "管理：\n1. S0\n" in text and "\n10. S9\n" in text and "VIP：没有" in text
    assert "还有" not in text and "最近游玩" not in text


async def test_player_info_without_rsp(module, player, rendered):
    def down():
        raise module.Bf1Error("查询服务暂时连不上，稍后再试")

    player["/stats/2/servers"] = down
    player["/stats/2/rounds"] = lambda limit: down()
    rendered["result"] = b"jpeg"
    await module.info_reply("abc", object())
    assert not rendered["context"]["rounds"] and rendered["context"]["recent"] == []
    rendered["result"] = None
    text = await module.info_reply("abc", None)
    assert "私服身份暂时查不到" in text and "最近游玩" not in text


async def test_wiki_catalog(module, rendered):
    rendered["result"] = b"jpeg"
    reply = await module.wiki_reply("武器", object())
    assert isinstance(reply[0], Image)
    ctx = rendered["context"]
    assert rendered["template"] == "catalog.html" and ctx["count"] == 123
    assert len(ctx["columns"]) == 4 and ctx["columns"][0][0] == (1, "MP18")
    assert sum(len(c) for c in ctx["columns"]) == 123
    assert module.pages.catalog_page("全世界", module.codex.search_wiki("全世界"))["width"] == 980
    overview = await module.wiki_reply("目录", None)
    assert overview.startswith("百科共 310 条：武器 123 条、载具 40 条")


CAREER = {
    "honors": {"mvp": 109, "ace_squad": 426, "ribbons": 51487, "medals": 82},
    "combat": {"headshot_rate": 0.0947, "accuracy": 0.28, "squad_wipes": 2364},
    "teamwork": {"flag_captures": 4083, "resupplies": 57872},
    "modes": [
        {"mode": "rush", "wins": 3, "losses": 7, "win_rate": 0.3, "score": 14000},
        {"mode": "conquest", "wins": 1036, "losses": 1101, "win_rate": 0.4848, "score": 7816460},
        {"mode": "air_assault", "wins": 0, "losses": 0, "win_rate": None, "score": None},
    ],
    "elite_kits": [
        {"kit": "flamethrower", "kills": 52, "time_seconds": 2678},
        {"kit": "sentry", "kills": None, "time_seconds": 3838},
        {"kit": "infiltrator", "kills": 0, "time_seconds": 0},
    ],
    "maps": [
        {"map": "MP_Underworld", "time_seconds": 7200},
        {"map": "MP_Ridge", "time_seconds": 3600},
    ],
    "ribbons": [
        {"code": "r13", "count": 6872},
        {"code": "r01", "count": 4097},
        {"code": "r05", "count": 0},
    ],
    "misc": {"rounds_completed": 3268, "rounds_played": 4299, "completion_rate": 0.76},
}


def test_collection_data(module):
    c = module.collection
    assert (len(c.RIBBONS), len(c.MEDALS), len(c.DOGTAGS)) == (47, 60, 320)
    assert c.RIBBON["r13"].name and c.grouped(c.RIBBONS)[0][0] == "载具"
    assert c.image("dogtags", "df082", "https://x/") == "https://x/bf1/codex/dogtags/df082.webp"
    assert len(module.names.MAPS) == 32 and module.names.MAPS["MP_Ridge"] == "阿奇巴巴"


async def test_stats_card_career_extras(module, player, rendered):
    player["/stats/2"] = {**STATS, **CAREER}
    rendered["result"] = b"jpeg"
    await module.stats_reply("abc", object())
    ctx = rendered["context"]
    kpis = dict(ctx["kpis"])
    assert (kpis["MVP"], kpis["王牌小队"]) == ("109", "426")
    details = dict(ctx["details"])
    assert (details["爆头率"], details["命中率"], details["团灭"]) == ("9.5%", "28.0%", "2,364")
    assert details["占点"] == "4,083" and details["压制助攻"] == "-"
    # 按局数排，没玩过的不列；目标得分到百万级用“万”
    assert [(m["name"], m["score"]) for m in ctx["modes"]] == [
        ("征服", "782 万"),
        ("突袭", "1.4 万"),
    ]
    assert [(k["name"], k["kills"], k["kpm"]) for k in ctx["elite"]] == [
        ("哨兵", "-", "-"),
        ("喷火兵", "52", "1.17"),
    ]
    assert [(m["name"], m["share"]) for m in ctx["maps"]] == [
        ("法乌克斯要塞", "66.7%"),
        ("阿奇巴巴", "33.3%"),
    ]
    assert ctx["maps"][0]["image"].endswith("/bf1/codex/maps-480/MP_Underworld.webp")
    assert (ctx["wide"], ctx["width"]) == (True, 1760)
    assert [r["count"] for r in ctx["ribbons"]] == [6872, 4097]
    assert dict(ctx["honors"])["完成率"] == "76.0%"
    rendered["result"] = None
    text = await module.stats_reply("abc", None)
    assert "MVP 109 | 王牌小队 426 | 勋带 51487 | 勋章 82 | 完成率 76.0%" in text
    assert "模式：征服 1,036 / 1,101（48.5%）；突袭 3 / 7（30.0%）" in text
    assert "常玩地图：法乌克斯要塞 2 小时；阿奇巴巴 1 小时" in text


async def test_stats_card_without_career_extras(module, player, rendered):
    """旧的接口没有这些字段：对应部分不画，也不出错。"""

    rendered["result"] = b"jpeg"
    await module.stats_reply("abc", object())
    ctx = rendered["context"]
    assert len(ctx["kpis"]) == 4 and not ctx["details"] and not ctx["modes"]
    assert not ctx["maps"] and not ctx["elite"] and not ctx["honors"] and not ctx["ribbons"]
    # 没有扩展数据时照旧一栏
    assert (ctx["wide"], ctx["width"]) == (False, 1000)


def _detailed(item, **player):
    """单局详情：所有人一个列表，本人那一行多了生涯对比。"""

    other = {**item["player"], "persona_id": 9}
    return {**item["round"], "players": [other, {**item["player"], **player}]}


async def test_recent_rounds_with_honors_and_career(module, rounds, rendered):
    rounds["/rounds/5"] = _detailed(
        ROUNDS[0],
        battle_score=7700,
        mvp=True,
        ribbons=[{"code": "r05", "count": 3}, {"code": "r15", "count": 1}],
        rank_to=87,
        kit_ranks=["scout"],
        records={"longest_headshot": 312},
        medals=1,
        stars=[{"kind": "weapon", "name": "罗斯 MKIII（步兵）", "stars": 3}],
        skill_delta=-2.5,
        combat={"shots": 200, "hits": 50, "headshots": 6},
        score_parts={
            "general": 5770,
            "team": 168,
            "squad": 1015,
            "objective": 750,
            "other": -3,
            "award": 2500,
            "bonus": 0,
        },
        career={"kd": 2.0, "kpm": 1.5, "spm": 900.0, "accuracy": 0.2, "headshot_rate": 0.1},
        career_after={
            "kd": 2.01,
            "kpm": 1.5,
            "spm": 899.0,
            "accuracy": 0.2002,
            "headshot_rate": 0.1,
        },
    )
    rendered["result"] = b"jpeg"
    await module.recent_reply("abc", object())
    first, left = rendered["context"]["rounds"][:2]
    assert first["mvp"] and not first["ace"]
    # 不是正数的项（“其他”的负零头、没有的结算奖励）不画；总经验是含奖励的总分
    segments = first["parts"]["segments"]
    assert [x["label"] for x in segments] == ["战斗", "团队", "小队", "目标", "勋带勋章"]
    assert round(sum(x["pct"] for x in segments)) == 100 and first["parts"]["total"] == "10,000"
    stats = {c["label"]: c for c in first["stats"]}
    assert stats["得分"]["value"] == "7,700"
    kd = stats["KD"]["compare"]
    assert (kd["career"], kd["shift"], kd["trend"]) == ("2.00", "+0.50%", "up")
    assert stats["KPM"]["compare"]["shift"] == "持平"
    assert stats["SPM"]["compare"]["trend"] == "down"
    assert stats["命中率"]["value"] == "25.0%" and stats["爆头率"]["value"] == "20.0%"
    assert [text for text, _ in first["growth"]] == [
        "升到 87 级",
        "侦察兵升级",
        "最远爆头新纪录 312 米",
        "完成勋章 1",
        "罗斯 MKIII（步兵） ★3",
        "技巧值 -2.5",
    ]
    assert [r["name"] for r in first["ribbons"]] == [
        module.collection.RIBBON["r05"].name,
        module.collection.RIBBON["r15"].name,
    ]
    rendered["result"] = None
    text = await module.recent_reply("abc", None)
    assert "胜利 MVP\n" in text and "得分 7,700" in text
    assert "对比生涯：KD 3.00（生涯 2.00 +0.50%）；KPM 3.00（生涯 1.50 持平）" in text
    assert "升到 87 级、侦察兵升级" in text and "×3" in text
    assert (
        "得分构成：战斗 5,770 | 团队 168 | 小队 1,015 | 目标 750 | 勋带勋章 2,500 | 总经验 10,000"
        in text
    )
    # 没取到详情的局：得分用总分，不比生涯
    assert {c["label"]: c["value"] for c in left["stats"]}["得分"] == "10,000"
    assert (
        all(c["compare"] is None for c in left["stats"])
        and not left["growth"]
        and not left["parts"]
    )


COLLECTION = {
    "persona_id": 2,
    "medals": [
        {"code": "m05", "unlocked": True, "stages_done": 0, "stages_total": 5},
        {"code": "m06", "unlocked": False, "stages_done": 2, "stages_total": 5},
    ],
    "dogtags": [
        {"id": "df082", "unlocked": True, "acquired": None, "needed": None},
        {"id": "dbxp516", "unlocked": False, "acquired": 30, "needed": 120},
    ],
    "equipped": ["df082", "nope"],
}


async def test_collection_card(module, player, rendered):
    player["/stats/2/collection"] = COLLECTION
    player["/stats/2"] = {**STATS, **CAREER}
    rendered["result"] = b"jpeg"
    reply = await module.collection_reply("abc", object())
    assert str(reply[1]).startswith("玩家名：abc\nPID：2\n")
    ctx = rendered["context"]
    assert rendered["template"] == "collection.html"
    assert [t["name"] for t in ctx["equipped"]] == [module.collection.DOGTAG["df082"].name]
    medals = {m["name"]: m for _, items in ctx["medals"] for m in items}
    m05, m06 = (medals[module.collection.MEDAL[c].name] for c in ("m05", "m06"))
    assert (m05["got"], m05["progress"]) == (True, "") and m06["progress"] == "2/5"
    tags = {t["name"]: t for _, items in ctx["dogtags"] for t in items}
    assert tags[module.collection.DOGTAG["dbxp516"].name]["progress"] == 25
    assert (ctx["medals_got"], ctx["dogtags_got"], ctx["ribbons_got"]) == (1, 1, 2)
    assert ctx["ribbons_sum"] == 6872 + 4097 and len(ctx["ribbons"]) == 6
    rendered["result"] = None
    text = await module.collection_reply("abc", None)
    assert "勋章 1/60" in text and "进行中：" in text and "2/5" in text
    assert "狗牌 1/320：" in text and "勋带 2/47 种，共 10,969 次" in text


async def test_collection_without_ribbons_or_data(module, player, rendered):
    player["/stats/2/collection"] = COLLECTION
    player["/stats/2"] = STATS  # 旧接口没有勋带
    rendered["result"] = b"jpeg"
    await module.collection_reply("abc", object())
    assert rendered["context"]["ribbons"] == []
    del player["/stats/2/collection"]
    reply = await module._answer(module.collection_reply("abc", None))
    assert reply.startswith("测试里没有")


def test_weapon_against_its_own_career(module):
    """命中率开火满 50 发才比，KPM 要用满 3 分钟；差不到 5% 不标箭头。"""

    compare = module.pages._gear_compare
    gun = {
        "kind": "weapon",
        "kills": 6,
        "hits": 30,
        "shots": 200,
        "seconds": 240,
        "career_accuracy": 0.1,
        "career_kpm": 1.48,
    }
    rows = {r["label"]: r for r in compare(gun)}
    assert (rows["命中"]["value"], rows["命中"]["career"], rows["命中"]["trend"]) == (
        "15.0%",
        "10.0%",
        "up",
    )
    assert (rows["KPM"]["value"], rows["KPM"]["trend"]) == ("1.50", None)
    assert compare({**gun, "shots": 49, "seconds": 179}) == []
    # 载具不比命中率；没有生涯值（不是单局详情）也不比
    assert [r["label"] for r in compare({**gun, "kind": "vehicle"})] == ["KPM"]
    assert compare({**gun, "career_accuracy": None, "career_kpm": None}) == []


async def test_recent_rounds_two_columns(module, rounds, rendered):
    rendered["result"] = b"jpeg"
    await module.recent_reply("abc", object())
    assert (rendered["context"]["columns"], rendered["context"]["width"]) == (2, 1760)
    rounds["/stats/2/rounds"] = {"items": [ROUNDS[0]], "next_before": None}
    await module.recent_reply("abc", object())
    assert (rendered["context"]["columns"], rendered["context"]["width"]) == (1, 1000)


async def test_stats_card_platoon_panel(module, player, rendered):
    """照旧 bot：战队单独一块，带人数和描述；没加入战队时那一块放小贴士。"""

    player["/stats/2/platoon"] = {
        "tag": "EA",
        "name": "Electronic Arts",
        "size": 77,
        "description": "Employees of Electronic Arts - Invite Only.",
        "verified": True,
    }
    rendered["result"] = b"jpeg"
    await module.stats_reply("abc", object())
    platoon = rendered["context"]["platoon"]
    assert platoon["description"].startswith("Employees") and platoon["emblem_url"] is None
    assert not rendered["context"]["tip"]["text"]
    rendered["result"] = None
    text = await module.stats_reply("abc", None)
    assert "战队：[EA] Electronic Arts（77 人，EA 认证）\n战队描述：Employees of" in text
    player["/stats/2/platoon"] = None
    rendered["result"] = b"jpeg"
    await module.stats_reply("abc", object())
    assert rendered["context"]["platoon"] is None and rendered["context"]["tip"]["text"]


async def test_stats_card_equipped_dogtags(module, player, rendered):
    """装备的狗牌放在名字旁边，取自收藏接口；收藏接口取不到时不画、不报错。"""

    name = module.collection.DOGTAG["df082"].name
    player["/stats/2/collection"] = COLLECTION
    rendered["result"] = b"jpeg"
    await module.stats_reply("abc", object())
    ctx = rendered["context"]
    assert [t["name"] for t in ctx["equipped"]] == [name]
    assert ctx["equipped"][0]["image"].endswith("/bf1/codex/dogtags/df082.webp")
    assert html_has(module, ctx, 'class="worn"', name)
    rendered["result"] = None
    assert f"装备狗牌：{name}\n" in await module.stats_reply("abc", None)
    del player["/stats/2/collection"]
    rendered["result"] = b"jpeg"
    await module.stats_reply("abc", object())
    assert rendered["context"]["equipped"] == []
    assert not html_has(module, rendered["context"], 'class="worn"')
