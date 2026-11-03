"""各家搜索服务的请求和返回格式：纯逻辑，经 module fixture 取子模块。"""

import json

import httpx
import pytest


@pytest.fixture
def websearch(module):
    return module.websearch


def client(handle):
    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


async def test_tavily_sends_key_and_reads_content(websearch):
    seen = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "新赛季", "url": "https://www.ea.com/x", "content": "加了新地图"},
                    {"title": "", "url": "https://empty.example", "content": ""},
                ]
            },
        )

    results = await websearch.search(client(handle), "tavily", " 战地6 新赛季 ", api_key="tvly-1")
    assert seen["url"] == "https://api.tavily.com/search"
    assert seen["auth"] == "Bearer tvly-1"
    assert seen["body"]["query"] == "战地6 新赛季" and seen["body"]["max_results"] == 4
    # 站点取域名，空结果丢掉
    assert results == [
        websearch.Result("新赛季", "ea.com", "", "加了新地图", "https://www.ea.com/x")
    ]


async def test_bocha_prefers_long_summary_and_reports_errors(websearch):
    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.bochaai.com/v1/web-search"
        assert json.loads(request.content)["summary"] is True
        page = {
            "name": "新赛季",
            "url": "https://news.example/1",
            "siteName": "某新闻",
            "snippet": "短",
            "summary": "长摘要",
            "datePublished": "2026-10-20T08:00:00+08:00",
        }
        return httpx.Response(200, json={"code": 200, "data": {"webPages": {"value": [page]}}})

    (result,) = await websearch.search(client(handle), "bocha", "新赛季", api_key="sk-1")
    assert (result.site, result.date, result.text) == ("某新闻", "2026-10-20", "长摘要")

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 403, "msg": "余额不足"})

    with pytest.raises(ValueError, match="余额不足"):
        await websearch.search(client(failing), "bocha", "新赛季", api_key="sk-1")


async def test_searxng_uses_own_instance_without_key(websearch):
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search" and request.url.params["format"] == "json"
        assert "Authorization" not in request.headers
        hits = [
            {"title": f"r{i}", "url": f"https://s{i}.example", "content": "c"} for i in range(9)
        ]
        return httpx.Response(200, json={"results": hits})

    results = await websearch.search(
        client(handle), "searxng", "q", api_key="", base_url="http://searx.local/", count=3
    )
    assert [r.title for r in results] == ["r0", "r1", "r2"]


def test_problem_explains_unusable_settings(websearch):
    assert websearch.problem("tavily", "tvly-1", "") is None
    assert "API key" in websearch.problem("tavily", "", "")
    assert "地址" in websearch.problem("searxng", "", "")
    assert "可选" in websearch.problem("google", "k", "")
