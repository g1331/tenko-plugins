from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from satori import Image, Text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

_REAL_CLIENT = httpx.AsyncClient

PLAYER = {
    "global": {
        "name": "abc",
        "platform": "PC",
        "level": 120,
        "levelPrestige": 1,
        "rank": {"rankName": "Diamond", "rankDiv": 2, "rankScore": 14500},
        "bans": {"isActive": False},
    },
    "realtime": {"currentStateAsText": "In match (05:12)", "selectedLegend": "Wraith"},
    "legends": {
        "selected": {
            "data": [
                {"name": "BR Season 31 wins", "value": 5, "key": "wins_season_31"},
                {"name": "BR Kills", "value": 1234, "key": "kills"},
                {"name": "Smoke Show", "value": 7, "key": "smoke_show_damage_done"},
                {"name": "BR Wins", "value": None, "key": "wins"},
            ]
        }
    },
}


@pytest_asyncio.fixture(autouse=True)
async def db(module, monkeypatch):
    """内存 SQLite 代替宿主给插件的库。"""

    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(module.Snapshot.__table__.create)
    monkeypatch.setattr(module, "get_session", lambda: AsyncSession(engine))
    yield
    await engine.dispose()


async def test_plugin_loads_on_by_default(runtime):
    assert runtime.loaded_plugins["apex"].metadata.feature_scope == "group"


async def test_player(module, monkeypatch):
    seen = {}

    async def fake_get(path, **params):
        seen.update(params, path=path)
        return PLAYER

    monkeypatch.setattr(module, "_get", fake_get)
    reply = await module.player_reply("abc", "PS5")
    assert seen == {"path": "/bridge", "player": "abc", "platform": "PS4"}
    assert "等级 120（1 转）| 段位 钻石 2 14500 分" in reply.replace(" |", "|")
    assert "状态 比赛中 (05:12) | 当前英雄 恶灵" in reply
    # 认不全的追踪器保留原文
    # 没有全英雄汇总时用当前横幅；技能类追踪器不显示
    assert "生涯汇总：击杀 1234 | 第 31 赛季胜场 5\n" in reply
    assert "英雄击杀" not in reply
    assert "胜场 None" not in reply
    assert "禁赛" not in reply


async def test_bad_platform_and_missing_key(module, monkeypatch):
    assert await module._answer(module.player_reply("abc", "nes")) == (
        "平台只能是 PC、PS、Xbox 或 Switch"
    )
    monkeypatch.setattr(module.config, "api_key", "")
    assert "api_key" in await module._answer(module.rotation_reply())


def _respond(module, monkeypatch, status, body):
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    monkeypatch.setattr(
        module.httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=transport, **kw)
    )


async def test_api_errors_become_replies(module, monkeypatch):
    _respond(module, monkeypatch, 200, {"Error": "Player abc not found"})
    assert await module._answer(module.player_reply("abc", None)) == (
        "没找到这名玩家：PC 玩家要用 EA 账号名，其他平台记得带上平台"
    )
    _respond(module, monkeypatch, 429, {})
    assert await module._answer(module.predator_reply()) == "查得太快了，过几秒再试"
    _respond(module, monkeypatch, 500, None)
    assert await module._answer(module.predator_reply()) == "查询失败（500）"


async def test_rotation_predator_news(module):
    rotation = module.format_rotation(
        {
            "battle_royale": {
                "current": {"map": "Kings Canyon", "remainingTimer": "00:12:34"},
                "next": {"map": "Mystery Map"},
            },
            "ranked": {"current": {"map": "E-District"}},
            "ltm": {"current": {"map": "Habitat", "eventName": "Gun Run"}},
        }
    )
    assert "匹配：诸王峡谷 剩 00:12:34，下一张 Mystery Map" in rotation
    assert "排位：电力区\n" in rotation
    assert "限时模式：子弹时间 · 栖息地" in rotation

    predator = module.format_predator(
        {"RP": {"PC": {"val": 15000, "totalMastersAndPreds": 9000}, "SWITCH": None}}
    )
    assert "PC：15000 分 | 大师及猎杀 9000 人" in predator
    assert "Switch" not in predator

    news = module.format_news([{"title": "改版", "date": "2026-10-20", "url": "u"}])
    assert news.endswith("1. [10-20] 改版\n   u")
    assert module.format_news([]) == "暂时没有资讯"


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


async def test_player_card(module, monkeypatch, rendered):
    async def fake_get(path, **params):
        return PLAYER

    monkeypatch.setattr(module, "_get", fake_get)
    rendered["result"] = b"jpeg"
    reply = await module.player_reply("abc", None, object())
    assert isinstance(reply[0], Image)
    ctx = rendered["context"]
    assert rendered["template"] == "player.html"
    assert (ctx["tier"], ctx["rank_score"], ctx["core"], ctx["trend"]) == (
        "钻石 2",
        "14,500",
        [],
        None,
    )
    assert ctx["rank_progress"] == {"next": "钻石 1", "need": "500", "pct": 50}
    assert ctx["ban"] == ""

    # 渲染失败回文字；字段缺得很多也不报错
    rendered["result"] = None
    PLAYER_BANNED = {
        "global": {
            "name": "x",
            "bans": {
                "isActive": True,
                "last_banReason": "COMPETITIVE_ABANDON",
                "remainingSeconds": 61,
            },
        }
    }
    monkeypatch.setattr(module, "_get", lambda *a, **k: _value(PLAYER_BANNED))
    reply = await module.player_reply("x", None, object())
    assert isinstance(reply, str) and "排位禁赛：中途退出排位，还剩 2 分钟" in reply
    assert rendered["context"]["state"] == "-"


async def _value(value):
    return value


async def test_rotation_and_predator_cards(module, monkeypatch, rendered):
    rotation = {
        "battle_royale": {
            "current": {
                "map": "Kings Canyon",
                "asset": "kc.png",
                "remainingTimer": "00:15:00",
                "remainingSecs": 900,
                "DurationInSecs": 3600,
            },
            "next": {"map": "Olympus"},
        },
        "ranked": {"current": {"map": "E-District"}},
        "ltm": {},
    }
    monkeypatch.setattr(module, "_get", lambda *a, **k: _value(rotation))
    await module.rotation_reply(object())
    modes = rendered["context"]["modes"]
    assert [m["label"] for m in modes] == ["匹配", "排位"]
    assert (modes[0]["map"], modes[0]["elapsed"], modes[0]["next"]) == ("诸王峡谷", 75, "奥林匹斯")
    assert (modes[1]["remaining"], modes[1]["elapsed"]) == ("", 0)

    predator = {"RP": {"PC": {"val": 15000, "totalMastersAndPreds": 9000}, "X1": None}}
    monkeypatch.setattr(module, "_get", lambda *a, **k: _value(predator))
    await module.predator_reply(object())
    # 猎杀线低于大师门槛（大师不满 750 人）时不显示“高出”
    assert rendered["context"]["platforms"] == [
        {
            "code": "PC",
            "label": "PC",
            "score": "15,000",
            "above": "",
            "delta": None,
            "masters": "9,000",
            "pos": None,
            "masters_pct": 100,
            "updated": "",
        }
    ]


EA_PAGE = """
<a href="/zh-hant/games/apex-legends/apex-legends/news/ranked-roadmap" class="x"><div>
<img alt="a" class="p" src="https://drop-assets.ea.com/r.jpg?im=Crop&amp;q=40"/></div>
<span class="t">2026年10月8日</span>
<h3 class="h"><span class="c">《Apex 英雄》：積分賽路線圖</span></h3></a>
<a href="/zh-hant/games/apex-legends/apex-legends/news/ranked-roadmap" class="x"><h3>重複</h3></a>
<a href="/zh-hant/games/apex-legends/apex-legends/news" class="nav">新聞</a>
<a href="/zh-hant/games/apex-legends/apex-legends/news/no-image"><h3>沒有圖片</h3></a>
"""


async def test_news_card(module, monkeypatch, rendered):
    async def fake_request(url, **params):
        if url == module.pages.EA_NEWS:
            return SimpleNamespace(status_code=200, text=EA_PAGE)
        if url.endswith("ranked-roadmap"):
            return SimpleNamespace(
                status_code=200, text='<meta content="黑暗降臨&amp;峽谷" name="description"/>'
            )
        raise module.ApexError("down")

    monkeypatch.setattr(module, "_request", fake_request)
    rendered["result"] = b"jpeg"
    reply = await module.news_reply(object())
    url = "https://www.ea.com/zh-hant/games/apex-legends/apex-legends/news/ranked-roadmap"
    assert reply[1] == Text(url)
    first, second = rendered["context"]["items"]
    assert first == {
        "title": "《Apex 英雄》：积分赛路线图",
        "date": "2026-10-08",
        "cover": "https://drop-assets.ea.com/r.jpg?im=Crop&q=40",
        "url": url,
        "summary": "黑暗降临&峡谷",
    }
    # 文章页取不到时没有简介，也不影响整张图
    assert (second["title"], second["date"], second["cover"], second["summary"]) == (
        "没有图片",
        "",
        "",
        "",
    )
    assert len(
        module.pages.description('<meta content="' + "长" * 200 + '" name="description"')
    ) == (module.pages.SUMMARY + 1)


# 结构取自真实的 /bridge 返回
FULL = {
    "global": {
        "name": "abc",
        "platform": "X1",
        "level": 232,
        "rank": {
            "rankName": "Bronze",
            "rankDiv": 3,
            "rankScore": 1803,
            "ladderPosPlatform": -1,
            "ALStopPercentGlobal": 88.88,
        },
        "bans": {"isActive": False, "remainingSeconds": 0},
    },
    "realtime": {"isOnline": 1, "lobbyState": "open", "partyFull": 0},
    "legends": {
        "selected": {"LegendName": "Bangalore", "data": [{"name": "BR Kills", "value": 104}]},
        "all": {
            "Global": {"data": [{"name": "X", "value": 999, "key": "kills"}]},
            "Bangalore": {
                "data": [
                    {
                        "name": "BR Kills",
                        "value": 104,
                        "key": "kills",
                        "rank": {"rankPos": 3288497, "topPercent": 98.43},
                    }
                ],
                "ImgAssets": {"icon": "b.png"},
            },
            "Gibraltar": {
                "data": [
                    {
                        "name": "BR Kills",
                        "value": 208,
                        "key": "kills",
                        "rank": {"topPercent": "NOT_CALCULATED_YET"},
                    }
                ]
            },
            "Horizon": {"data": [{"name": "BR Kills", "value": 0, "key": "kills"}]},
            "Wraith": {"ImgAssets": {"icon": "w.png"}},
        },
    },
    "total": {
        "kills": {"name": "BR Kills", "value": 917},
        "kills_season_10": {"name": "BR Season 10 kills", "value": 31},
        "kills_season_12": {"name": "BR Season 12 kills", "value": 12},
        "specialEvent_kills": {"name": "BR Kills", "value": 40},
        "lmg_kills": {"name": "BR LMG kills", "value": 7},
        "kd": {"value": "-1", "name": "KD"},
    },
}


async def test_player_totals_and_legends(module, monkeypatch, rendered):
    monkeypatch.setattr(module, "_get", lambda *a, **k: _value(FULL))
    reply = await module.player_reply("abc", "xbox", object())
    ctx = rendered["context"]
    assert ctx["platform"] == "Xbox"
    assert ctx["rank_position"] == "全球前 88.88%"
    assert ctx["party"] == "队伍开放加入"
    # KD 为 -1 表示没数据，不显示；卡片只放核心数据，赛季数据画成趋势
    assert ctx["core"] == [("击杀", "917")]
    assert ctx["trend"] == {
        "label": "击杀",
        "bars": [
            {"season": "S10", "value": "31", "pct": 100},
            {"season": "S12", "value": "12", "pct": 39},
        ],
    }
    # 文字回复里活动击杀和普通击杀按 key 区分，赛季新的在前
    assert module.pages.totals(FULL) == [
        ("击杀", 917),
        ("轻机枪击杀", 7),
        ("活动击杀", 40),
        ("第 12 赛季击杀", 12),
        ("第 10 赛季击杀", 31),
    ]
    assert ctx["legends"] == [
        {"name": "直布罗陀", "icon": "", "kills": "208", "top": "", "ratio": 100},
        {"name": "班加罗尔", "icon": "b.png", "kills": "104", "top": "全球前 98.43%", "ratio": 50},
    ]
    assert "段位 青铜 3 1803 分（全球前 88.88%）" in reply
    assert "英雄击杀：直布罗陀 208 | 班加罗尔 104" in reply

    FULL["total"]["kd"]["value"] = "1.5"
    assert module.pages.totals(FULL)[0] == ("KD", "1.50")
    assert module.pages.core_stats(FULL)[0] == ("KD", "1.50")


async def test_servers(module, monkeypatch, rendered):
    data = {
        "Origin_login": {"Asia": {"Status": "UP", "ResponseTime": 52}},
        "EA_novafusion": {"Asia": {"Status": "SLOW", "ResponseTime": 131}},
        "otherPlatforms": {"Playstation-Network": {"Status": "DOWN", "ResponseTime": None}},
    }
    monkeypatch.setattr(module, "_get", lambda *a, **k: _value(data))
    reply = await module.servers_reply(object())
    ctx = rendered["context"]
    assert ctx["sections"][0]["cells"][0] == {"status": "正常", "css": "up", "ms": "52 ms"}
    assert ctx["sections"][1]["cells"][0]["status"] == "缓慢"
    assert ctx["platforms"][0] == {
        "label": "PlayStation 网络",
        "status": "中断",
        "css": "down",
        "ms": "",
    }
    # 没给数据的地区算未知，不算异常
    assert (ctx["summary"], ctx["healthy"]) == ("2 项异常", False)
    assert "EA 后端：亚洲 缓慢、欧洲西部 未知" in reply
    assert "PlayStation 网络：中断" in reply


def test_rank_progress(module):
    progress = module.pages.rank_progress
    # 钻石 2 是 14000 起，下一段钻石 1 从 15000 起
    assert progress({"rankName": "Diamond", "rankDiv": 2, "rankScore": 14520}) == {
        "next": "钻石 1",
        "need": "480",
        "pct": 52,
    }
    # 钻石 1 的下一段是大师，大师没有小段
    assert progress({"rankName": "Diamond", "rankDiv": 1, "rankScore": 15900})["next"] == "大师"
    assert progress({"rankName": "Master", "rankDiv": 0, "rankScore": 17000}) is None
    assert progress({"rankName": "Unranked", "rankDiv": 4, "rankScore": 0}) is None


async def test_rank_delta_survives_repeat_queries(module, monkeypatch, rendered):
    data = {"global": {"name": "abc", "uid": "1", "rank": {"rankScore": 1000}}}
    monkeypatch.setattr(module, "_get", lambda *a, **k: _value(data))
    clock = iter([0.0, 3600.0, 7200.0, 7300.0])
    monkeypatch.setattr(module.time, "time", lambda: next(clock))

    await module.player_reply("abc", None, object())
    assert rendered["context"]["rank_delta"] is None  # 第一次查没有可比的

    data["global"]["rank"]["rankScore"] = 1086
    reply = await module.player_reply("abc", None, object())
    assert rendered["context"]["rank_delta"] == {"text": "+86", "css": "up", "ago": "1 小时前"}

    # 分数没变时不覆盖记录，仍和 1 小时前那次比
    await module.player_reply("abc", None, object())
    assert rendered["context"]["rank_delta"]["text"] == "持平"
    data["global"]["rank"]["rankScore"] = 1000
    await module.player_reply("abc", None, object())
    assert rendered["context"]["rank_delta"] == {"text": "-86", "css": "down", "ago": "1 小时前"}
    assert isinstance(reply, str) and "比 1 小时前：+86" in reply


async def test_predator_delta(module, monkeypatch, rendered):
    data = {"RP": {"PC": {"val": 20000, "totalMastersAndPreds": 9000}, "X1": None}}
    monkeypatch.setattr(module, "_get", lambda *a, **k: _value(data))
    await module.predator_reply(object())
    first = rendered["context"]["platforms"][0]
    assert (first["above"], first["delta"], first["masters_pct"]) == ("4,000", None, 100)
    data["RP"]["PC"]["val"] = 20500
    await module.predator_reply(object())
    assert rendered["context"]["platforms"][0]["delta"]["text"] == "+500"


def test_predator_line(module):
    data = {"RP": {"PC": {"val": 20000}, "PS4": {"val": 18000}, "X1": {"val": 15000}}}
    # 线从大师门槛 16000 画到最高的猎杀线 20000；没到大师的平台不画
    assert [p["pos"] for p in module.pages.predator_page(data)["platforms"]] == [100, 50, None]
