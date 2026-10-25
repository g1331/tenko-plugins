import pytest
from satori import Image, Text

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
