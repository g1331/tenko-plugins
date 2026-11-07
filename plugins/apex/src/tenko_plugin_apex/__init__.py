"""Apex 英雄查询：玩家战绩、地图轮换、猎杀线和官方资讯。

战绩、地图和猎杀线来自 Apex Legends Status 的 API（需要 API key）；
资讯取 EA 官网繁中版新闻并转成简体，不需要 key。

这里只声明命令，每条命令取出参数后交给 :mod:`.info`；请求在 :mod:`.api`，
图片排版在 :mod:`.pages`，渲染不了时回文字。
"""

from __future__ import annotations

from arclet.alconna import Alconna, Args, CommandMeta
from arclet.entari import command, plugin
from arclet.entari.command import Query
from tenko_api.features import declare
from tenko_api.render import RenderService

from . import info
from .config import ApexConfig
from .info import answer

plugin.metadata(
    "Apex 查询",
    author=["13"],
    version="0.1.0",
    description="查询 Apex 英雄玩家战绩、地图轮换、猎杀线、服务器状态和官方资讯。",
    urls={
        "homepage": "https://github.com/g1331/tenko-plugins/tree/main/plugins/apex",
        "repository": "https://github.com/g1331/tenko-plugins",
    },
    config=ApexConfig,
)
declare(scope="group", default_enabled=True)


player_command = Alconna(
    "apex",
    Args["name", str]["platform?", str],
    meta=CommandMeta(
        "查询 Apex 玩家的等级、段位和横幅数据；PC 玩家用 EA 账号名",
        usage="apex <玩家名> [PC|PS|Xbox|Switch]",
        example="/apex ImperialHal",
    ),
)


@command.on(player_command)
async def apex(
    name: Query[str] = Query("name"),  # noqa: B008
    platform: Query[str] = Query("platform"),  # noqa: B008
    *,
    render_service: RenderService,
):
    platform_name = platform.result if platform.available else None
    return await answer(info.player_reply(name.result, platform_name, render_service))


@command.on(Alconna("apex地图", meta=CommandMeta("Apex 当前和下一张地图", usage="apex地图")))
async def apex_map(*, render_service: RenderService):
    return await answer(info.rotation_reply(render_service))


@command.on(Alconna("apex猎杀", meta=CommandMeta("Apex 各平台猎杀线", usage="apex猎杀")))
async def apex_predator(*, render_service: RenderService):
    return await answer(info.predator_reply(render_service))


@command.on(Alconna("apex资讯", meta=CommandMeta("Apex 最新官方公告", usage="apex资讯")))
async def apex_news(*, render_service: RenderService):
    return await answer(info.news_reply(render_service))


@command.on(Alconna("apex服务器", meta=CommandMeta("Apex 各地区服务器状态", usage="apex服务器")))
async def apex_servers(*, render_service: RenderService):
    return await answer(info.servers_reply(render_service))
