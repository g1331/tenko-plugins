"""网页搜索：几家搜索服务的接口和返回格式各不相同，这里统一成同样几项（标题、站点、日期、摘要）。

每家一个函数，按 ``search_provider`` 的名字分发；换服务、换密钥都是调用时才读，不用重载插件。
打开网页看正文（:func:`read`）也按服务分：有自己读网页接口的用它的，没有的用 Jina Reader。
网页都在对方服务器上抓，群里发的网址不会从我们这台机器去访问。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

_QUERY_CHARS = 128  # 搜索词本来就该短；DeepSeek 只收这么长
# 给她看的正文上限：Tavily 按她要找的挑出几段，通常 2000 字以内；Jina 只能从头截，导航也算在里面。
_PAGE_CHARS = 3000
_JINA = "https://r.jina.ai"


@dataclass(frozen=True)
class Result:
    title: str
    site: str
    date: str  # YYYY-MM-DD，拿不到时为空
    text: str
    url: str = ""


Fetch = Callable[[httpx.AsyncClient, str, str, str, int], Awaitable[list[Result]]]
# (http, base_url, api_key, 网址, 想找什么) -> 正文
Read = Callable[[httpx.AsyncClient, str, str, str, str], Awaitable[str]]


async def _jina_read(
    http: httpx.AsyncClient, base_url: str, api_key: str, url: str, want: str
) -> str:
    """Jina Reader：不要 key（也不发搜索服务的 key 过去），按 IP 限每分钟 20 次左右。"""

    response = await http.get(f"{_JINA}/{url}", headers={"X-Return-Format": "text"})
    response.raise_for_status()
    return response.text


@dataclass(frozen=True)
class Provider:
    fetch: Fetch
    base_url: str  # 默认地址；空的表示必须在配置里填
    cost: float  # 每次大约多少美元，和对话的用量记在一起
    charge_empty: bool  # 没搜到结果也收费
    needs_key: bool = True
    read: Read = _jina_read  # 打开网页看正文
    read_cost: float = 0.0  # 打开一页大约多少美元，打不开不收


def _site(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host.removeprefix("www.")


def _date(value: object) -> str:
    text = str(value or "")[:10]
    return text if len(text) == 10 and text[4] == "-" and text[7] == "-" else ""


def _bearer(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


async def _deepseek(
    http: httpx.AsyncClient, base_url: str, api_key: str, query: str, count: int
) -> list[Result]:
    """DeepSeek 开放平台的 ``/v1/tools/search``：和对话共用 API key，搜不到不收费。"""

    response = await http.post(
        f"{base_url}/v1/tools/search",
        headers=_bearer(api_key),
        json={"query": query, "count": count},
    )
    response.raise_for_status()
    return [
        Result(
            title=r.get("title") or "",
            site=r.get("site_name") or _site(r.get("url") or ""),
            date=_date(r.get("date")),
            text=r.get("summary") or r.get("snippet") or "",
            url=r.get("url") or "",
        )
        for r in response.json().get("results") or []
    ]


async def _tavily(
    http: httpx.AsyncClient, base_url: str, api_key: str, query: str, count: int
) -> list[Result]:
    """Tavily 的 ``/search``：basic 深度每次 1 credit，免费账号每月 1000 次。"""

    response = await http.post(
        f"{base_url}/search",
        headers=_bearer(api_key),
        json={"query": query, "max_results": count, "search_depth": "basic", "topic": "general"},
    )
    response.raise_for_status()
    return [
        Result(
            title=r.get("title") or "",
            site=_site(r.get("url") or ""),
            date=_date(r.get("published_date")),
            text=r.get("content") or "",
            url=r.get("url") or "",
        )
        for r in response.json().get("results") or []
    ]


async def _tavily_read(
    http: httpx.AsyncClient, base_url: str, api_key: str, url: str, want: str
) -> str:
    """Tavily 的 ``/extract``：按 ``want`` 挑最相关的几段；basic 每 5 个网址 1 credit，
    打不开不收。"""

    body: dict = {"urls": [url], "extract_depth": "basic", "format": "text"}
    if want:
        body |= {"query": want, "chunks_per_source": 3}
    response = await http.post(f"{base_url}/extract", headers=_bearer(api_key), json=body)
    response.raise_for_status()
    return next((r.get("raw_content") or "" for r in response.json().get("results") or []), "")


async def _bocha(
    http: httpx.AsyncClient, base_url: str, api_key: str, query: str, count: int
) -> list[Result]:
    """博查的 ``/v1/web-search``：国内网页收得全，带长摘要（``summary``）。"""

    response = await http.post(
        f"{base_url}/v1/web-search",
        headers=_bearer(api_key),
        json={"query": query, "count": count, "summary": True},
    )
    response.raise_for_status()
    body = response.json()
    if body.get("code") not in (None, 200, "200"):
        raise ValueError(f"博查返回 {body.get('code')}：{body.get('msg')}")
    pages = ((body.get("data") or {}).get("webPages") or {}).get("value") or []
    return [
        Result(
            title=r.get("name") or "",
            site=r.get("siteName") or _site(r.get("url") or ""),
            date=_date(r.get("datePublished") or r.get("dateLastCrawled")),
            text=r.get("summary") or r.get("snippet") or "",
            url=r.get("url") or "",
        )
        for r in pages
    ]


async def _searxng(
    http: httpx.AsyncClient, base_url: str, api_key: str, query: str, count: int
) -> list[Result]:
    """自建的 SearXNG：``settings.yml`` 的 ``search.formats`` 里要开 ``json``。"""

    response = await http.get(
        f"{base_url}/search", headers=_bearer(api_key), params={"q": query, "format": "json"}
    )
    response.raise_for_status()
    return [
        Result(
            title=r.get("title") or "",
            site=_site(r.get("url") or ""),
            date=_date(r.get("publishedDate")),
            text=r.get("content") or "",
            url=r.get("url") or "",
        )
        for r in (response.json().get("results") or [])[:count]
    ]


PROVIDERS: dict[str, Provider] = {
    # 每次 0.005 元
    "deepseek": Provider(_deepseek, "", 0.0007, charge_empty=False),
    # 按量 0.008 美元一个 credit
    "tavily": Provider(
        _tavily,
        "https://api.tavily.com",
        0.008,
        charge_empty=True,
        read=_tavily_read,
        read_cost=0.0016,
    ),
    # 每次 0.036 元
    "bocha": Provider(_bocha, "https://api.bochaai.com", 0.005, charge_empty=True),
    "searxng": Provider(_searxng, "", 0.0, charge_empty=False, needs_key=False),
}


def problem(name: str, api_key: str, base_url: str) -> str | None:
    """这套配置用不了时说明原因，能用时返回 None。"""

    provider = PROVIDERS.get(name)
    if provider is None:
        return f"未知的搜索服务“{name}”，可选：{'、'.join(PROVIDERS)}"
    if provider.needs_key and not api_key:
        return f"搜索服务 {name} 需要 API key"
    if not (base_url or provider.base_url):
        return f"搜索服务 {name} 需要填地址"
    return None


async def search(
    http: httpx.AsyncClient,
    name: str,
    query: str,
    *,
    api_key: str,
    base_url: str = "",
    count: int = 4,
) -> list[Result]:
    """用 ``name`` 这家服务搜一次。配置不对时抛 ValueError，接口出错时抛 httpx.HTTPError。"""

    if reason := problem(name, api_key, base_url):
        raise ValueError(reason)
    provider = PROVIDERS[name]
    url = (base_url or provider.base_url).rstrip("/")
    results = await provider.fetch(http, url, api_key, query.strip()[:_QUERY_CHARS], count)
    return [r for r in results if r.title or r.text][:count]


async def read(
    http: httpx.AsyncClient,
    name: str,
    url: str,
    want: str,
    *,
    api_key: str,
    base_url: str = "",
) -> str:
    """用 ``name`` 这家服务打开 ``url``，返回和 ``want`` 有关的正文（空行去掉，
    截到 _PAGE_CHARS 字），打不开是空的。配置不对、不是 http(s) 网址时抛 ValueError，
    接口出错时抛 httpx.HTTPError。"""

    if reason := problem(name, api_key, base_url):
        raise ValueError(reason)
    if urlsplit(url).scheme not in ("http", "https"):
        raise ValueError(f"只能打开 http(s) 网址：{url}")
    provider = PROVIDERS[name]
    root = (base_url or provider.base_url).rstrip("/")
    text = await provider.read(http, root, api_key, url, want.strip()[:_QUERY_CHARS])
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)[:_PAGE_CHARS]
