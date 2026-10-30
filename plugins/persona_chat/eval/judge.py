"""照群主的标准两两盲评：同一个场景下的两种回法，评委挑更像真人群友的那个。

每对正反各问一次（A、B 换位），两次指向同一边才算赢，不然记“平”：评委偏爱先出现的那个，
只问一次的话一半的胜负是位置给的。评委先拿群主亲手评过的题对过一致率（``calibrate``）再用。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx

RUBRIC = (Path(__file__).with_name("rubric.md")).read_text()
_TOKEN = re.compile(r"\[表情[:：]([^\]]+)\]")
_QUOTED = re.compile(r"^↪#(\d+) ")
_CONTEXT = 10
_RETRIES = 4


def context(lines: list[dict], cut: int) -> list[str]:
    """截到 ``cut`` 的最近几条，按群友看到的样子写：“昵称：内容”，她自己写作“她”。"""

    rows = [x for x in lines if x["number"] <= cut][-_CONTEXT:]
    return [f"{_who(x)}：{x['text'][:200].replace('@你', '@她')}" for x in rows]


def _who(line: dict) -> str:
    if line["speaker"] is None:
        return "她"
    if line["speaker"] == "Tenko 功能":
        return "[她的号上的查询功能]"
    return line["speaker"].rpartition("(")[0] or line["speaker"]


def show(bubbles: list[str], lines: list[dict], moods: dict[str, str]) -> str:
    """她的回法：“｜”分隔连着发的几条，表情包带上备注，引用写明引的是谁的哪句。"""

    by_number = {x["number"]: x for x in lines}
    out = []
    for bubble in bubbles:
        if quoted := _QUOTED.match(bubble):
            old = by_number.get(int(quoted[1]))
            head = f"[引用 {_who(old)}：{old['text'][:20]}] " if old else ""
            bubble = head + bubble[quoted.end() :]
        out.append(_TOKEN.sub(lambda m: f"[表情包：{m[1]}（{moods.get(m[1], '')[:30]}）]", bubble))
    return " ｜ ".join(out) or "（不说话）"


class Judge:
    def __init__(self, client: httpx.AsyncClient, model: str, limit: int = 6) -> None:
        self.client, self.model = client, model
        self.gate = asyncio.Semaphore(limit)
        self.usage: list[dict] = []

    async def _ask(self, ctx: list[str], a: str, b: str) -> str:
        prompt = (
            f"{RUBRIC}\n\n聊天片段（“她”是小埋，@她 是在叫她）：\n"
            + "\n".join(ctx)
            + f"\n\nA：{a}\nB：{b}\n\n"
            '只输出 JSON：{"why": "一两句理由", "winner": "A 或 B 或 平 或 都差"}'
        )
        payload = {
            "model": self.model,
            "max_tokens": 8000,  # 评委开着思考，思考也算在里面，给少了正文是空的
            "messages": [{"role": "user", "content": prompt}],
        }
        for attempt in range(_RETRIES):
            try:
                async with self.gate:
                    response = await self.client.post("/chat/completions", json=payload)
                response.raise_for_status()
                break
            except (httpx.TransportError, httpx.HTTPStatusError):
                if attempt == _RETRIES - 1:
                    return ""
                await asyncio.sleep(3 * (attempt + 1))
        data = response.json()
        self.usage.append(data.get("usage") or {})
        text = data["choices"][0]["message"].get("content") or ""
        try:
            return str(json.loads(re.search(r"\{.*\}", text, re.S)[0]).get("winner", "")).strip()
        except (TypeError, ValueError):
            return ""

    async def compare(self, ctx: list[str], a: str, b: str) -> str:
        """返回 "A"、"B" 或 "平"（含都差、两次说法不一）。"""

        if a == b:
            return "平"
        first, second = await asyncio.gather(self._ask(ctx, a, b), self._ask(ctx, b, a))
        second = {"A": "B", "B": "A"}.get(second, second)
        return first if first == second and first in ("A", "B") else "平"
