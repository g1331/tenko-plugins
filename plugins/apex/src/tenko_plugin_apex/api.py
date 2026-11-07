"""Apex Legends Status 的接口：请求和要回给群友的错误。"""

from __future__ import annotations

from typing import Any

import httpx
from loguru import logger

from .config import config

API = "https://api.mozambiquehe.re"


class ApexError(Exception):
    """要原样回给群友的错误。"""


async def request(url: str, **params: Any) -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            return await client.get(url, params=params)
    except httpx.HTTPError as error:
        logger.warning("Apex 查询请求失败：{} {}", url, type(error).__name__)
        raise ApexError("查询服务暂时连不上，稍后再试") from error


async def fetch(url: str, **params: Any) -> Any:
    response = await request(url, **params)
    try:
        data = response.json()
    except ValueError:
        data = None
    if response.status_code == 429:
        raise ApexError("查得太快了，过几秒再试")
    # 查无此人时有的返回 404，有的返回 200 带 Error 字段
    if isinstance(data, dict) and "Error" in data:
        if "not found" in str(data["Error"]).casefold():
            raise ApexError("没找到这名玩家：PC 玩家要用 EA 账号名，其他平台记得带上平台")
        logger.info("Apex 接口报错：{}", data["Error"])
        raise ApexError("查询失败，稍后再试")
    if response.status_code >= 400 or data is None:
        raise ApexError(f"查询失败（{response.status_code}）")
    return data


async def get(path: str, **params: Any) -> Any:
    if not config.api_key:
        raise ApexError("还没配置 Apex Legends Status 的 API key，请 Master 在插件配置里填 api_key")
    return await fetch(f"{API}{path}", auth=config.api_key, **params)
