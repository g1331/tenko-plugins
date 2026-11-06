"""bf-manager 的公开查询接口：请求、错误和按名字找玩家。"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger

from .config import config


class Bf1Error(Exception):
    """要原样回给群友的错误。"""


class NoSuchPlayer(Bf1Error):
    """按名字没找到玩家。"""


async def get(path: str, **params: Any) -> Any:
    # EA 接口慢，战绩和武器常要好几秒。
    url = f"{config.base_url.rstrip('/')}/api/v1/bf1{path}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, params=params)
    except httpx.HTTPError as error:
        logger.warning("bf-manager 请求失败：{} {}", url, type(error).__name__)
        raise Bf1Error("查询服务暂时连不上，稍后再试") from error
    if response.status_code >= 400:
        try:
            message = response.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = f"查询失败（{response.status_code}）"
        raise Bf1Error(message)
    return response.json()


async def optional(path: str, **params: Any) -> Any:
    """封禁、战队、在线状态这类附加信息拿不到就不显示，不影响主战绩。"""

    try:
        return await get(path, **params)
    except Bf1Error:
        return None


async def find_persona(name: str) -> dict[str, Any]:
    result = await get("/players/search", name=name)
    personas = result["personas"]
    # 搜索是模糊匹配，精确同名的不一定排第一。
    exact = [p for p in personas if p["display_name"].casefold() == name.casefold()]
    if not exact:
        if not personas:
            raise NoSuchPlayer(f"没找到玩家 {name}")
        names = "、".join(p["display_name"] for p in personas[:5])
        raise NoSuchPlayer(f"没找到玩家 {name}，相近的有：{names}")
    # 重名时 bf-manager 会回填生涯时长，0 是没玩过 BF1 的空号。
    return next((p for p in exact if p.get("time_played_hours")), exact[0])


def source() -> str:
    """卡片角落写的数据来源。"""

    return urlparse(config.base_url).netloc or config.base_url


def site(path: str) -> str:
    """bf-manager 网页上的地址。"""

    return f"{config.base_url.rstrip('/')}{path}"


def player_link(pid: int, tab: str = "") -> str:
    """bf-manager 网页的玩家页，分区记在 # 后面。"""

    return site(f"/bf1/player/{pid}{tab and '#' + tab}")
