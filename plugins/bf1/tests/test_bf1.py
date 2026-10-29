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
    assert html_has(module, ctx, "e.png", "正在游戏 · S1", "[EA]")


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
                    {"display_name": "low", "rank": 3, "stats": None},
                    {"display_name": "top", "rank": 150, "is_admin": True, "stats": {"kd": 2.5}},
                ],
            }
        ],
        "queued": [{"display_name": "wait", "rank": 91}],
        "spectators": [],
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
    assert "美国（2）：low、top" in reply and reply.endswith(
        "其他 1 个：/bf1ss test\nhttps://bfm.umrbotech.com/bf1/server/7#players"
    )
    assert ctx["waiting"] == [("排队", "#34d399", [("wait", 91)])] and "排队（1）：wait" in reply

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
