"""bf-manager 的 BF6 公开查询接口：请求、错误和站点地址。"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
from loguru import logger

from .config import config


class Bf6Error(Exception):
    """要原样回给群友的错误。"""


class NoSuchPlayer(Bf6Error):
    """按名字没找到玩家。"""


async def get(path: str, **params: Any) -> Any:
    # bf-manager 缓存了生涯 20 分钟，没命中时要现找 EA 拉，可能要好几秒。
    url = f"{config.base_url.rstrip('/')}/api/v1/bf6{path}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, params=params)
    except httpx.HTTPError as error:
        logger.warning("bf-manager 请求失败：{} {}", url, type(error).__name__)
        raise Bf6Error("查询服务暂时连不上，稍后再试") from error
    if response.status_code >= 400:
        try:
            body = response.json()["error"]
            code, message = body.get("code"), body["message"]
        except (ValueError, KeyError, TypeError):
            code, message = None, f"查询失败（{response.status_code}）"
        raise (NoSuchPlayer if code == "BF6_PLAYER_NOT_FOUND" else Bf6Error)(message)
    return response.json()


async def resolve(name: str) -> dict[str, Any]:
    """EA 显示名 → nucleus_id、显示名、平台和头像；只认完全同名。"""

    player = await get("/players/resolve", name=name)
    # EA 偶尔不回显示名，按名字搜到的就用搜的名字
    return {**player, "display_name": player.get("display_name") or name}


def source() -> str:
    """卡片角落写的数据来源。"""

    return urlparse(config.base_url).netloc or config.base_url


def site(path: str | None) -> str | None:
    """bf-manager 站内路径换成完整地址；图片都托管在那里。"""

    return f"{config.base_url.rstrip('/')}{path}" if path else None


# Drop API 的平台命名空间 → 网页地址里的平台参数、卡片上写的平台名
PLATFORMS = {
    "cem_ea_id": ("ea", "EA"),
    "ps3": ("psn", "PlayStation"),
    "xbox": ("xbox", "Xbox"),
    "epic": ("epic", "Epic"),
}


def platform_label(namespace: str | None) -> str | None:
    return PLATFORMS[namespace][1] if namespace in PLATFORMS else None


def player_link(player: dict[str, Any]) -> str:
    """网页的玩家页；带上名字和平台，网页才显示名字和头像。"""

    params = {"name": player.get("display_name")}
    if player.get("platform") in PLATFORMS:
        params["platform"] = PLATFORMS[player["platform"]][0]
    query = urlencode({k: v for k, v in params.items() if v})
    return f"{config.base_url.rstrip('/')}/bf6/player/{player['nucleus_id']}?{query}".rstrip("?")
